"""The "Think here" trainer: learn to feel when a position needs a long think.

Most club players don't lose games because they can't find good moves. They lose them because they play a
critical position at the same speed as a routine one. This module finds, in your own games:

* **Critical moments**: one move clearly matters. Stockfish's best move is at least CRITICAL_GAP win-% points
  better than its second choice, and the position isn't already lost or won either way. Plain recaptures
  and forced replies (in check, 1-2 legal moves) are marked *obvious*: playing those fast is right.
* **How long you took**: from the clocks in the PGN, `clock before - clock after + increment`.
* **Rushed** = fast on a critical moment, where fast is relative to your own pace in that time control
  (60% of your median time per move, between 2 and 10 seconds).
* **Over-thought** = a long think (3x your median, at least 12 s) on a routine move, where several moves
  are about equally good: time you'll miss later.

The drill shows these positions and asks first "think or play?", then (if it was critical) for the move.
The stats put a number on it: the win-% you lose on rushed critical moves beyond what you lose when you do
stop and think, turned into points per game and a rating estimate with the Elo formula.
"""

from __future__ import annotations

import json
import math
import statistics
import time

import chess
import chess.engine

from .analysis import GameAnalysis, win_percent
from .games import parse_time_control
from .store import BOX_INTERVAL_DAYS, DAY

CRITICAL_GAP = 15.0  # win-% points between the best and the second-best move
HOPELESS = 15.0  # best move still leaves you below this: nothing to find
ROUTINE_GAP = 6.0  # several moves this close to the best: a routine position
CONTESTED_CP = 600
FAST_SHARE, FAST_MIN, FAST_MAX = 0.6, 2.0, 10.0
SLOW_FACTOR, SLOW_MIN = 3.0, 12.0
SCAN_CHUNK = 10
SKIP_MOVES = 4  # your first moves are mostly memorised theory
ALSO_GOOD = 3.0  # a move within 3 win-% of the best is just as good
PRIOR_SLOW_LOSS, PRIOR_WEIGHT = 8.0, 5.0  # what thinking typically saves, before we have your own numbers


# ---- clocks --------------------------------------------------------------------------------------------
def time_spent(g: GameAnalysis) -> dict[int, float]:
    """Seconds you spent on each of your moves (by ply). The last move, with no clock after it, is left out."""
    _, inc = parse_time_control(g.time_control)
    mine = sorted((m for m in g.moves if m.clock_before is not None), key=lambda m: m.ply)
    out = {}
    for a, b in zip(mine, mine[1:], strict=False):
        if b.ply - a.ply != 2:
            continue
        out[a.ply] = max(0.1, round(a.clock_before - b.clock_before + inc, 1))
    return out


def fast_threshold(median_s: float) -> float:
    return round(min(FAST_MAX, max(FAST_MIN, FAST_SHARE * median_s)), 1)


def slow_threshold(median_s: float) -> float:
    return round(max(SLOW_MIN, SLOW_FACTOR * median_s), 1)


# ---- scanning games -------------------------------------------------------------------------------------
def _boards(g: GameAnalysis) -> list[chess.Board] | None:
    b, out = chess.Board(), [chess.Board()]
    for san in g.moves_san:
        try:
            b.push_san(san)
        except ValueError:
            return None
        out.append(b.copy(stack=False))
    return out


def scan_game(g: GameAnalysis, engine, depth: int) -> list[dict]:
    """Every one of your moves with clock data, with the engine's top three and how much the position demanded."""
    spent = time_spent(g)
    if not spent:
        return []
    boards = _boards(g)
    if boards is None:
        return []
    base, _ = parse_time_control(g.time_control)
    limit = chess.engine.Limit(depth=depth, time=0.4)
    out = []
    for m in g.moves:
        if m.ply not in spent or m.ply < 2 * SKIP_MOVES or abs(m.cp_before) > CONTESTED_CP:
            continue
        board = boards[m.ply]
        legal = board.legal_moves.count()
        if legal <= 1:
            continue
        cands = engine.candidates(board, 3, limit)
        if not cands:
            continue
        wins = [win_percent(cp) for _, cp in cands]
        best_win = wins[0]
        gap = best_win - wins[1] if len(wins) > 1 else 100.0
        critical = gap >= CRITICAL_GAP and best_win >= HOPELESS and (len(wins) < 2 or wins[1] <= 92)
        prev = boards[m.ply - 1]
        last = chess.Move.from_uci(_uci(prev, g.moves_san[m.ply - 1])) if m.ply >= 1 else None
        best = chess.Move.from_uci(cands[0][0])
        recapture = bool(last and prev.is_capture(last) and best.to_square == last.to_square and board.is_capture(best))
        obvious = critical and (recapture or (board.is_check() and legal <= 2))
        out.append({
            "game_id": g.game_id, "ply": m.ply, "fen": board.fen(), "prev_fen": prev.fen(), "prev_uci": last.uci() if last else None,
            "played": m.played, "played_san": m.played_san,
            "cands": [[u, board.san(chess.Move.from_uci(u)), round(w, 1)] for (u, _), w in zip(cands, wins, strict=True)],
            "best_win": round(best_win, 1), "gap": round(gap, 1), "loss": round(m.win_loss, 1), "spent": spent[m.ply],
            "clock": m.clock_before, "base": base, "critical": int(critical), "obvious": int(obvious), "phase": m.phase,
            "time_class": g.time_class or "", "played_at": g.played_at or "", "opponent": g.opponent or "", "move_number": m.move_number,
        })
    return out


