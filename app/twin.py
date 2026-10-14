"""Opponent Twin: a practice bot that plays like one specific person, built from their saved games.

How the Twin chooses a move
---------------------------
1. **Their own book.** If they have reached this position before (by any move order, from the Opponent
   Library's position index), the Twin replays what *they* chose, weighted toward recent games. It trusts the
   book in proportion to the evidence: with one past game it follows it 40% of the time, with five games
   83%, so a single odd game doesn't become a rule.
2. **Their decision model.** Out of book, Stockfish lists the top moves (plus two random legal moves, so
   bad moves are possible, as they are for humans). Each candidate gets features: how much it loses (in
   expected score, separately for the opening, middlegame, endgame and time trouble), whether it's the
   engine's top move, a capture, an even trade, a check, castling, a pawn push, a forward or king-hunting
   piece move, an early queen sortie, moving the same piece again, a king walk. The Twin picks with the
   probabilities of a **conditional-logit choice model** whose weights were fitted to *this player's* real
   decisions (maximum likelihood, with a rating-based prior so a small sample can't run wild).
   That is what makes it theirs: how often they blunder and in which phase, and what they like to do when
   several moves are about equal.

How it's built (a background job)
---------------------------------
Their most recent games are replayed. At each of their moves Stockfish scores the candidates, and their
actual move is recorded. The model is fitted with Newton's method (pure Python, no extra dependencies)
on 80% of the games and tested on the other 20%: the **faithfulness** score is how often the Twin's top
choice is the move they really played, next to how often Stockfish's top move is. Then it's refitted on all
games. The profile (accuracy by phase, blunders, time-trouble errors, style, openings) comes from the same data.

Drawbacks handled
-----------------
* Few games: builds need 10+ games; the rating prior keeps a small sample sensible; confidence is shown.
* Blitz vs classical: the Twin can be built from chosen time controls only (openings differ by speed).
* Old habits: book moves are weighted by recency (half-life one year).
* Memorising one game: book use is probabilistic and the model samples, so no two games are the same.
* Engine-perfect play: the Twin only plays as well as the person; "on a good day" sharpens it a little.
* Fair play: it's a practice opponent inside the app, built from public games; it never runs during live games.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import time
from collections import defaultdict

import chess
import chess.engine

from .analysis import PIECE_VALUE, _time_pressure, classify, game_phase, move_accuracy, win_percent
from .games import GameRecord
from .library import position_key, record_from_json

VERSION = 2  # bump when features change: older studies and Twins are then rebuilt
MIN_GAMES = 10
FEATURES = ["loss_opening", "loss_middlegame", "loss_endgame", "loss_pressure", "best", "top3", "capture", "trade", "check",
            "castle", "pawn_push", "forward", "king_hunt", "queen_early", "same_piece", "king_walk", "recapture", "escape",
            "develop"]
NF = len(FEATURES)
LOSS = range(4)  # indexes of the move-quality weights
TOP_N = 10  # engine candidates per decision
EXTRA_RANDOM = 2  # random legal moves added per decision (so bad moves can be chosen, as humans do)
SKIP_FIRST = 3  # their first moves are memorised theory: the book covers them
MAX_DECISIONS_PER_GAME = 30
BOOK_HALF_LIFE_DAYS = 365
GOOD_DAY = 1.6  # "on a good day": move-quality weights x1.6
TIME_CLASSES = ("bullet", "blitz", "rapid", "classical")
STYLE_TEXT = {  # (feature, sign) -> what it means
    ("trade", 1): "Happy to trade pieces", ("trade", -1): "Avoids trades",
    ("check", 1): "Loves giving check", ("king_hunt", 1): "Goes after your king",
    ("pawn_push", 1): "Pushes pawns", ("pawn_push", -1): "Keeps the pawns back",
    ("queen_early", 1): "Brings the queen out early", ("same_piece", 1): "Moves the same piece again and again",
    ("king_walk", 1): "Walks the king", ("capture", 1): "Grabs material", ("capture", -1): "Keeps the tension",
    ("forward", 1): "Plays forward, active moves", ("forward", -1): "Likes regrouping and quiet retreats",
    ("castle", -1): "Delays castling", ("castle", 1): "Castles early",
    ("develop", 1): "Develops pieces quickly", ("develop", -1): "Slow to develop pieces",
    ("escape", 1): "Moves attacked pieces away", ("escape", -1): "Leaves pieces en prise to play their own ideas",
    ("recapture", -1): "Doesn't always take back", ("top3", 1): "Usually finds a good, natural move",
}


def tc_key(classes: list[str] | None) -> str:
    wanted = sorted({c for c in (classes or []) if c in TIME_CLASSES})
    return "all" if not wanted or len(wanted) == len(TIME_CLASSES) else ",".join(wanted)


def es(cp: int) -> float:
    """Expected score (0-1) for the side to move, from centipawns."""
    return win_percent(cp) / 100


# ---- features -------------------------------------------------------------------------------------
def features(board: chess.Board, move: chess.Move, loss: float, rank: int, pressure: bool, prev_to: int | None,
             opp_to: int | None = None) -> list[float]:
    """The feature vector of one candidate move. `loss`: expected score lost vs the best move (0-1); `rank`: the
    engine's ranking (0 = best); `prev_to`: where this player's last move landed; `opp_to`: the opponent's."""
    i = {name: k for k, name in enumerate(FEATURES)}
    f = [0.0] * NF
    q = -10.0 * loss  # a 10% expected-score loss is -1
    phase = game_phase(board)
    f[i["loss_" + phase]] = q
    if pressure:
        f[i["loss_pressure"]] = q
    f[i["best"]] = 1.0 if rank == 0 else 0.0
    f[i["top3"]] = 1.0 if rank < 3 else 0.0
    piece = board.piece_at(move.from_square)
    victim = board.piece_at(move.to_square)
    if board.is_en_passant(move):
        victim = chess.Piece(chess.PAWN, not board.turn)
    if victim is not None:
        f[i["capture"]] = 1.0
        if piece and PIECE_VALUE[victim.piece_type] == PIECE_VALUE[piece.piece_type]:
            f[i["trade"]] = 1.0
        if opp_to is not None and move.to_square == opp_to:
            f[i["recapture"]] = 1.0  # humans take back almost automatically
    if board.gives_check(move):
        f[i["check"]] = 1.0
    if board.is_castling(move):
        f[i["castle"]] = 1.0
    if piece is None:
        return f
    if piece.piece_type == chess.PAWN and victim is None:
        f[i["pawn_push"]] = 1.0
    if piece.piece_type not in (chess.PAWN, chess.KING):
        dr = chess.square_rank(move.to_square) - chess.square_rank(move.from_square)
        f[i["forward"]] = (dr if board.turn == chess.WHITE else -dr) / 7
        king = board.king(not board.turn)
        if king is not None:
            f[i["king_hunt"]] = (chess.square_distance(move.from_square, king) - chess.square_distance(move.to_square, king)) / 7
        # Moving a piece that's attacked by something cheaper, or attacked and undefended
        if board.is_attacked_by(not board.turn, move.from_square):
            cheaper = any(PIECE_VALUE[board.piece_type_at(a)] < PIECE_VALUE[piece.piece_type]
                          for a in board.attackers(not board.turn, move.from_square))
            if cheaper or not board.is_attacked_by(board.turn, move.from_square):
                f[i["escape"]] = 1.0
        home = 0 if board.turn == chess.WHITE else 7
        if piece.piece_type in (chess.KNIGHT, chess.BISHOP) and chess.square_rank(move.from_square) == home and phase == "opening":
            f[i["develop"]] = 1.0
    if piece.piece_type == chess.QUEEN and board.fullmove_number <= 10:
        f[i["queen_early"]] = 1.0
    if prev_to is not None and move.from_square == prev_to:
        f[i["same_piece"]] = 1.0
    if piece.piece_type == chess.KING and not board.is_castling(move) and phase != "endgame":
        f[i["king_walk"]] = 1.0
    return f


