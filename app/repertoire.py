"""Repertoire Leaks (Pro): where your openings bleed points, and mistakes you keep repeating.

Two analyses:
  * Opening tree by colour. For each line you play often: games, score, your
    accuracy in the opening, and the average evaluation when the opening ends.
    A "leak" is a line you play a lot and score badly in or come out of worse.
  * Recurring mistakes. The exact same position, the exact same wrong move, more
    than once. These are the cheapest rating points available: one fix, many games.
"""

from __future__ import annotations

from collections import defaultdict

from .analysis import GameAnalysis, MoveAnalysis

LEAK_MIN_GAMES = 3


def fmt_line(sans: list[str]) -> str:
    out = []
    for i, san in enumerate(sans):
        out.append(f"{i // 2 + 1}.{san}" if i % 2 == 0 else san)
    return " ".join(out)


def _epd(fen: str) -> str:
    return " ".join(fen.split(" ")[:4])


def _opening_exit_cp(g: GameAnalysis) -> int | None:
    """The player's evaluation at the first of their moves after the opening."""
    for m in g.moves:
        if m.phase != "opening":
            return m.cp_before
    return g.moves[-1].cp_after if g.moves else None


def _line_stats(games: list[GameAnalysis]) -> dict:
    opening_moves = [m for g in games for m in g.moves if m.phase == "opening"]
    exits = [c for c in (_opening_exit_cp(g) for g in games) if c is not None]
    return {
        "games": len(games),
        "score_pct": round(100 * sum(g.score for g in games) / len(games)),
        "opening_accuracy": round(sum(m.accuracy for m in opening_moves) / len(opening_moves), 1) if opening_moves else None,
        "avg_eval_after_opening": round(sum(exits) / len(exits) / 100, 2) if exits else None,
    }


def _costliest_opening_move(games: list[GameAnalysis]) -> dict | None:
    """The opening mistake that cost most in these games; repeated ones first."""
    groups: dict[tuple, list[tuple[GameAnalysis, MoveAnalysis]]] = defaultdict(list)
    for g in games:
        for m in g.moves:
            if m.phase == "opening" and m.classification != "good":
                groups[(_epd(m.fen_before), m.played)].append((g, m))
    if not groups:
        return None
    key = max(groups, key=lambda k: (len(groups[k]), sum(m.win_loss for _, m in groups[k])))
    g, m = groups[key][0]
    return {
        "game_id": g.game_id,
        "ply": m.ply,
        "move": f"{m.move_number}{'.' if m.color == 'white' else '...'}{m.played_san}",
        "times_played": len(groups[key]),
        "engine_best": m.best_san,
        "engine_line": m.best_line[:4],
        "winning_chances_lost": m.win_loss,
        "fen": m.fen_before,
        "played_uci": m.played,
        "best_uci": m.best,
    }


def repertoire_report(games: list[GameAnalysis], depths: tuple[int, ...] = (2, 4, 6, 8)) -> dict:
    report: dict = {"white": [], "black": [], "leaks": [], "recurring_mistakes": recurring_mistakes(games)}
    for color in ("white", "black"):
        mine = [g for g in games if g.player_color == color and g.moves_san]
        seen: set[str] = set()
        rows = []
        for depth in depths:
            groups: dict[tuple, list[GameAnalysis]] = defaultdict(list)
            for g in mine:
                if len(g.moves_san) >= depth:
                    groups[tuple(g.moves_san[:depth])].append(g)
            for line, gs in groups.items():
                if len(gs) < 2:
                    continue
                text = fmt_line(list(line))
                if text in seen:
                    continue
                seen.add(text)
                row = {"color": color, "line": text, "depth": depth, "opening": gs[0].opening, **_line_stats(gs)}
                rows.append(row)
                if len(gs) >= LEAK_MIN_GAMES and (
                    row["score_pct"] < 45 or (row["avg_eval_after_opening"] is not None and row["avg_eval_after_opening"] < -0.5)
                ):
                    report["leaks"].append({**row, "costliest_move": _costliest_opening_move(gs)})
        rows.sort(key=lambda r: (-r["games"], r["depth"]))
        report[color] = rows[:15]
    # Prefer the deepest version of a leak (most specific), worst first.
    leaks = sorted(report["leaks"], key=lambda r: (r["score_pct"], r["avg_eval_after_opening"] or 0))
    kept, prefixes = [], []
    for leak in sorted(leaks, key=lambda r: -r["depth"]):
        if any(p.startswith(leak["line"]) for p in prefixes):
            continue
        prefixes.append(leak["line"])
        kept.append(leak)
    report["leaks"] = sorted(kept, key=lambda r: (r["score_pct"], r["avg_eval_after_opening"] or 0))[:8]
    return report


def recurring_mistakes(games: list[GameAnalysis], min_times: int = 2, limit: int = 10) -> list[dict]:
    groups: dict[tuple, list[tuple[GameAnalysis, MoveAnalysis]]] = defaultdict(list)
    for g in games:
        for m in g.moves:
            if m.classification != "good" and m.best and m.best != m.played:
                groups[(_epd(m.fen_before), m.played)].append((g, m))
    out = []
    for items in groups.values():
        if len(items) < min_times:
            continue
        g, m = items[0]
        out.append({
            "game_id": g.game_id,
            "ply": m.ply,
            "times": len(items),
            "you_played": m.played_san,
            "engine_best": m.best_san,
            "engine_line": m.best_line[:4],
            "phase": m.phase,
            "total_winning_chances_lost": round(sum(x.win_loss for _, x in items), 1),
            "opponents": sorted({x.opponent for x, _ in items}),
            "fen": m.fen_before,
            "played_uci": m.played,
            "best_uci": m.best,
        })
    out.sort(key=lambda r: (-r["times"], -r["total_winning_chances_lost"]))
    return out[:limit]