def _uci(board: chess.Board, san: str) -> str:
    return board.parse_san(san).uci()


def save_scan(store, key: str, game_id: str, moments: list[dict]) -> None:
    with store._conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO think_moments (username, game_id, ply, fen, prev_fen, prev_uci, played, played_san, cands, best_win, gap, "
            "loss, spent, clock, base, critical, obvious, phase, time_class, played_at, opponent, move_number) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(key, m["game_id"], m["ply"], m["fen"], m["prev_fen"], m["prev_uci"], m["played"], m["played_san"], json.dumps(m["cands"]),
              m["best_win"], m["gap"], m["loss"], m["spent"], m["clock"], m["base"], m["critical"], m["obvious"], m["phase"],
              m["time_class"], m["played_at"], m["opponent"], m["move_number"]) for m in moments])
        c.execute("INSERT OR REPLACE INTO think_games (username, game_id, moments, scanned_at) VALUES (?, ?, ?, ?)",
                  (key, game_id, len(moments), time.time()))


def scanned_ids(store, key: str) -> set[str]:
    with store._conn() as c:
        return {r[0] for r in c.execute("SELECT game_id FROM think_games WHERE username = ?", (key,))}


def pick_unscanned(games: list[GameAnalysis], done: set[str], budget: int) -> list[str]:
    """Newest first, only games with clock times, up to the budget."""
    with_clock = [g for g in sorted(games, key=lambda g: (g.played_at, g.game_id), reverse=True)
                  if g.game_id not in done and sum(m.clock_before is not None for m in g.moves) > 4]
    return [g.game_id for g in with_clock[:max(0, budget)]]


def clock_games(games: list[GameAnalysis]) -> int:
    return sum(1 for g in games if sum(m.clock_before is not None for m in g.moves) > 4)


# ---- reading it back ------------------------------------------------------------------------------------------
def moments(store, key: str) -> list[dict]:
    with store._conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM think_moments WHERE username = ?", (key,))]
    for r in rows:
        r["cands"] = json.loads(r["cands"])
    return rows


def medians(rows: list[dict]) -> dict[str, float]:
    """Your median time per move in each time control."""
    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r["time_class"] or "other", []).append(r["spent"])
    return {k: statistics.median(v) for k, v in by.items() if v}