def candidate_set(board: chess.Board, engine, limit: chess.engine.Limit, rng: random.Random,
                  must_include: str | None = None) -> list[tuple[str, int]]:
    """Engine's top moves + a couple of random legal ones (+ `must_include`), each with a comparable score."""
    top = engine.candidates(board, TOP_N, limit)
    have = {u for u, _ in top}
    others = [m.uci() for m in board.legal_moves if m.uci() not in have]
    extra = rng.sample(others, min(EXTRA_RANDOM, len(others)))
    if must_include and must_include not in have and must_include not in extra:
        extra.append(must_include)
    if extra:
        top += engine.candidates(board, 0, limit, moves=extra)
    return top


def decision_rows(board: chess.Board, cands: list[tuple[str, int]], pressure: bool, prev_to: int | None,
                  opp_to: int | None = None) -> tuple[list[str], list[list[float]], list[float]]:
    ranked = sorted(cands, key=lambda x: -x[1])
    rank = {u: r for r, (u, _) in enumerate(ranked)}
    best_es = es(ranked[0][1])
    ucis, rows, losses = [], [], []
    for u, cp in cands:
        loss = max(0.0, best_es - es(cp))
        ucis.append(u)
        losses.append(loss)
        rows.append(features(board, chess.Move.from_uci(u), loss, rank[u], pressure, prev_to, opp_to))
    return ucis, rows, losses


_PAIRS: dict[tuple[int, float], tuple[int, float]] = {}


