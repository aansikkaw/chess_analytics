"""Game review: a report card for one game, both players, every move checked by Stockfish.

For each position the engine finds its two best moves, so every move gets:
  * how much it lost in expected score (win %), from the mover's side, and a Lichess-style accuracy;
  * a class: Brilliant (a sound piece sacrifice that's also the best move), Great (the only good move, found),
    Best, Excellent, Good, Book (still in a named opening), Inaccuracy, Mistake, Miss (didn't punish the
    opponent's mistake), Blunder, Forced (the only legal move);
  * the phase it was played in.
Then, per player: accuracy, average centipawn loss, counts by class and a grade for each phase (A-F by
accuracy). The key moments are the biggest swings, each with the engine's line and a plain-language reason
(hanging piece, missed mate, missed tactic, let the win slip...). The opening is named from Lichess's open
list (app/openings.py), and the review exports an annotated PGN (NAGs, [%eval] and comments).
"""

from __future__ import annotations

import io
import json
import time

import chess
import chess.engine
import chess.pgn

from . import openings
from .analysis import PIECE_VALUE, game_phase, move_accuracy, win_percent
from .engine import MATE_CP

VERSION = 1
MATE_THRESHOLD = MATE_CP - 500
CLASS_INFO = {  # name -> (symbol, label, NAG). Standard symbols, our own words (DESIGN.md: no other site's move tiers).
    "brilliant": ("!!", "Sound sacrifice", chess.pgn.NAG_BRILLIANT_MOVE),
    "great": ("!", "Only good move", chess.pgn.NAG_GOOD_MOVE),
    "best": ("", "Engine's choice", None),
    "excellent": ("", "Good", None),
    "good": ("", "Good", None),
    "book": ("", "Book move", None),
    "forced": ("□", "Only move", 7),  # $7: only move
    "inaccuracy": ("?!", "Inaccuracy", chess.pgn.NAG_DUBIOUS_MOVE),
    "mistake": ("?", "Mistake", chess.pgn.NAG_MISTAKE),
    "miss": ("?", "Didn't punish", chess.pgn.NAG_MISTAKE),
    "blunder": ("??", "Blunder", chess.pgn.NAG_BLUNDER),
}
CLASS_ORDER = list(CLASS_INFO)
GRADES = ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F"))
TAG_TEXT = {
    "allowed_mate": "it allowed a forced mate",
    "missed_mate": "there was a forced mate",
    "hanging_piece": "it left a piece to be taken",
    "missed_tactic": "a forcing move (a check, capture or threat) won more",
    "let_slip": "it let a winning position slip",
    "missed_punish": "the opponent's previous move was a mistake, and this didn't punish it",
    "time_trouble": "it was played in time trouble",
}


def grade(acc: float | None) -> str | None:
    if acc is None:
        return None
    return next(g for lim, g in GRADES if acc >= lim)


def _win(cp: int) -> float:
    return win_percent(cp)


# ---- the engine pass ----------------------------------------------------------------------------------------
def evaluate_positions(moves: list[str], engine, depth: int, progress=None) -> list[dict]:
    """Every position of the game (before each move, and the final one): the engine's two best moves and scores,
    from the side to move's point of view."""
    board = chess.Board()
    limit = chess.engine.Limit(depth=depth, time=0.6)
    out = []
    for i in range(len(moves) + 1):
        if board.is_checkmate():
            out.append({"cp": -MATE_CP, "best": None, "second": None, "legal": 0})
        elif board.is_game_over(claim_draw=False):
            out.append({"cp": 0, "best": None, "second": None, "legal": board.legal_moves.count()})
        else:
            cands = engine.candidates(board, 2, limit)
            out.append({"cp": cands[0][1], "best": cands[0][0], "second": cands[1][1] if len(cands) > 1 else None,
                        "legal": board.legal_moves.count()})
        if progress:
            progress(i + 1, len(moves) + 1)
        if i < len(moves):
            board.push(chess.Move.from_uci(moves[i]))
    return out


