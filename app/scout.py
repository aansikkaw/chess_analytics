"""Opponent Scout (Pro): prepare for a specific opponent from their public games.

Works for any Lichess or Chess.com player, no engine run over their games needed
(fast). The report covers:
  * what they play, by colour, and how often;
  * lines where they score badly (steer into these) or very well (avoid);
  * habits: losing on time, collapsing early, game length;
  * the likely battleground: where your repertoire meets theirs, with your score there;
  * engine-checked evaluations at the end of their main lines.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median

import chess

from .analysis import GameAnalysis
from .engine import Engine
from .games import GameRecord
from .repertoire import fmt_line

DEPTHS = (2, 4, 6, 8)


def _sans(rec: GameRecord, plies: int = 12) -> list[str]:
    b, out = chess.Board(), []
    for uci in rec.moves[:plies]:
        mv = chess.Move.from_uci(uci)
        out.append(b.san(mv))
        b.push(mv)
    return out


def _tree(recs: list[tuple[GameRecord, list[str]]]) -> list[dict]:
    rows = []
    total = len(recs) or 1
    for depth in DEPTHS:
        groups: dict[tuple, list[GameRecord]] = defaultdict(list)
        for r, sans in recs:
            if len(sans) >= depth:
                groups[tuple(sans[:depth])].append(r)
        for line, gs in groups.items():
            if len(gs) < 2:
                continue
            rows.append({
                "line": fmt_line(list(line)),
                "sans": list(line),
                "depth": depth,
                "games": len(gs),
                "share_pct": round(100 * len(gs) / total),
                "their_score_pct": round(100 * sum(g.player_score for g in gs) / len(gs)),
                "opening": gs[0].opening,
            })
    return rows


def _engine_eval(sans: list[str], for_color: chess.Color, engine: Engine | None) -> str | None:
    if engine is None:
        return None
    b = chess.Board()
    for s in sans:
        b.push_san(s)
    cp = engine.evaluate(b).cp_for(for_color)
    return f"{cp / 100:+.1f}" if abs(cp) < 9000 else ("mate" if cp > 0 else "getting mated")


def scout_report(opponent: str, games: list[GameRecord], engine: Engine | None = None,
                 my_games: list[GameAnalysis] | None = None) -> dict:
    if not games:
        return {"opponent": opponent, "games": 0, "insights": ["No recent rated games found for this player."]}
    recs = [(g, _sans(g)) for g in games]
    by_color = {
        "white": [(g, s) for g, s in recs if g.player_color == chess.WHITE],
        "black": [(g, s) for g, s in recs if g.player_color == chess.BLACK],
    }
    score = sum(g.player_score for g in games) / len(games)
    by_tc: dict[str, list[float]] = defaultdict(list)
    for g in games:
        by_tc[g.time_class or "other"].append(g.player_score)

    report: dict = {
        "opponent": opponent,
        "games": len(games),
        "score_pct": round(100 * score),
        "by_time_class": {k: {"games": len(v), "score_pct": round(100 * sum(v) / len(v))} for k, v in by_tc.items()},
        "as_white": [], "as_black": [], "steer_into": [], "avoid": [], "prep": [], "battleground": [], "habits": {},
        "insights": [],
    }
    for color, items in by_color.items():
        rows = sorted(_tree(items), key=lambda r: (-r["games"], r["depth"]))
        report[f"as_{color}"] = [{k: v for k, v in r.items() if k != "sans"} for r in rows[:12]]
        you = chess.BLACK if color == "white" else chess.WHITE  # you play the other side
        for r in rows:
            if r["depth"] >= 4 and r["games"] >= 3 and r["their_score_pct"] <= 40:
                report["steer_into"].append({"their_color": color, **{k: v for k, v in r.items() if k != "sans"}})
            if r["depth"] >= 4 and r["games"] >= 3 and r["their_score_pct"] >= 70:
                report["avoid"].append({"their_color": color, **{k: v for k, v in r.items() if k != "sans"}})
        main = [r for r in rows if r["depth"] == 8][:2] or [r for r in rows if r["depth"] == 6][:2]
        for r in main:
            report["prep"].append({
                "their_color": color, "line": r["line"], "games": r["games"],
                "their_score_pct": r["their_score_pct"], "eval_for_you": _engine_eval(r["sans"], you, engine),
            })

    report["steer_into"] = sorted(report["steer_into"], key=lambda r: (r["their_score_pct"], -r["games"]))[:5]
    report["avoid"] = sorted(report["avoid"], key=lambda r: (-r["their_score_pct"], -r["games"]))[:5]

    # Habits
    lengths = [len(g.moves) // 2 for g in games]
    losses = [g for g in games if g.player_score == 0]
    on_time = [g for g in losses if g.lost_on_time]
    early = [g for g in losses if len(g.moves) // 2 < 25]
    clock_fracs = []
    for g in games:
        idx = 58 if g.player_color == chess.WHITE else 59  # clock after their 30th move (0-based ply)
        if g.initial_seconds and len(g.clocks) > idx and g.clocks[idx] is not None:
            clock_fracs.append(g.clocks[idx] / g.initial_seconds)
    report["habits"] = {
        "avg_game_length_moves": round(sum(lengths) / len(lengths)),
        "losses": len(losses),
        "losses_on_time_pct": round(100 * len(on_time) / len(losses)) if losses else 0,
        "losses_before_move_25_pct": round(100 * len(early) / len(losses)) if losses else 0,
        "median_clock_left_at_move_30_pct": round(100 * median(clock_fracs)) if len(clock_fracs) >= 5 else None,
    }

    # Where your repertoire meets theirs.
    if my_games:
        for my_color in ("white", "black"):
            mine = [g for g in my_games if g.player_color == my_color and g.moves_san]
            theirs = by_color["black" if my_color == "white" else "white"]
            if not mine or not theirs:
                continue
            for depth in (4, 2):
                my_lines: dict[tuple, list[GameAnalysis]] = defaultdict(list)
                for g in mine:
                    if len(g.moves_san) >= depth:
                        my_lines[tuple(g.moves_san[:depth])].append(g)
                their_lines: dict[tuple, list[GameRecord]] = defaultdict(list)
                for g, s in theirs:
                    if len(s) >= depth:
                        their_lines[tuple(s[:depth])].append(g)
                common = [(ln, my_lines[ln], their_lines[ln]) for ln in my_lines if ln in their_lines]
                if common:
                    ln, mg, tg = max(common, key=lambda x: min(len(x[1]) / len(mine), len(x[2]) / len(theirs)))
                    report["battleground"].append({
                        "you_play": my_color,
                        "line": fmt_line(list(ln)),
                        "your_games": len(mg),
                        "your_score_pct": round(100 * sum(g.score for g in mg) / len(mg)),
                        "their_games": len(tg),
                        "their_score_pct": round(100 * sum(g.player_score for g in tg) / len(tg)),
                    })
                    break

    # Plain-language insights
    ins = report["insights"]
    ins.append(f"{opponent} scored {report['score_pct']}% over their last {len(games)} rated games.")
    for color in ("white", "black"):
        top = next((r for r in report[f"as_{color}"] if r["depth"] == 2), None)
        if top:
            ins.append(f"As {color} they most often reach {top['line']} ({top['share_pct']}% of their {color} games).")
    if report["steer_into"]:
        s = report["steer_into"][0]
        ins.append(f"Steer into {s['line']}: they score only {s['their_score_pct']}% there as {s['their_color']} ({s['games']} games).")
    h = report["habits"]
    if h["losses_on_time_pct"] >= 25:
        ins.append(f"{h['losses_on_time_pct']}% of their losses are on time. Keep the position complicated and the clock running.")
    if h["losses_before_move_25_pct"] >= 30:
        ins.append(f"{h['losses_before_move_25_pct']}% of their losses come before move 25. Early pressure pays off.")
    if h["median_clock_left_at_move_30_pct"] is not None and h["median_clock_left_at_move_30_pct"] < 20:
        ins.append(f"They typically have only {h['median_clock_left_at_move_30_pct']}% of their clock left by move 30.")
    for b in report["battleground"]:
        ins.append(f"Likely battleground when you're {b['you_play']}: {b['line']}. You score {b['your_score_pct']}% there, they score {b['their_score_pct']}%.")
    return report