def sparse(row: list[float]) -> tuple:
    """A feature row as ((index, value), ...) for its non-zero features. Values are rounded and the pairs shared,
    so tens of thousands of stored decisions fit in a few tens of megabytes."""
    out = []
    for k, v in enumerate(row):
        if v:
            v = round(v, 3)
            if v:
                key = (k, v)
                out.append(_PAIRS.setdefault(key, key))
    return tuple(out)


# ---- the choice model --------------------------------------------------------------------------------
def prior(rating: float | None) -> list[float]:
    """Sensible weights before seeing any games: stronger players lose less per move."""
    r = rating or 1500
    q = 0.8 + max(0.0, r - 800) / 500  # 1500 -> 2.2, 2200 -> 3.6
    b = [0.0] * NF
    b[0], b[1], b[2], b[3] = q * 1.2, q, q * 0.9, 0.0
    b[FEATURES.index("best")] = 0.3
    b[FEATURES.index("recapture")] = 1.0  # almost everyone takes back
    return b


def probabilities(beta: list[float], rows, loss_scale: float = 1.0) -> list[float]:
    """Choice probabilities for sparse feature rows (see sparse())."""
    w = [beta[k] * (loss_scale if k in LOSS else 1.0) for k in range(NF)]
    u = [sum(w[k] * v for k, v in row) for row in rows]
    m = max(u)
    ex = [math.exp(x - m) for x in u]
    z = sum(ex)
    return [x / z for x in ex]


def fit(decisions: list[tuple], beta0: list[float], lam: float = 2.0, iters: int = 15, with_se: bool = False):
    """Maximum-likelihood conditional logit with a Gaussian prior around beta0 (Newton's method with a line search).

    `decisions` are (sparse rows, chosen index) or (sparse rows, chosen index, weight): weights let recent games
    count more. They are normalised to average 1, so the prior keeps the same pull. Uses H = sum_c p_c f_c f_c^T
    - m m^T on sparse rows, so a thousand decisions fit in well under a second.
    """
    ws = [d[2] if len(d) > 2 else 1.0 for d in decisions]
    mean_w = (sum(ws) / len(ws)) if ws else 1.0
    sparse = [(d[0], d[1], w / mean_w) for d, w in zip(decisions, ws, strict=True)]
    beta = list(beta0)
    h: list[list[float]] = []
    for _ in range(iters):
        g = [-lam * (beta[k] - beta0[k]) for k in range(NF)]
        h = [[(lam if i == j else 0.0) for j in range(NF)] for i in range(NF)]  # negative Hessian
        for rows, chosen, wt in sparse:
            u = [sum(beta[k] * v for k, v in nz) for nz in rows]
            mx = max(u)
            ex = [math.exp(x - mx) for x in u]
            z = sum(ex)
            mean = [0.0] * NF
            for nz, e in zip(rows, ex, strict=True):
                pc = e / z
                if pc < 1e-9:
                    continue
                for k, v in nz:
                    mean[k] += pc * v
                pw = pc * wt
                for i, vi in nz:
                    hi = h[i]
                    pvi = pw * vi
                    for j, vj in nz:
                        hi[j] += pvi * vj
            for k, v in rows[chosen]:
                g[k] += wt * v
            for k in range(NF):
                mk = mean[k]
                if mk:
                    g[k] -= wt * mk
                    hk = h[k]
                    wmk = wt * mk
                    for j in range(NF):
                        if mean[j]:
                            hk[j] -= wmk * mean[j]
        step = _solve(h, g)
        # Backtracking line search: a full Newton step can overshoot when a few moves lose a lot.
        current = _objective(sparse, beta, beta0, lam)
        t = 1.0
        while t > 1e-3:
            trial = [beta[k] + t * step[k] for k in range(NF)]
            if _objective(sparse, trial, beta0, lam) >= current:
                break
            t /= 2
        else:
            break
        beta = trial
        if max(abs(t * x) for x in step) < 1e-4:
            break
    if not with_se:
        return beta
    # Standard errors from the curvature (the inverse of the negative Hessian), so the profile only reports
    # habits the data really supports.
    se = []
    for k in range(NF):
        e = [1.0 if j == k else 0.0 for j in range(NF)]
        se.append(math.sqrt(max(0.0, _solve(h, e)[k])) if h else 1.0)
    return beta, se


def _objective(sparse, beta: list[float], beta0: list[float], lam: float) -> float:
    total = -0.5 * lam * sum((beta[k] - beta0[k]) ** 2 for k in range(NF))
    for rows, chosen, wt in sparse:
        u = [sum(beta[k] * v for k, v in nz) for nz in rows]
        mx = max(u)
        total += wt * (u[chosen] - mx - math.log(sum(math.exp(x - mx) for x in u)))
    return total


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (a is positive definite here)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-12:
            continue
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r] if abs(m[r][r]) > 1e-12 else 0.0
    return x


