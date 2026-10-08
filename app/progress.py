"""Progress tracking (Pro): is the player actually improving?

Games are bucketed by month (or by ISO week when the history covers less than
three months). Each bucket gets the same metrics as the main profile, so the
trend lines are directly comparable with the Rating DNA.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .analysis import GameAnalysis
from .profile import contested_moves, rating_dna

MIN_GAMES_PER_BUCKET = 3


def _date(played_at: str) -> dt.date | None:
    try:
        y, m, d = (int(x) for x in played_at.replace("-", ".").split(".")[:3])
        return dt.date(y, m, d)
    except (ValueError, TypeError):
        return None


def progress_report(games: list[GameAnalysis]) -> dict:
    dated = [(d, g) for g in games if (d := _date(g.played_at))]
    if not dated:
        return {"granularity": None, "buckets": [], "trend": None, "note": "Games have no dates, so progress can't be tracked."}
    span = (max(d for d, _ in dated) - min(d for d, _ in dated)).days
    weekly = span < 90
    groups: dict[str, list[GameAnalysis]] = defaultdict(list)
    for d, g in dated:
        key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}" if weekly else f"{d.year}-{d.month:02d}"
        groups[key].append(g)

    buckets = []
    for key in sorted(groups):
        gs = groups[key]
        if len(gs) < MIN_GAMES_PER_BUCKET:
            continue
        moves = contested_moves(gs)
        dna = rating_dna(gs)
        ratings = sorted(g.player_rating for g in gs if g.player_rating)
        buckets.append({
            "period": key,
            "games": len(gs),
            "score_pct": round(100 * sum(g.score for g in gs) / len(gs)),
            "accuracy": dna["overall_accuracy"],
            "blunders_per_100": round(100 * sum(m.classification == "blunder" for m in moves) / len(moves), 2) if moves else 0.0,
            "rating": ratings[len(ratings) // 2] if ratings else None,
            "skills": {s["key"]: s["accuracy"] for s in dna["skills"] if not s["low_confidence"]},
        })

    trend = None
    if len(buckets) >= 2:
        a, b = buckets[0], buckets[-1]
        skill_change = {k: round(b["skills"][k] - a["skills"][k], 1) for k in a["skills"] if k in b["skills"]}
        trend = {
            "from": a["period"],
            "to": b["period"],
            "accuracy_change": round(b["accuracy"] - a["accuracy"], 1),
            "blunder_rate_change": round(b["blunders_per_100"] - a["blunders_per_100"], 2),
            "rating_change": (b["rating"] - a["rating"]) if a["rating"] and b["rating"] else None,
            "most_improved": max(skill_change, key=skill_change.get) if skill_change else None,
            "most_declined": min(skill_change, key=skill_change.get) if skill_change else None,
            "skill_accuracy_change": skill_change,
        }
    note = None if len(buckets) >= 2 else f"Need at least two {'weeks' if weekly else 'months'} with {MIN_GAMES_PER_BUCKET}+ games to show a trend."
    return {"granularity": "week" if weekly else "month", "buckets": buckets, "trend": trend, "note": note}
