"""Rating DNA, insights and the weekly study plan.

Rating DNA is a *heuristic*: it doesn't claim to be a true Elo per skill.
It takes your real rating as the anchor and moves each skill up or down by how
your accuracy in that kind of position compares with your overall accuracy.
That makes it explainable ("you're 9 accuracy points worse in endgames than
your average") and stable with modest sample sizes.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from collections.abc import Callable

from .analysis import GameAnalysis, MoveAnalysis

ELO_PER_ACCURACY_POINT = 12
MAX_SKILL_OFFSET = 350
MIN_MOVES_FOR_CONFIDENCE = 25
DEFAULT_BASE_RATING = 1500
# Moves played when the game is already decided (beyond +/-6 pawns) barely move
# the win probability, so they score ~100% accuracy whatever you play and would
# hide real weaknesses. Skill scores only use "contested" moves.
CONTESTED_CP = 600

SKILLS: dict[str, tuple[str, Callable[[MoveAnalysis], bool]]] = {
    "openings": ("Openings", lambda m: m.phase == "opening"),
    "middlegame": ("Middlegame play", lambda m: m.phase == "middlegame"),
    "endgames": ("Endgames", lambda m: m.phase == "endgame"),
    "tactics": ("Tactics", lambda m: m.tactical),
    "time": ("Time management", lambda m: m.time_pressure),
    "converting": ("Converting wins", lambda m: m.cp_before >= 200),
    "defending": ("Defending", lambda m: m.cp_before <= -200),
}

TAG_LABELS = {
    "missed_tactic": "missing a forcing move (capture, check or promotion)",
    "hanging_piece": "leaving a piece en prise",
    "time_trouble": "mistakes in time trouble",
    "conversion": "slipping in a winning position",
    "missed_mate": "missing a forced mate",
}


@dataclass
class SkillScore:
    key: str
    label: str
    rating: int
    accuracy: float
    moves: int
    share_of_moves: float  # % of your moves in this kind of position
    share_of_loss: float  # % of all win-probability you lost that was lost here
    low_confidence: bool


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def all_moves(games: list[GameAnalysis]) -> list[MoveAnalysis]:
    return [m for g in games for m in g.moves]


def contested_moves(games: list[GameAnalysis]) -> list[MoveAnalysis]:
    return [m for m in all_moves(games) if abs(m.cp_before) <= CONTESTED_CP]


def base_rating(games: list[GameAnalysis], override: int | None = None) -> tuple[int, bool]:
    """(rating, is_estimated). Uses the median rating recorded in the games."""
    if override:
        return override, False
    ratings = [g.player_rating for g in games if g.player_rating]
    if ratings:
        return int(statistics.median(ratings)), False
    return DEFAULT_BASE_RATING, True


def rating_dna(games: list[GameAnalysis], override: int | None = None) -> dict:
    moves = contested_moves(games)
    base, estimated = base_rating(games, override)
    overall_acc = _mean([m.accuracy for m in moves])
    total_loss = sum(m.win_loss for m in moves) or 1.0

    skills: list[SkillScore] = []
    for key, (label, selector) in SKILLS.items():
        subset = [m for m in moves if selector(m)]
        if not subset:
            continue
        acc = _mean([m.accuracy for m in subset])
        offset = max(-MAX_SKILL_OFFSET, min(MAX_SKILL_OFFSET, ELO_PER_ACCURACY_POINT * (acc - overall_acc)))
        skills.append(
            SkillScore(
                key=key,
                label=label,
                rating=int(round(base + offset)),
                accuracy=round(acc, 1),
                moves=len(subset),
                share_of_moves=round(100 * len(subset) / len(moves), 1),
                share_of_loss=round(100 * sum(m.win_loss for m in subset) / total_loss, 1),
                low_confidence=len(subset) < MIN_MOVES_FOR_CONFIDENCE,
            )
        )
    return {
        "base_rating": base,
        "rating_is_estimated": estimated,
        "overall_accuracy": round(overall_acc, 1),
        "games": len(games),
        "moves": len(moves),
        "contested_cp": CONTESTED_CP,
        "skills": [s.__dict__ for s in skills],
    }


def insights(games: list[GameAnalysis], dna: dict) -> list[dict]:
    """Plain-language findings, most important first. Each has a headline number."""
    out: list[dict] = []
    moves = contested_moves(games)
    confident = [s for s in dna["skills"] if not s["low_confidence"]]

    if confident:
        worst = min(confident, key=lambda s: s["rating"])
        gap = dna["base_rating"] - worst["rating"]
        if gap > 0:
            out.append({
                "severity": "high" if gap >= 120 else "medium",
                "stat": str(worst["rating"]),
                "text": f"{worst['label']} is your weakest area, about {gap} points below your overall {dna['base_rating']}.",
                "skill": worst["key"],
            })
        lopsided = max(confident, key=lambda s: s["share_of_loss"] - s["share_of_moves"])
        excess = lopsided["share_of_loss"] - lopsided["share_of_moves"]
        if excess >= 5:
            out.append({
                "severity": "medium",
                "stat": f"{lopsided['share_of_loss']:.0f}%",
                "text": (f"of the winning chances you throw away go in {lopsided['label'].lower()}, "
                         f"though it's only {lopsided['share_of_moves']:.0f}% of your moves."),
                "skill": lopsided["key"],
            })

    had_it = [g for g in games if g.peak_cp >= 300]
    failed = [g for g in had_it if g.conversion_failure]
    if len(had_it) >= 3 and failed:
        pct = 100 * len(failed) / len(had_it)
        out.append({
            "severity": "high" if pct >= 30 else "medium",
            "stat": f"{len(failed)} of {len(had_it)}",
            "text": "games where you reached +3 or better ended without a win.",
            "skill": "converting",
        })

    pressured = [m for m in moves if m.time_pressure]
    calm = [m for m in moves if not m.time_pressure]
    if len(pressured) >= 15 and calm:
        rate_p = sum(m.classification == "blunder" for m in pressured) / len(pressured)
        rate_c = sum(m.classification == "blunder" for m in calm) / len(calm)
        if rate_c > 0 and rate_p / rate_c >= 1.5:
            out.append({
                "severity": "high" if rate_p / rate_c >= 2.5 else "medium",
                "stat": f"{rate_p / rate_c:.1f}×",
                "text": "more blunders per move when you're low on the clock than when you have time.",
                "skill": "time",
            })

    tags = Counter(t for m in moves for t in m.tags)
    if tags:
        tag, count = tags.most_common(1)[0]
        out.append({
            "severity": "medium",
            "stat": str(count),
            "text": f"of your mistakes and blunders came from {TAG_LABELS.get(tag, tag)}, your most common pattern.",
            "skill": None,
        })

    by_opening: dict[str, list[float]] = {}
    for g in games:
        by_opening.setdefault(g.opening.split(":")[0], []).append(g.score)
    scored = [(o, _mean(s), len(s)) for o, s in by_opening.items() if len(s) >= 3]
    if scored:
        o, score, n = min(scored, key=lambda x: x[1])
        if score < 0.45:
            out.append({
                "severity": "low",
                "stat": f"{100 * score:.0f}%",
                "text": f"your score in the {o} across {n} games, your weakest opening.",
                "skill": "openings",
            })

    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda i: rank[i["severity"]])


STUDY_TEMPLATES = {
    "openings": [("Review your 3 most-played lines against your games and fix the move where you left theory", 20)],
    "middlegame": [("Annotated master game in your openings: guess each move before revealing it", 25)],
    "endgames": [("Rook-ending technique: active rook, cutting off the king, Lucena and Philidor", 20),
                 ("Play out a won endgame from your own games against the engine", 15)],
    "tactics": [("Timed tactics set at your rating: aim for accuracy, not speed", 20)],
    "time": [("Play one game with a reserve rule: never go below 20% of your starting clock before move 30", 25)],
    "converting": [("Take 3 positions you had at +3 and play them out against the engine until you win", 20)],
    "defending": [("Defend a worse position from your games against the engine for 20 moves", 20)],
}


def weekly_plan(dna: dict, due_puzzles: int, minutes_per_day: int = 45) -> dict:
    confident = [s for s in dna["skills"] if not s["low_confidence"]] or dna["skills"]
    focus = sorted(confident, key=lambda s: s["rating"])[:2]
    days = []
    for d in range(7):
        skill = focus[d % len(focus)] if focus else None
        items = [{"task": f"Puzzles from your own mistakes ({min(due_puzzles, 8) or 'new'} due)", "minutes": 10, "skill": "tactics"}]
        if skill:
            for task, mins in STUDY_TEMPLATES.get(skill["key"], []):
                items.append({"task": task, "minutes": mins, "skill": skill["key"]})
        if d in (2, 5):
            items.append({"task": "One rated game at your main time control, then review it here", "minutes": 25, "skill": None})
        # Trim to the time budget, always keeping the first item.
        kept, total = [], 0
        for it in items:
            if not kept or total + it["minutes"] <= minutes_per_day:
                kept.append(it)
                total += it["minutes"]
        days.append({"day": d + 1, "focus": skill["label"] if skill else "General", "items": kept, "minutes": total})
    return {"focus": [s["label"] for s in focus], "days": days}