# ---- the opening book from their own games ---------------------------------------------------------------
def _age_weight(played_at: str, today: dt.date) -> float:
    try:
        d = dt.date(*map(int, played_at[:10].split(".")))
    except (ValueError, TypeError):
        return 0.5
    return 0.5 ** (max(0, (today - d).days) / BOOK_HALF_LIFE_DAYS)


def book(store, player_id: int, board: chess.Board, classes: list[str] | None = None, exclude: set[str] | None = None) -> list[dict]:
    """What this player chose in this exact position (any move order), most-played first."""
    color = "white" if board.turn == chess.WHITE else "black"
    sql = ("SELECT pos.next_uci, g.game_id, g.played_at, g.score FROM lib_positions pos JOIN lib_games g ON g.id = pos.game_ref "
           "WHERE pos.hash = ? AND g.player_id = ? AND g.color = ? AND pos.next_uci IS NOT NULL")
    args: list = [position_key(board), player_id, color]
    wanted = [c for c in (classes or []) if c in TIME_CLASSES]
    if wanted and len(wanted) < len(TIME_CLASSES):
        sql += f" AND g.time_class IN ({','.join('?' * len(wanted))})"
        args += wanted
    with store._conn() as c:
        rows = c.execute(sql, args).fetchall()
    today = dt.date.today()
    agg: dict[str, dict] = {}
    for r in rows:
        if exclude and r["game_id"] in exclude:
            continue
        a = agg.setdefault(r["next_uci"], {"uci": r["next_uci"], "games": set(), "weight": 0.0, "last": "", "score": []})
        if r["game_id"] in a["games"]:
            continue
        a["games"].add(r["game_id"])
        a["weight"] += _age_weight(r["played_at"], today)
        a["last"] = max(a["last"], r["played_at"])
        a["score"].append(r["score"])
    out = []
    for a in agg.values():
        try:
            mv = chess.Move.from_uci(a["uci"])
            if mv not in board.legal_moves:
                continue
            san = board.san(mv)
        except ValueError:
            continue
        out.append({"uci": a["uci"], "san": san, "games": len(a["games"]), "weight": a["weight"], "last": a["last"],
                    "score_pct": round(100 * sum(a["score"]) / len(a["score"]))})
    out.sort(key=lambda x: (-x["weight"], -x["games"]))
    return out


def book_trust(entries: list[dict]) -> float:
    """How often to follow the book here: n / (n + 1.5) for n weighted games."""
    n = sum(e["weight"] for e in entries)
    return n / (n + 1.5)


# ---- building a Twin --------------------------------------------------------------------------------------
def collect_decisions(records: list[GameRecord], engine, limit: chess.engine.Limit, rng: random.Random,
                      progress=None) -> list[dict]:
    """Replay their games; at each of their moves record the candidates, their features and what they played."""
    out = []
    for gi, rec in enumerate(records):
        board = chess.Board()
        their_turns = [i for i in range(len(rec.moves)) if (i % 2 == 0) == (rec.player_color == chess.WHITE)]
        their_turns = their_turns[SKIP_FIRST:]
        if len(their_turns) > MAX_DECISIONS_PER_GAME:  # long games: spread the sample over the whole game
            step = len(their_turns) / MAX_DECISIONS_PER_GAME
            their_turns = [their_turns[int(i * step)] for i in range(MAX_DECISIONS_PER_GAME)]
        wanted = set(their_turns)
        prev_to: int | None = None
        opp_to: int | None = None
        for ply, uci in enumerate(rec.moves):
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                break
            if move not in board.legal_moves:
                break
            mover_is_them = (ply % 2 == 0) == (rec.player_color == chess.WHITE)
            if mover_is_them and ply in wanted and board.legal_moves.count() > 1:
                clock = rec.clocks[ply - 2] if ply >= 2 and len(rec.clocks) > ply - 2 else None
                pressure = _time_pressure(clock, rec.initial_seconds) if clock is not None else False
                cands = candidate_set(board, engine, limit, rng, must_include=uci)
                if cands:
                    ucis, rows, losses = decision_rows(board, cands, pressure, prev_to, opp_to)
                    if uci in ucis:
                        chosen = ucis.index(uci)
                        best_cp = max(cp for _, cp in cands)
                        out.append({"game": gi, "rows": [sparse(r) for r in rows], "chosen": chosen, "loss": losses[chosen], "phase": game_phase(board),
                                    "pressure": pressure, "engine_top": ucis.index(max(cands, key=lambda x: x[1])[0]),
                                    "best_es": es(best_cp), "played_es": es(dict(cands)[uci]), "ply": ply})
            if mover_is_them:
                prev_to = move.to_square
            else:
                opp_to = move.to_square
            board.push(move)
        if progress:
            progress(gi + 1, len(records))
    return out


