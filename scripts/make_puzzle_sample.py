"""Generate the small puzzle bank that ships with the app (sample_data/puzzles_sample.csv).

The real bank is the Lichess puzzle database (CC0, about 5 million puzzles), loaded with
`python scripts/import_puzzles.py --download`. This script makes a few hundred puzzles of our own, in the
same CSV format, so a fresh install (and the test suite) has something to serve before that import.

How a puzzle is found (the same idea as Lichess's own puzzle generator):
  1. Replay games (the demo games plus Stockfish games at a low skill level, which blunder often).
  2. After each move, look for a position where the side to move suddenly has one winning move: the best move
     wins (75%+ expected score, or mates) and the second-best is at least 25 points worse.
  3. Extend the line while each of the solver's moves is still the only good one (up to three moves, or the
     whole mate).
  4. Tag themes (mate in N, fork, pin, hanging piece, sacrifice, promotion, quiet move, phase, length...) and
     estimate a rating from how deep Stockfish must search to find the first move.

  python scripts/make_puzzle_sample.py --games 700 --out run1.csv
  python scripts/make_puzzle_sample.py --games 700 --seed 23 --no-demo --out run2.csv
  python scripts/make_puzzle_sample.py --merge run1.csv run2.csv --out sample_data/puzzles_sample.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import sys
from pathlib import Path

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analysis import PIECE_VALUE, game_phase, win_percent  # noqa: E402
from app.config import load_settings  # noqa: E402

MATE = 10_000


def score_cp(info, turn) -> int:
    return info["score"].pov(turn).score(mate_score=MATE)


def top2(engine, board: chess.Board, depth: int):
    infos = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=2)
    if isinstance(infos, dict):
        infos = [infos]
    out = []
    for i in infos:
        if i.get("pv") and "score" in i:
            out.append((i["pv"][0], score_cp(i, board.turn), i["score"].pov(board.turn).mate(), i["pv"]))
    out.sort(key=lambda x: -x[1])
    return out


def unique_winner(engine, board: chess.Board, depth: int, need_win: float = 70.0, gap: float = 22.0):
    """The only winning move here, or None."""
    c = top2(engine, board, depth)
    if not c:
        return None
    best = c[0]
    w1 = win_percent(best[1])
    if best[2] is not None and best[2] > 0:
        # A mate: unique if the second move doesn't mate as fast
        if len(c) > 1 and c[1][2] is not None and 0 < c[1][2] <= best[2]:
            return None
        return best
    if w1 < need_win:
        return None
    if len(c) > 1 and w1 - win_percent(c[1][1]) < gap:
        return None
    return best


def find_depth(engine, board: chess.Board, move: chess.Move) -> int:
    """The shallowest search depth at which Stockfish already prefers `move` (a difficulty proxy)."""
    for d in range(1, 15):
        info = engine.analyse(board, chess.engine.Limit(depth=d))
        pv = info.get("pv") or []
        if pv and pv[0] == move:
            return d
    return 15


# ---- themes ------------------------------------------------------------------------------------
def themes(board: chess.Board, line: list[chess.Move], final_cp: int, mate_n: int | None) -> list[str]:
    out: list[str] = []
    b = board.copy()
    first = line[0]
    mover = b.turn
    piece = b.piece_at(first.from_square)
    victim = b.piece_at(first.to_square)
    forcing = b.is_capture(first) or b.gives_check(first) or first.promotion is not None
    phase = game_phase(b)
    out.append(phase)
    if phase == "endgame":
        kinds = {p.piece_type for p in b.piece_map().values() if p.piece_type not in (chess.KING, chess.PAWN)}
        out += {frozenset(): ["pawnEndgame"], frozenset({chess.ROOK}): ["rookEndgame"], frozenset({chess.QUEEN}): ["queenEndgame"],
                frozenset({chess.BISHOP}): ["bishopEndgame"], frozenset({chess.KNIGHT}): ["knightEndgame"]}.get(frozenset(kinds), [])
    if not forcing:
        out.append("quietMove")
    if victim is not None and not b.is_attacked_by(not mover, first.to_square):
        out.append("hangingPiece")
    # Sacrifice: the first move puts a piece where something cheaper can take it (and it isn't a plain capture-up)
    after = b.copy()
    after.push(first)
    if piece and piece.piece_type != chess.PAWN:
        cheaper = [a for a in after.attackers(not mover, first.to_square) if PIECE_VALUE[after.piece_type_at(a)] < PIECE_VALUE[piece.piece_type]]
        gained = PIECE_VALUE[victim.piece_type] if victim else 0
        if cheaper and PIECE_VALUE[piece.piece_type] - gained >= 2:
            out.append("sacrifice")
    # Fork: the moved piece attacks two or more valuable targets (or the king and one)
    if piece:
        def is_target(sq: int) -> bool:
            t = after.piece_at(sq)
            if t is None or t.color == mover:
                return False
            if t.piece_type == chess.KING or PIECE_VALUE[t.piece_type] > PIECE_VALUE[piece.piece_type]:
                return True
            return PIECE_VALUE[t.piece_type] >= 3 and not after.is_attacked_by(not mover, sq)  # undefended

        if sum(is_target(sq) for sq in after.attacks(first.to_square)) >= 2:
            out.append("fork")
    # Pin: after the move, an enemy piece is pinned to its king
    if any(after.is_pinned(not mover, sq) and not b.is_pinned(not mover, sq) for sq in chess.SQUARES
           if after.piece_at(sq) and after.piece_at(sq).color != mover and after.piece_at(sq).piece_type != chess.KING):
        out.append("pin")
    # Discovered attack: moving this piece uncovers a slider's attack on something valuable
    for sq in chess.SQUARES:
        p = b.piece_at(sq)
        if not p or p.color != mover or sq == first.from_square or p.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            continue
        new = set(after.attacks(sq)) - set(b.attacks(sq))
        if any(after.piece_at(t) and after.piece_at(t).color != mover and PIECE_VALUE[after.piece_at(t).piece_type] >= 3
               or (after.piece_at(t) and after.piece_at(t).piece_type == chess.KING and after.piece_at(t).color != mover) for t in new):
            out.append("discoveredAttack")
            break
    if any(m.promotion for m in line[::2]):
        out.append("promotion")
    if mate_n:
        out += ["mate", f"mateIn{mate_n}"]
        end = board.copy()
        for m in line:
            end.push(m)
        k = end.king(end.turn)
        last = line[-1]
        if k is not None and chess.square_rank(k) in (0, 7) and end.piece_type_at(last.to_square) in (chess.ROOK, chess.QUEEN):
            out.append("backRankMate")
        if end.piece_type_at(last.to_square) == chess.KNIGHT and k is not None and all(
                end.piece_at(s) is not None and end.piece_at(s).color == end.turn for s in chess.SquareSet(chess.BB_KING_ATTACKS[k])):
            out.append("smotheredMate")
    else:
        w = win_percent(final_cp)
        out.append("crushing" if w >= 90 else "advantage")
    solver_moves = (len(line) + 1) // 2
    out.append({1: "oneMove", 2: "short", 3: "long"}.get(solver_moves, "veryLong"))
    return out


def estimate_rating(depth_found: int, solver_moves: int, tags: list[str], rng: random.Random) -> int:
    r = 750 + 95 * depth_found + 140 * (solver_moves - 1)
    r += 180 * ("quietMove" in tags) + 130 * ("sacrifice" in tags) - 120 * ("mateIn1" in tags) - 60 * ("hangingPiece" in tags)
    r += rng.randint(-60, 60)
    return max(600, min(2700, int(r)))


# ---- sources -----------------------------------------------------------------------------------
def demo_games() -> list[list[chess.Move]]:
    text = (ROOT / "sample_data" / "demo_games.pgn").read_text()
    out, stream = [], io.StringIO(text)
    while (g := chess.pgn.read_game(stream)) is not None:
        out.append(list(g.mainline_moves()))
    return out


def selfplay(engine, n: int, rng: random.Random) -> list[list[chess.Move]]:
    games = []
    for _ in range(n):
        engine.configure({"Skill Level": rng.randint(0, 7)})
        b = chess.Board()
        moves: list[chess.Move] = []
        for _ in range(rng.randint(2, 6)):  # a random opening
            legal = list(b.legal_moves)
            mv = rng.choice(legal)
            moves.append(mv)
            b.push(mv)
        while not b.is_game_over() and len(moves) < 140:
            mv = engine.play(b, chess.engine.Limit(time=0.01)).move
            moves.append(mv)
            b.push(mv)
        games.append(moves)
    engine.configure({"Skill Level": 20})
    return games


def mine(engine, game: list[chess.Move], depth: int, rng: random.Random, seen: set[str]) -> list[dict]:
    b = chess.Board()
    quick = chess.engine.Limit(depth=8)
    prev_w = None  # previous position's expected score for the side to move then
    out = []
    for i, mv in enumerate(game):
        setup = b.copy()
        b.push(mv)
        if b.is_game_over() or i < 6:
            prev_w = None
            continue
        info = engine.analyse(b, quick)
        w = win_percent(score_cp(info, b.turn))
        # The opponent's move (mv) turned a balanced position into a winning one for the side to move now.
        if prev_w is not None and w >= 70 and (100 - prev_w) <= 65 and b.epd() not in seen:
            pz = build(engine, setup, mv, depth, rng)
            if pz:
                seen.add(b.epd())
                out.append(pz)
        prev_w = w
    return out


def build(engine, setup: chess.Board, setup_move: chess.Move, depth: int, rng: random.Random) -> dict | None:
    b = setup.copy()
    b.push(setup_move)
    start = b.copy()
    line: list[chess.Move] = []
    mate_n = None
    final_cp = 0
    for k in range(6):
        w = unique_winner(engine, b, depth)
        if w is None:
            break
        mv, cp, mate, pv = w
        if k == 0 and mate is not None and mate > 0:
            if mate > 3:
                return None
            mate_n = mate
        line.append(mv)
        final_cp = cp
        b.push(mv)
        if b.is_game_over():
            break
        if mate_n is None and len(line) >= 3:
            break
        reply = engine.analyse(b, chess.engine.Limit(depth=depth)).get("pv")
        if not reply:
            break
        line.append(reply[0])
        b.push(reply[0])
    if not line:
        return None
    if len(line) % 2 == 0:  # must end on the solver's move
        line = line[:-1]
    if len(line) == 1 and setup.is_capture(setup_move) and line[0].to_square == setup_move.to_square:
        return None  # just taking back: not a puzzle
    if mate_n is not None:
        end = start.copy()
        for m in line:
            end.push(m)
        if not end.is_checkmate():
            return None
        mate_n = (len(line) + 1) // 2
    tags = themes(start, line, final_cp, mate_n)
    d = find_depth(engine, start, line[0])
    rating = estimate_rating(d, (len(line) + 1) // 2, tags, rng)
    return {"FEN": setup.fen(), "Moves": " ".join([setup_move.uci()] + [m.uci() for m in line]), "Rating": rating,
            "RatingDeviation": 150, "Popularity": 0, "NbPlays": 0, "Themes": " ".join(tags), "GameUrl": "", "OpeningTags": ""}


def merge(paths: list[str], out: str, mean: float = 1550, sd: float = 420) -> int:
    """Combine several runs, drop duplicates, and spread the estimated ratings over a realistic range by rank
    (the estimates order puzzles by difficulty well, but their scale is arbitrary)."""
    from statistics import NormalDist

    rows, seen = [], set()
    for p in paths:
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                k = (r["FEN"], r["Moves"])
                if k not in seen:
                    seen.add(k)
                    rows.append(r)
    rows.sort(key=lambda r: (int(r["Rating"]), r["Moves"]))
    nd = NormalDist(mean, sd)
    for i, r in enumerate(rows):
        r["Rating"] = max(700, min(2700, round(nd.inv_cdf((i + 0.5) / len(rows)))))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["PuzzleId", "FEN", "Moves", "Rating", "RatingDeviation", "Popularity", "NbPlays", "Themes",
                                          "GameUrl", "OpeningTags"])
        w.writeheader()
        for i, r in enumerate(rows, 1):
            w.writerow({**r, "PuzzleId": f"pb{i:05d}"})
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", type=int, default=150, help="Stockfish self-play games to mine (plus the 40 demo games)")
    ap.add_argument("--depth", type=int, default=13)
    ap.add_argument("--out", default=str(ROOT / "sample_data" / "puzzles_sample.csv"))
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--no-demo", action="store_true", help="skip the demo games (for a second, parallel run)")
    ap.add_argument("--merge", nargs="+", metavar="CSV", help="combine earlier runs into --out (no engine work)")
    args = ap.parse_args()
    if args.merge:
        print(f"Wrote {merge(args.merge, args.out)} puzzles to {args.out}")
        return
    s = load_settings()
    rng = random.Random(args.seed)
    engine = chess.engine.SimpleEngine.popen_uci(s.stockfish_path)
    engine.configure({"Threads": 1, "Hash": 64})
    seen: set[str] = set()
    puzzles: list[dict] = []
    sources = ([] if args.no_demo else demo_games()) + selfplay(engine, args.games, rng)
    for gi, g in enumerate(sources, 1):
        puzzles += mine(engine, g, args.depth, rng, seen)
        if gi % 10 == 0:
            print(f"{gi}/{len(sources)} games, {len(puzzles)} puzzles", flush=True)
    engine.quit()
    puzzles.sort(key=lambda p: p["Rating"])
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["PuzzleId", "FEN", "Moves", "Rating", "RatingDeviation", "Popularity", "NbPlays", "Themes",
                                          "GameUrl", "OpeningTags"])
        w.writeheader()
        for i, p in enumerate(puzzles, 1):
            w.writerow({"PuzzleId": f"pb{i:05d}", **p})
    print(f"Wrote {len(puzzles)} puzzles to {args.out}")


if __name__ == "__main__":
    main()