def median_for(store, key: str, time_class: str) -> float:
    """Your median time per move in one time control, straight from the database (no need to load every move)."""
    with store._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM think_moments WHERE username = ? AND time_class = ?", (key, time_class or "")).fetchone()[0]
        if not n:
            return 10.0
        if n % 2:
            return c.execute("SELECT spent FROM think_moments WHERE username = ? AND time_class = ? ORDER BY spent LIMIT 1 OFFSET ?",
                             (key, time_class or "", n // 2)).fetchone()[0]
        a, b = (r[0] for r in c.execute("SELECT spent FROM think_moments WHERE username = ? AND time_class = ? ORDER BY spent "
                                         "LIMIT 2 OFFSET ?", (key, time_class or "", n // 2 - 1)))
        return (a + b) / 2


def label(r: dict, med: dict[str, float]) -> dict:
    """Add fast / slow / routine flags, relative to your own pace in that time control."""
    m = med.get(r["time_class"] or "other", 10.0)
    r["fast"] = r["spent"] <= fast_threshold(m)
    r["slow"] = r["spent"] >= slow_threshold(m)
    r["routine"] = not r["critical"] and r["gap"] < ROUTINE_GAP
    r["rushed"] = bool(r["critical"] and not r["obvious"] and r["fast"])
    r["overthought"] = bool(r["routine"] and r["slow"])
    r["plenty_of_time"] = bool(r["clock"] and r["base"] and r["clock"] >= 0.25 * r["base"])
    return r


def elo_from_score(d: float) -> float:
    """Rating difference worth an expected score of 0.5 + d per game (the Elo formula)."""
    d = max(-0.45, min(0.45, d))
    return 400 * math.log10((0.5 + d) / (0.5 - d))


def stats(store, key: str, raw: list[dict] | None = None) -> dict:
    raw = moments(store, key) if raw is None else [dict(r) for r in raw]
    med = medians(raw)
    rows = [label(r, med) for r in raw]
    with store._conn() as c:
        games = c.execute("SELECT COUNT(*) FROM think_games WHERE username = ? AND moments > 0", (key,)).fetchone()[0]
    crit = [r for r in rows if r["critical"] and not r["obvious"]]
    rushed = [r for r in crit if r["fast"]]
    thought = [r for r in crit if not r["fast"]]
    routine = [r for r in rows if r["routine"]]
    over = [r for r in routine if r["slow"]]
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    median = lambda xs: round(statistics.median(xs), 1) if xs else None  # noqa: E731
    # What thinking would have saved: your loss on rushed critical moves beyond your loss when you did think
    # (shrunk toward a typical value while you have few "thought" examples).
    slow_loss = (sum(r["loss"] for r in thought) + PRIOR_SLOW_LOSS * PRIOR_WEIGHT) / (len(thought) + PRIOR_WEIGHT)
    recoverable = sum(max(0.0, r["loss"] - slow_loss) for r in rushed)
    per_game = recoverable / 100 / games if games else 0.0
    phases = {}
    for p in ("opening", "middlegame", "endgame"):
        rp = [r for r in rushed if r["phase"] == p]
        if rp:
            phases[p] = {"rushed": len(rp), "lost": round(sum(r["loss"] for r in rp), 1)}
    t_crit, t_routine = median([r["spent"] for r in crit]), median([r["spent"] for r in routine])
    buckets = []
    for lo, hi, name in ((0, ROUTINE_GAP, "Routine"), (ROUTINE_GAP, CRITICAL_GAP, "Some care"), (CRITICAL_GAP, 30, "Critical"),
                         (30, 1000, "Only move")):
        xs = [r["spent"] for r in rows if lo <= r["gap"] < hi and not r["obvious"]]
        buckets.append({"label": name, "median_s": median(xs), "moves": len(xs)})
    return {
        "games": games, "moves": len(rows), "medians": {k: round(v, 1) for k, v in med.items()},
        "fast_below": {k: fast_threshold(v) for k, v in med.items()},
        "critical": len(crit), "rushed": len(rushed), "rushed_pct": round(100 * len(rushed) / len(crit)) if crit else None,
        "rushed_with_time": sum(r["plenty_of_time"] for r in rushed),
        "loss_rushed": mean([r["loss"] for r in rushed]), "loss_thought": mean([r["loss"] for r in thought]),
        "rushed_errors": sum(r["loss"] >= 10 for r in rushed),
        "points_lost": round(recoverable / 100, 2), "points_per_10_games": round(10 * per_game, 2),
        "rating_estimate": round(elo_from_score(per_game)) if games else None,
        "time_critical_s": t_crit, "time_routine_s": t_routine,
        "time_ratio": round(t_crit / t_routine, 2) if t_crit and t_routine else None,
        "overthought": len(over), "overthought_s": round(sum(r["spent"] for r in over)), "phases": phases, "buckets": buckets,
    }


# ---- the drill ----------------------------------------------------------------------------------------------------
def drill(store, key: str, limit: int = 20, raw: list[dict] | None = None) -> list[dict]:
    """Due drill positions: rushed critical moments (costliest first), mixed with over-thought routine ones."""
    rows = moments(store, key) if raw is None else [dict(r) for r in raw]
    med = medians(rows)
    now = time.time()
    rows = [label(r, med) for r in rows]
    due = [r for r in rows if (r["rushed"] or r["overthought"]) and (r["box"] == 0 or r["due_at"] <= now)]
    # New ones first; among them, moments where you had time to think (rushing in time trouble is a different problem)
    crit = sorted((r for r in due if r["rushed"]), key=lambda r: (r["box"] > 0, not r["plenty_of_time"], -r["loss"], -r["gap"]))
    calm = sorted((r for r in due if r["overthought"]), key=lambda r: (r["box"] > 0, -r["spent"]))
    out, i, j = [], 0, 0
    while len(out) < limit and (i < len(crit) or j < len(calm)):  # two critical, then one routine
        for _ in range(2):
            if i < len(crit) and len(out) < limit:
                out.append(crit[i])
                i += 1
        if j < len(calm) and len(out) < limit:
            out.append(calm[j])
            j += 1
    return [public(r) for r in out]


def public(r: dict) -> dict:
    """What the browser may see before answering: no engine moves, no verdict."""
    return {"id": r["id"], "fen": r["fen"], "prev_fen": r["prev_fen"], "prev_uci": r["prev_uci"], "move_number": r["move_number"],
            "time_class": r["time_class"], "played_at": r["played_at"], "opponent": r["opponent"], "clock": r["clock"], "base": r["base"],
            "phase": r["phase"], "box": r["box"], "game_id": r["game_id"]}


def get(store, item_id: int) -> dict | None:
    with store._conn() as c:
        r = c.execute("SELECT * FROM think_moments WHERE id = ?", (item_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["cands"] = json.loads(d["cands"])
    return d


def fine_moves(r: dict) -> list[str]:
    best = r["cands"][0][2] if r["cands"] else 0
    return [san for _, san, w in r["cands"] if best - w <= ROUTINE_GAP]


def call(store, r: dict, choice: str, med: dict[str, float]) -> dict:
    """Your answer to "think or play?". Routine positions finish here; critical ones go on to "find the move"."""
    label(r, med)
    critical = bool(r["critical"])
    right = (choice == "think") == critical
    with store._conn() as c:
        c.execute("UPDATE think_moments SET last_call = ?, calls_right = calls_right + ? WHERE id = ?",
                  ("right" if right else "wrong", int(right), r["id"]))
    out = {"right": right, "critical": critical, "spent": r["spent"], "played_san": r["played_san"], "loss": r["loss"],
           "move_number": r["move_number"], "fast_below": fast_threshold(med.get(r["time_class"] or "other", 10.0))}
    if critical:
        out["message"] = ("Right: one move matters here. Find it." if right else
                          "Careful: this was critical. Only one move keeps your position here. Find it.")
        return out
    fine = fine_moves(r)
    out["fine_moves"] = fine
    out["message"] = ("Right: a routine position. " if right else "This one was routine. ") + \
        (f"{', '.join(fine[:4])} {'are' if len(fine) > 1 else 'is'} all about equal" if fine else "Several moves are about equal") + \
        f". In the game you spent {_secs(r['spent'])} here: time you could have saved for a critical moment."
    out.update(_schedule(store, r["id"], right))
    return out


def move(store, r: dict, uci: str, practice: bool, engine=None, reveal: bool = False) -> dict:
    """Grade the move on a critical position: the best move, or anything as good (within 3 win-%).
    reveal=True shows the answer and counts as not found."""
    board = chess.Board(r["fen"])
    best_u, best_san, best_w = r["cands"][0]
    if reveal:
        mv = chess.Move.from_uci(best_u)
    else:
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            mv = None
        if mv is None or mv not in board.legal_moves:
            try:
                mv = board.parse_san(uci)
            except ValueError as exc:
                raise ValueError("That move isn't legal here.") from exc
    verdict = "wrong"
    if reveal:
        pass
    elif mv.uci() == best_u:
        verdict = "correct"
    else:
        known = {u: w for u, _, w in r["cands"]}
        w = known.get(mv.uci())
        if w is None and engine is not None:
            after = board.copy()
            after.push(mv)
            ev = engine.evaluate(after)
            w = win_percent(ev.cp_for(board.turn))
        if w is not None and best_w - w <= ALSO_GOOD:
            verdict = "also_good"
    out = {"verdict": verdict, "your_move": board.san(mv), "best": best_u, "best_san": best_san, "played_san": r["played_san"],
           "loss": r["loss"], "spent": r["spent"], "second_san": r["cands"][1][1] if len(r["cands"]) > 1 else None, "gap": r["gap"]}
    if practice:
        return out | {"practice": True}
    right_call = r.get("last_call") == "right"
    return out | _schedule(store, r["id"], right_call and verdict != "wrong", solved=verdict != "wrong")


def _schedule(store, item_id: int, good: bool, solved: bool = False) -> dict:
    with store._conn() as c:
        box = c.execute("SELECT box FROM think_moments WHERE id = ?", (item_id,)).fetchone()[0]
        box = min(max(box, 1) + 1, 5) if good else 1  # a new position (box 0) answered right goes to box 2, like a puzzle
        due = time.time() + (BOX_INTERVAL_DAYS[box] * DAY if good else 600)
        c.execute("UPDATE think_moments SET box = ?, due_at = ?, attempts = attempts + 1, solved = solved + ? WHERE id = ?",
                  (box, due, int(solved), item_id))
    return {"box": box, "next_review_in_days": round((due - time.time()) / DAY, 2)}


def _secs(s: float) -> str:
    s = round(s)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"