def evaluate(beta: list[float], test: list[dict]) -> dict:
    """On games the model never saw: how often its first choice (and its top three) is the move they really
    played, next to Stockfish's first choice (and top three)."""
    if not test:
        return {"twin_top1": None, "engine_top1": None, "twin_top3": None, "engine_top3": None, "decisions": 0}
    t1 = t3 = e1 = e3 = 0
    top3 = FEATURES.index("top3")
    for d in test:
        p = probabilities(beta, d["rows"])
        order = sorted(range(len(p)), key=lambda k: -p[k])
        t1 += order[0] == d["chosen"]
        t3 += d["chosen"] in order[:3]
        e1 += d["engine_top"] == d["chosen"]
        e3 += any(k == top3 for k, _ in d["rows"][d["chosen"]])
    n = len(test)
    return {"twin_top1": round(100 * t1 / n), "engine_top1": round(100 * e1 / n), "twin_top3": round(100 * t3 / n),
            "engine_top3": round(100 * e3 / n), "decisions": n}


def book_coverage(store, player_id: int, test_records: list[GameRecord], classes, held_out_ids: set[str]) -> int | None:
    """Of their first 10 moves in held-out games, how often the book (built from the other games) predicted them."""
    hits = total = 0
    for rec in test_records:
        board = chess.Board()
        for ply, uci in enumerate(rec.moves[:20]):
            if (ply % 2 == 0) == (rec.player_color == chess.WHITE):
                entries = book(store, player_id, board, classes, exclude=held_out_ids)
                total += 1
                if entries and entries[0]["uci"] == uci:
                    hits += 1
            try:
                board.push(chess.Move.from_uci(uci))
            except ValueError:
                break
    return round(100 * hits / total) if total else None


# ---- studying games (each game once, ever) -----------------------------------------------------------------
# A player's history can run to thousands of games, and the engine is the expensive part (about a second per
# game). So every game is studied once and the result is kept (table twin_games). Each build studies the next
# batch, and a rebuild only fits the model again, which takes seconds.
PHASE_CODE = {"opening": 0, "middlegame": 1, "endgame": 2}
PHASE_NAME = {v: k for k, v in PHASE_CODE.items()}
MODEL_HALF_LIFE_DAYS = 730  # in the model, a game two years older counts half as much (players change)
MAX_FIT_DECISIONS = 24_000  # the model is fitted on at most this many of the most recent decisions
STUDY_CHUNK = 10  # games per job turn, so a long study shares the workers fairly


def pack(decisions: list[dict]) -> bytes:
    import json
    import zlib

    rows = [[[list(p) for p in r] for r in d["rows"]] for d in decisions]
    meta = [[d["chosen"], round(d["loss"], 4), PHASE_CODE[d["phase"]], int(d["pressure"]), d["engine_top"],
             round(d["best_es"], 4), round(d["played_es"], 4), d["ply"]] for d in decisions]
    return zlib.compress(json.dumps([rows, meta], separators=(",", ":")).encode(), 6)


def unpack(blob: bytes) -> list[dict]:
    import json
    import zlib

    rows, meta = json.loads(zlib.decompress(blob))
    out = []
    for r, m in zip(rows, meta, strict=True):
        srows = []
        for row in r:
            pairs = []
            for k, v in row:
                key = (k, v)
                pairs.append(_PAIRS.setdefault(key, key))
            srows.append(tuple(pairs))
        out.append({"rows": srows, "chosen": m[0], "loss": m[1], "phase": PHASE_NAME[m[2]], "pressure": bool(m[3]),
                    "engine_top": m[4], "best_es": m[5], "played_es": m[6], "ply": m[7]})
    return out


def _tc_sql(classes) -> tuple[str, list]:
    wanted = [c for c in (classes or []) if c in TIME_CLASSES]
    if wanted and len(wanted) < len(TIME_CLASSES):
        return f" AND g.time_class IN ({','.join('?' * len(wanted))})", wanted
    return "", []


def study_counts(store, player_id: int, classes, depth: int) -> tuple[int, int]:
    """(games studied, games available) for this player and these time controls."""
    extra, args = _tc_sql(classes)
    with store._conn() as c:
        available = c.execute(f"SELECT COUNT(*) FROM lib_games g WHERE g.player_id = ?{extra}", [player_id, *args]).fetchone()[0]
        studied = c.execute(f"SELECT COUNT(*) FROM twin_games t JOIN lib_games g ON g.id = t.game_ref WHERE t.player_id = ? "
                            f"AND t.version = ? AND t.depth = ?{extra}", [player_id, VERSION, depth, *args]).fetchone()[0]
    return studied, available