def is_sacrifice(board: chess.Board, move: chess.Move) -> bool:
    """A piece (not a pawn) put where a cheaper piece can take it, or where it can be taken and isn't defended,
    giving up at least two points more than it captures. Only legal captures count (a king can't take a defended
    piece, a pinned piece can't take at all)."""
    piece = board.piece_at(move.from_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    victim = board.piece_at(move.to_square)
    gained = PIECE_VALUE[victim.piece_type] if victim else 0
    after = board.copy(stack=False)
    after.push(move)
    takers = [m.from_square for m in after.legal_moves if m.to_square == move.to_square]
    if not takers:
        return False
    cheaper = any(PIECE_VALUE[after.piece_type_at(a)] < PIECE_VALUE[piece.piece_type] for a in takers)
    defended = after.is_attacked_by(board.turn, move.to_square)
    return (cheaper or not defended) and PIECE_VALUE[piece.piece_type] - gained >= 2


def classify_moves(moves: list[str], evals: list[dict], clocks: dict[int, float] | None = None, base: int | None = None) -> list[dict]:
    board = chess.Board()
    out = []
    prev_loss = 0.0
    for i, uci in enumerate(moves):
        mv = chess.Move.from_uci(uci)
        e0, e1 = evals[i], evals[i + 1]
        mover = board.turn
        wb = _win(e0["cp"])
        wa = 100 - _win(e1["cp"])
        loss = max(0.0, wb - wa)
        cpb = max(-1000, min(1000, e0["cp"]))
        cpa = max(-1000, min(1000, -e1["cp"]))
        gap = (wb - _win(e0["second"])) if e0["second"] is not None else 100.0
        san = board.san(mv)
        best_san = board.san(chess.Move.from_uci(e0["best"])) if e0["best"] else None
        played_best = e0["best"] == uci
        phase = game_phase(board)
        after = board.copy(stack=False)
        after.push(mv)
        allowed_mate = e1["cp"] >= MATE_THRESHOLD and e0["cp"] > -MATE_THRESHOLD  # there was a way to avoid being mated
        missed_mate = e0["cp"] >= MATE_THRESHOLD and -e1["cp"] < MATE_THRESHOLD
        if e0["legal"] == 1:
            cls = "forced"
        elif allowed_mate:  # walking into a forced mate is always a blunder, even in a lost position (or "in theory")
            cls = "blunder"
        elif i < 30 and loss < 5 and openings.in_book(after) and not after.is_checkmate():
            cls = "book"
        elif loss >= 30:
            cls = "blunder"
        elif missed_mate and wa < 97:
            cls = "mistake"
        elif prev_loss >= 15 and loss >= 10:
            cls = "miss"
        elif loss >= 20:
            cls = "mistake"
        elif loss >= 10:
            cls = "inaccuracy"
        elif played_best or loss <= 0.5:
            last = board.peek() if board.move_stack else None
            recapture = bool(last and last.to_square == mv.to_square and board.is_capture(mv))
            if played_best and is_sacrifice(board, mv) and wb < 95 and wa >= 45:
                cls = "brilliant"
            elif played_best and gap >= 15 and not recapture and not (board.is_check() and e0["legal"] <= 2) and not after.is_checkmate():
                cls = "great"
            else:
                cls = "best"
        elif loss < 2:
            cls = "excellent"
        else:
            cls = "good"
        tags = []
        if cls in ("inaccuracy", "mistake", "miss", "blunder"):
            if allowed_mate:
                tags.append("allowed_mate")
            if missed_mate:
                tags.append("missed_mate")
            if e1["best"]:
                reply = chess.Move.from_uci(e1["best"])
                victim = after.piece_at(reply.to_square)
                if victim is not None and victim.color == mover and PIECE_VALUE[victim.piece_type] >= 3:
                    tags.append("hanging_piece")
            if e0["best"]:
                b = chess.Move.from_uci(e0["best"])
                if (board.is_capture(b) or board.gives_check(b) or b.promotion) and not (board.is_capture(mv) or board.gives_check(mv)):
                    tags.append("missed_tactic")
            if wb >= 70 and wa < 55:
                tags.append("let_slip")
            if cls == "miss":
                tags.append("missed_punish")
            clock = (clocks or {}).get(i)
            if clock is not None and base and clock < max(30.0, 0.1 * base):
                tags.append("time_trouble")
        out.append({"ply": i, "move_number": board.fullmove_number, "color": "white" if mover == chess.WHITE else "black",
                    "san": san, "uci": uci, "best": e0["best"], "best_san": best_san, "class": cls, "symbol": CLASS_INFO[cls][0],
                    "loss": round(loss, 1), "accuracy": round(move_accuracy(wb, wa), 1), "cp_loss": max(0, cpb - cpa),
                    "win_before": round(wb, 1), "win_after": round(wa, 1), "gap": round(gap, 1), "phase": phase,
                    "eval_white": e1["cp"] if after.turn == chess.WHITE else -e1["cp"],
                    "fen": board.fen(), "tags": tags, "clock": (clocks or {}).get(i)})
        prev_loss = loss
        board.push(mv)
    return out


def side_summary(rows: list[dict], color: str) -> dict:
    mine = [r for r in rows if r["color"] == color]
    scored = [r for r in mine if r["class"] != "forced"]
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    counts = {c: sum(r["class"] == c for r in mine) for c in CLASS_ORDER}
    phases = {}
    for p in ("opening", "middlegame", "endgame"):
        ps = [r for r in scored if r["phase"] == p]
        acc = mean([r["accuracy"] for r in ps])
        phases[p] = {"moves": len(ps), "accuracy": acc, "grade": grade(acc) if len(ps) >= 3 else None}
    acc = mean([r["accuracy"] for r in scored])
    return {"accuracy": acc, "grade": grade(acc), "acpl": round(sum(r["cp_loss"] for r in scored) / len(scored)) if scored else None,
            "moves": len(mine), "counts": counts, "phases": phases}


def key_moments(rows: list[dict], engine, depth: int, limit: int = 6) -> list[dict]:
    """The biggest swings of the game, with the engine's line and the punishing reply."""
    bad = sorted((r for r in rows if r["class"] in ("blunder", "mistake", "miss", "inaccuracy")), key=lambda r: -r["loss"])[:limit]
    out = []
    for r in sorted(bad, key=lambda r: r["ply"]):
        board = chess.Board(r["fen"])
        line = []
        if engine is not None and not board.is_game_over():
            ev = engine.evaluate(board, chess.engine.Limit(depth=depth + 2, time=1.0))
            b = board.copy()
            for u in ev.pv[:6]:
                mv = chess.Move.from_uci(u)
                if mv not in b.legal_moves:
                    break
                line.append(b.san(mv))
                b.push(mv)
        after = board.copy()
        after.push(chess.Move.from_uci(r["uci"]))
        reply = None
        if engine is not None and not after.is_game_over():
            ev2 = engine.evaluate(after, chess.engine.Limit(depth=depth, time=0.6))
            if ev2.best_move:
                reply = after.san(chess.Move.from_uci(ev2.best_move))
        num = f"{r['move_number']}{'.' if r['color'] == 'white' else '...'}"
        why = ", and ".join(TAG_TEXT[t] for t in r["tags"][:2])
        out.append({**{k: r[k] for k in ("ply", "color", "san", "best_san", "class", "loss", "win_before", "win_after", "fen", "uci",
                                         "best", "tags", "phase")},
                    "label": f"{num}{r['san']}", "line": line, "reply": reply,
                    "explain": ((f"{num}{r['san']} cost {round(r['loss'])}% of the winning chances" if round(r["loss"]) >= 1
                                 else f"{num}{r['san']} came when the game was already decided") + (f": {why}" if why else "")
                                + (f". {r['best_san']} was the move" if r["best_san"] else "") + "."
                                + (f" After it, {reply} is the strongest reply." if reply else ""))})
    return out


def turning_point(rows: list[dict]) -> dict | None:
    """The move after which the game was decided for good (the winner's expected score stayed above 80%)."""
    if not rows:
        return None
    white = [100 - rows[0]["win_before"] if rows[0]["color"] == "black" else rows[0]["win_before"]]
    for r in rows:
        white.append(r["win_after"] if r["color"] == "white" else 100 - r["win_after"])
    final = white[-1]
    if 20 < final < 80:
        return None
    winner_white = final >= 80
    k = len(white) - 1
    while k > 0 and ((white[k - 1] >= 80) if winner_white else (white[k - 1] <= 20)):
        k -= 1
    if k == 0:
        return None
    r = rows[k - 1]
    return {"ply": r["ply"], "label": f"{r['move_number']}{'.' if r['color'] == 'white' else '...'}{r['san']}", "color": r["color"]}


def build_report(game: dict, engine, depth: int, progress=None) -> dict:
    """game: {moves (UCI), white, black, result, date, event, time_control, termination, you ('white'/'black'/None),
    clocks ({ply: seconds left before the move}), base (initial seconds)}."""
    moves = game["moves"]
    evals = evaluate_positions(moves, engine, depth, progress)
    rows = classify_moves(moves, evals, game.get("clocks"), game.get("base"))
    series = [round(_win(evals[0]["cp"]), 1)]
    for r in rows:
        series.append(r["win_after"] if r["color"] == "white" else round(100 - r["win_after"], 1))
    sides = {c: side_summary(rows, c) for c in ("white", "black")}
    opening = openings.identify(moves)
    you = game.get("you")
    km = key_moments(rows, engine, depth)
    tp = turning_point(rows)
    final = chess.Board()
    for u in moves:
        final.push(chess.Move.from_uci(u))
    return {"version": VERSION, "depth": depth, "created_at": time.time(),
            **{k: game.get(k, "") for k in ("white", "black", "result", "date", "event", "time_control", "termination")},
            "you": you, "opening": opening, "plies": len(moves), "moves": rows, "series": series, "sides": sides, "final_fen": final.fen(),
            "key_moments": km, "turning_point": tp, "verdict": verdict(rows, sides, game.get("result", ""), tp, you)}


def verdict(rows: list[dict], sides: dict, result: str, tp: dict | None, you: str | None) -> str:
    """One line on how the game was decided."""
    worst = max(rows, key=lambda r: r["loss"], default=None)
    if not rows:
        return "No moves to review."
    who = lambda c: "you" if you == c else c.capitalize()  # noqa: E731
    if worst and worst["loss"] >= 30:
        num = f"{worst['move_number']}{'.' if worst['color'] == 'white' else '...'}{worst['san']}"
        return f"Decided by one big moment: {num}, a blunder by {who(worst['color'])}."
    if tp:
        return f"The game turned after {tp['label']}."
    if result == "1/2-1/2":
        return "A balanced game: neither side made a decisive mistake."
    acc = {c: sides[c]["accuracy"] or 0 for c in sides}
    better = max(acc, key=acc.get)
    return f"Won on accuracy: {who(better)} played the more precise moves throughout."


# ---- annotated PGN ----------------------------------------------------------------------------------------------
def annotated_pgn(report: dict) -> str:
    game = chess.pgn.Game()
    h = game.headers
    clean = lambda v, d="?": (str(v or "").replace('"', "").replace("\\", "").strip() or d)  # noqa: E731
    h["Event"] = clean(report.get("event"), "Game review")
    h["Site"] = "Plateau Breaker game review"
    h["Date"] = clean(report.get("date"), "????.??.??")
    h["White"] = clean(report.get("white"))
    h["Black"] = clean(report.get("black"))
    h["Result"] = report.get("result") if report.get("result") in ("1-0", "0-1", "1/2-1/2") else "*"
    if report["opening"].get("eco"):
        h["ECO"] = report["opening"]["eco"]
        h["Opening"] = report["opening"]["name"]
    for c in ("white", "black"):
        s = report["sides"][c]
        if s["accuracy"] is not None:
            h[f"{c.capitalize()}Accuracy"] = str(s["accuracy"])
    h["Annotator"] = f"Stockfish (depth {report['depth']}) via Plateau Breaker"
    node = game
    board = chess.Board()
    km = {k["ply"]: k for k in report["key_moments"]}
    for r in report["moves"]:
        mv = chess.Move.from_uci(r["uci"])
        node = node.add_variation(mv)
        nag = CLASS_INFO[r["class"]][2]
        if nag:
            node.nags.add(nag)
        board.push(mv)
        if r["class"] in ("inaccuracy", "mistake", "miss", "blunder"):
            text = f"{CLASS_INFO[r['class']][1]}. {r['best_san']} was best." if r["best_san"] else CLASS_INFO[r["class"]][1] + "."
            k = km.get(r["ply"])
            if k and k["line"]:
                text += f" Line: {' '.join(k['line'])}."
            node.comment = text
        elif r["class"] in ("brilliant", "great"):
            node.comment = CLASS_INFO[r["class"]][1] + "."
        v = int(r["eval_white"])
        if abs(v) >= MATE_THRESHOLD:  # a forced mate: [%eval #3] / [%eval #-2]; none once it's mate on the board
            n = MATE_CP - abs(v)
            if n > 0:
                node.set_eval(chess.engine.PovScore(chess.engine.Mate(n if v > 0 else -n), chess.WHITE))
        else:
            node.set_eval(chess.engine.PovScore(chess.engine.Cp(v), chess.WHITE))  # after the comment, so it keeps it
    out = io.StringIO()
    print(game, file=out, end="\n\n")
    return out.getvalue()


def to_json(report: dict) -> str:
    return json.dumps(report, separators=(",", ":"))


# ---- inputs and storage -----------------------------------------------------------------------------------------
def game_input(handle: str, g) -> dict:
    """An analysed game of yours (GameAnalysis) as review input."""
    from .games import parse_time_control

    board, ucis = chess.Board(), []
    for san in g.moves_san:
        try:
            mv = board.parse_san(san)
        except ValueError:
            break
        ucis.append(mv.uci())
        board.push(mv)
    white, black = (handle, g.opponent) if g.player_color == "white" else (g.opponent, handle)
    base, _ = parse_time_control(g.time_control)
    return {"moves": ucis, "white": white, "black": black, "result": g.result, "date": g.played_at,
            "event": f"{g.time_class.capitalize()} game" if g.time_class else "Over-the-board game", "time_control": g.time_control,
            "termination": g.termination, "you": g.player_color,
            "clocks": {m.ply: m.clock_before for m in g.moves if m.clock_before is not None}, "base": base}


def save(store, key: str, game_id: str, report: dict, ai: dict | None) -> None:
    with store._conn() as c:
        c.execute("INSERT INTO game_reviews (username, game_id, version, depth, report, ai, created_at, ai_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT (username, game_id) DO UPDATE SET version = excluded.version, depth = excluded.depth, report = excluded.report, "
                  "ai = excluded.ai, created_at = excluded.created_at, ai_at = excluded.ai_at",
                  (key, game_id, VERSION, report["depth"], to_json(report), json.dumps(ai) if ai else None, time.time(),
                   time.time() if ai else None))


def save_ai(store, key: str, game_id: str, ai: dict) -> None:
    with store._conn() as c:
        c.execute("UPDATE game_reviews SET ai = ?, ai_at = ? WHERE username = ? AND game_id = ?", (json.dumps(ai), time.time(), key, game_id))


def load(store, key: str, game_id: str) -> dict | None:
    with store._conn() as c:
        r = c.execute("SELECT * FROM game_reviews WHERE username = ? AND game_id = ?", (key, game_id)).fetchone()
    if not r or r["version"] != VERSION:
        return None
    return {"report": json.loads(r["report"]), "ai": json.loads(r["ai"]) if r["ai"] else None, "depth": r["depth"],
            "created_at": r["created_at"], "ai_at": r["ai_at"]}


def reviewed_ids(store, key: str) -> set[str]:
    with store._conn() as c:
        return {r[0] for r in c.execute("SELECT game_id FROM game_reviews WHERE username = ? AND version = ?", (key, VERSION))}