def pick_unstudied(store, player_id: int, classes, budget: int, depth: int) -> list[int]:
    """Which games to study next: two thirds the newest not yet studied, one third spread across the rest of
    their history (so even the first batch sees how they played over the years)."""
    if budget <= 0:
        return []
    extra, args = _tc_sql(classes)
    with store._conn() as c:
        rows = [r[0] for r in c.execute(f"SELECT g.id FROM lib_games g WHERE g.player_id = ?{extra} "
                                        "ORDER BY g.played_at DESC, g.id DESC", [player_id, *args])]
        done = {r[0] for r in c.execute("SELECT game_ref FROM twin_games WHERE player_id = ? AND version = ? AND depth = ?",
                                        (player_id, VERSION, depth))}
    todo = [r for r in rows if r not in done]
    newest = todo[: max(1, (2 * budget) // 3)]
    rest = todo[len(newest):]
    k = budget - len(newest)
    step = len(rest) / k if k > 0 and len(rest) > k else 1.0
    spread = [rest[int(i * step)] for i in range(min(k, len(rest)))]
    return newest + spread


def study_games(store, library, player_id: int, game_refs: list[int], engine, depth: int, rng: random.Random,
                on_game=None) -> int:
    """Study these games (each once, ever) and keep the results. Returns how many decisions were recorded."""
    limit = chess.engine.Limit(depth=depth, time=0.5)
    n = 0
    for i, ref in enumerate(game_refs, 1):
        found = library.game(ref)
        if found:
            decisions = collect_decisions([found[1]], engine, limit, rng)
            with store._conn() as c:
                c.execute("INSERT OR REPLACE INTO twin_games (game_ref, player_id, version, depth, n, data, studied_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)", (ref, player_id, VERSION, depth, len(decisions), pack(decisions), time.time()))
            n += len(decisions)
        if on_game:
            on_game(i)
    return n


def _recency(played_at: str, newest: dt.date) -> float:
    try:
        d = dt.date(*map(int, played_at[:10].split(".")))
    except (ValueError, TypeError):
        return 0.5
    return 0.5 ** (max(0, (newest - d).days) / MODEL_HALF_LIFE_DAYS)


def fit_twin(store, library, player_id: int, classes, depth: int, *, seed: int = 7, progress=None) -> dict:
    """Fit the Twin on everything studied so far (most recent first, weighted by recency) and save it.
    Also measures faithfulness on held-out games, and a learning curve: faithfulness as more games are used."""
    wanted = [c for c in (classes or []) if c in TIME_CLASSES]
    extra, args = _tc_sql(wanted)
    with store._conn() as c:
        rows = c.execute(f"SELECT t.game_ref, t.data, g.played_at, g.game_id, g.data AS rec FROM twin_games t JOIN lib_games g "
                         f"ON g.id = t.game_ref WHERE t.player_id = ? AND t.version = ? AND t.depth = ?{extra} "
                         "ORDER BY g.played_at DESC, g.id DESC", [player_id, VERSION, depth, *args]).fetchall()
    games, total = [], 0
    for r in rows:
        decs = unpack(r["data"])
        if not decs:
            continue
        games.append({"ref": r["game_ref"], "played_at": r["played_at"], "game_id": r["game_id"], "decisions": decs, "rec": r["rec"]})
        total += len(decs)
        if total >= MAX_FIT_DECISIONS:
            break
    if len(games) < MIN_GAMES:
        raise ValueError(f"The Twin needs at least {MIN_GAMES} studied games; there are {len(games)}.")
    if total < 30:
        raise ValueError("Not enough of their own moves to learn from (fewer than 30). Add more games.")
    if progress:
        progress(stage="Fitting their decision model")
    try:
        newest = dt.date(*map(int, games[0]["played_at"][:10].split(".")))
    except (ValueError, TypeError):
        newest = dt.date.today()
    for i, g in enumerate(games):
        g["weight"] = _recency(g["played_at"], newest)
        for d in g["decisions"]:
            d["game"] = i
    records = [record_from_json(g["rec"]) for g in games[:300]]  # rating, castling and the book check need the games
    ratings = sorted(r.player_rating for r in records if r.player_rating)
    rating = ratings[len(ratings) // 2] if ratings else None
    beta0 = prior(rating)
    # Faithfulness on held-out games (every fifth game), and the learning curve on the same test set.
    test_idx = {i for i in range(len(games)) if i % 5 == 4}
    test = [d for i in sorted(test_idx) for d in games[i]["decisions"]]
    train_games = [i for i in range(len(games)) if i not in test_idx]
    rng = random.Random(seed)
    order = train_games[:]
    rng.shuffle(order)
    sizes = [n for n in (10, 25, 50, 100, 200, 400, 800) if n < len(order)] + [len(order)]
    curve = []
    beta_train = beta0
    for n in sizes:
        subset = order[:n]
        data = [(d["rows"], d["chosen"], games[i]["weight"]) for i in subset for d in games[i]["decisions"]]
        beta_n = fit(data, beta0)
        ev = evaluate(beta_n, test)
        curve.append({"games": n, "twin_top3": ev["twin_top3"], "twin_top1": ev["twin_top1"]})
        beta_train = beta_n
    faith = evaluate(beta_train, test)
    held_out = [records[i] for i in sorted(test_idx) if i < len(records)][:40]
    faith["book_top1"] = book_coverage(store, player_id, held_out, wanted, {g.game_id for g in held_out})
    all_data = [(d["rows"], d["chosen"], g["weight"]) for g in games for d in g["decisions"]]
    beta, se = fit(all_data, beta0, with_se=True)
    decisions = [d for g in games for d in g["decisions"]]
    prof = profile(decisions, beta, beta0, records, store, player_id, wanted, se)
    studied, available = study_counts(store, player_id, wanted, depth)
    prof.update(faithfulness=faith, rating=rating, learning_curve=curve, studied=studied, available=available,
                used_games=len(games), oldest_game=games[-1]["played_at"], half_life_days=MODEL_HALF_LIFE_DAYS)
    return save(store, player_id, tc_key(wanted), beta, prof, len(games), len(decisions),
                newest=max((g["played_at"] for g in games), default=""))


def build(store, library, player_id: int, classes: list[str] | None, engine, *, max_games: int = 40, depth: int = 9,
          seed: int = 7, progress=None) -> dict:
    """Study up to `max_games` more of their games, then fit and save the Twin (all in one go: scripts and tests;
    the app does the same in chunks through the job queue)."""
    wanted = [c for c in (classes or []) if c in TIME_CLASSES]
    _, available = study_counts(store, player_id, wanted, depth)
    if available < MIN_GAMES:
        raise ValueError(f"The Twin needs at least {MIN_GAMES} games; there are {available} for these time controls.")
    refs = pick_unstudied(store, player_id, wanted, max_games, depth)
    rng = random.Random(seed)
    study_games(store, library, player_id, refs, engine, depth, rng,
                on_game=(lambda i: progress(stage="Studying their decisions", done=i, total=len(refs))) if progress else None)
    return fit_twin(store, library, player_id, wanted, depth, seed=seed, progress=progress)


def profile(decisions: list[dict], beta: list[float], beta0: list[float], records: list[GameRecord], store, player_id: int,
            classes, se: list[float] | None = None) -> dict:
    by_phase: dict[str, list[float]] = defaultdict(list)
    blunders = mistakes = 0
    pressure_acc, calm_acc = [], []
    for d in decisions:
        acc = move_accuracy(100 * d["best_es"], 100 * d["played_es"])
        by_phase[d["phase"]].append(acc)
        (pressure_acc if d["pressure"] else calm_acc).append(acc)
        c = classify(100 * d["loss"])
        blunders += c == "blunder"
        mistakes += c == "mistake"
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    style = []
    for k, name in enumerate(FEATURES):
        if k in LOSS or name == "best":
            continue
        delta = beta[k] - beta0[k]
        sure = se is None or abs(delta) >= 3 * se[k]  # only habits the data clearly supports
        if abs(delta) >= 0.5 and sure:
            text = STYLE_TEXT.get((name, 1 if delta > 0 else -1))
            if text:
                style.append({"feature": name, "text": text, "strength": round(min(1.0, abs(delta) / 1.5), 2)})
    style.sort(key=lambda s: -s["strength"])
    # Castling habits straight from the games
    castle_moves, sides = [], {"short": 0, "long": 0}
    for rec in records:
        b = chess.Board()
        for ply, u in enumerate(rec.moves[:60]):
            mv = chess.Move.from_uci(u)
            if (ply % 2 == 0) == (rec.player_color == chess.WHITE) and b.is_castling(mv):
                castle_moves.append(ply // 2 + 1)
                sides["short" if chess.square_file(mv.to_square) == 6 else "long"] += 1
                break
            b.push(mv)
    openings = {}
    for color, first in (("white", chess.Board()), ("black_vs_e4", _after("e2e4")), ("black_vs_d4", _after("d2d4"))):
        entries = book(store, player_id, first, classes)
        total = sum(e["games"] for e in entries)
        if total:
            openings[color] = [{"san": e["san"], "pct": round(100 * e["games"] / total), "games": e["games"]} for e in entries[:3]]
    n = len(decisions)
    return {
        "accuracy": {p: mean(by_phase.get(p, [])) for p in ("opening", "middlegame", "endgame")},
        "accuracy_overall": mean([a for xs in by_phase.values() for a in xs]),
        "blunders_per_100": round(100 * blunders / n, 1) if n else None,
        "mistakes_per_100": round(100 * mistakes / n, 1) if n else None,
        "time_trouble": {"accuracy": mean(pressure_acc), "calm_accuracy": mean(calm_acc), "moves": len(pressure_acc)},
        "style": style[:5],
        "castling": {"usual_move": sorted(castle_moves)[len(castle_moves) // 2] if castle_moves else None,
                     "short": sides["short"], "long": sides["long"], "games": len(records)},
        "openings": openings,
        "confidence": "high" if n >= 600 else "medium" if n >= 250 else "low",
    }


def _after(uci: str) -> chess.Board:
    b = chess.Board()
    b.push_uci(uci)
    return b


# ---- storage ----------------------------------------------------------------------------------------------
def save(store, player_id: int, key: str, beta: list[float], prof: dict, games: int, decisions: int, newest: str) -> dict:
    import json

    with store._conn() as c:
        c.execute("INSERT INTO twins (player_id, tc_key, version, built_at, games, decisions, newest_game, params, profile) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (player_id, tc_key) DO UPDATE SET version = excluded.version, "
                  "built_at = excluded.built_at, games = excluded.games, decisions = excluded.decisions, "
                  "newest_game = excluded.newest_game, params = excluded.params, profile = excluded.profile",
                  (player_id, key, VERSION, time.time(), games, decisions, newest, json.dumps(beta), json.dumps(prof)))
    return load(store, player_id, key)


def load(store, player_id: int, key: str) -> dict | None:
    import json

    with store._conn() as c:
        row = c.execute("SELECT * FROM twins WHERE player_id = ? AND tc_key = ?", (player_id, key)).fetchone()
    if not row or row["version"] != VERSION:
        return None
    t = dict(row)
    t["params"] = json.loads(t["params"])
    t["profile"] = json.loads(t["profile"])
    return t


# ---- playing ----------------------------------------------------------------------------------------------
def choose(store, twin: dict, board: chess.Board, engine, *, classes=None, mode: str = "faithful", main_line: bool = False,
           prev_to: int | None = None, opp_to: int | None = None, rng: random.Random | None = None, depth: int = 9) -> dict:
    """The Twin's move in this position, with the reason and the alternatives it considered."""
    rng = rng or random.Random()
    legal = list(board.legal_moves)
    if len(legal) == 1:
        mv = legal[0]
        return {"uci": mv.uci(), "san": board.san(mv), "source": "forced", "why": "The only legal move.", "alternatives": []}
    entries = book(store, twin["player_id"], board, classes)
    if entries and (main_line or rng.random() < book_trust(entries)):
        total_w = sum(e["weight"] for e in entries)
        # Rehearsal mode always plays their most common move; otherwise sample like they do.
        pick = entries[0] if main_line else _weighted(rng, entries, [e["weight"] for e in entries])
        games = sum(e["games"] for e in entries)
        recent = f" (most recently {pick['last'].replace('.', '-')})" if pick["last"] else ""
        why = f"From their games: they played {pick['san']} here in {pick['games']} of {games} game{'s' if games != 1 else ''}{recent}."
        alts = [{"san": e["san"], "pct": round(100 * e["weight"] / total_w), "games": e["games"]} for e in entries[:4]]
        return {"uci": pick["uci"], "san": pick["san"], "source": "book", "why": why, "alternatives": alts}
    limit = chess.engine.Limit(depth=depth, time=0.5)
    cands = candidate_set(board, engine, limit, rng)
    if not cands:
        mv = rng.choice(legal)
        return {"uci": mv.uci(), "san": board.san(mv), "source": "model", "why": "", "alternatives": []}
    ucis, rows, losses = decision_rows(board, cands, False, prev_to, opp_to)
    p = probabilities(twin["params"], [sparse(r) for r in rows], GOOD_DAY if mode == "good_day" else 1.0)
    i = _weighted(rng, list(range(len(ucis))), p)
    mv = chess.Move.from_uci(ucis[i])
    best = board.san(chess.Move.from_uci(max(cands, key=lambda x: x[1])[0]))
    san = board.san(mv)
    left = " They've never reached this position before," if not entries else " Their games split here, and the model chose."
    why = (f"{left.strip()} so the Twin predicts what they'd play: {san} ({round(100 * p[i])}% likely for them)."
           + ("" if san == best else f" The engine's best was {best}."))
    order = sorted(range(len(ucis)), key=lambda k: -p[k])[:4]
    alts = [{"san": board.san(chess.Move.from_uci(ucis[k])), "pct": round(100 * p[k])} for k in order]
    return {"uci": mv.uci(), "san": san, "source": "model", "why": why, "alternatives": alts, "loss": round(100 * losses[i], 1)}


def _weighted(rng: random.Random, items: list, weights: list[float]):
    total = sum(weights)
    if total <= 0:
        return rng.choice(items)
    x = rng.random() * total
    for it, w in zip(items, weights, strict=False):
        x -= w
        if x <= 0:
            return it
    return items[-1]
