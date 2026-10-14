"""The coach's written review of one game: what happened, what to learn, what to train. Grounded on the engine.

The model never analyses the game itself. It gets the facts Stockfish produced (accuracy and grades per
phase, the key moments with the engine's line and the punishing reply, the opening, the turning point) and
writes them up for a club player. The reply is a JSON form validated with Pydantic, and every move it
mentions is checked against the game and the engine's lines (app/grounding.py), and it may not quote numeric
evaluations the facts don't contain. One repair attempt; if something still doesn't check out, its sentence
is dropped. With no model configured (or if it fails), the built-in coach writes the same sections from the
facts, so a review is always there.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError, field_validator

from .grounding import Grounding
from .llm import Backend, LLMError, json_completion

FEATURES = {"predicted": "Train → Predicted for you", "think": "Train → Think here", "mistakes": "Train → Your mistakes",
            "plan": "Train → This week's plan", "coach": "Coach", "prepare": "Prepare"}


class Lesson(BaseModel):
    title: str = Field(..., max_length=120)
    detail: str = Field(..., max_length=600)
    ply: int | None = None


class Training(BaseModel):
    task: str = Field(..., max_length=200)
    why: str = Field("", max_length=300)
    feature: str | None = None

    @field_validator("feature", mode="before")
    @classmethod
    def _feature(cls, v):
        return v if v in FEATURES else None


class AIReview(BaseModel):
    headline: str = Field(..., max_length=160)
    summary: str = Field(..., max_length=1200)
    opening: str = Field("", max_length=600)
    middlegame: str = Field("", max_length=600)
    endgame: str = Field("", max_length=600)
    strengths: list[str] = Field(default_factory=list, max_length=4)
    lessons: list[Lesson] = Field(default_factory=list, max_length=5)
    training: list[Training] = Field(default_factory=list, max_length=4)

    @field_validator("strengths", mode="before")
    @classmethod
    def _strs(cls, v):
        return [str(x)[:300] for x in (v or []) if str(x).strip()][:4]


SYSTEM = """You are a friendly, precise chess coach writing a review of one game for a club player or beginner.
Use ONLY the facts in the user message: they come from Stockfish. Never invent moves, lines or evaluations,
and never analyse positions yourself. Write moves exactly as the facts write them (e.g. 23...Bxe4).
Don't quote engine numbers like +1.5; talk about winning chances in words, or the percentages in the facts.
If "you" is set, speak to that player as "you"; a key moment "by" your opponent is their mistake (a chance for
you), never yours. Explain the idea behind each mistake in plain words (what it allowed, what the better move
achieved), like a coach marking a test paper: kind, specific, useful.
Return ONLY a JSON object with these keys:
  headline: one sentence on how the game was decided
  summary: 3-5 sentences: the story of the game
  opening, middlegame, endgame: 1-3 sentences each on that phase ("" if the game never reached it)
  strengths: up to 3 short things done well
  lessons: 2-4 objects {title, detail, ply}: the most useful lessons, ply = the key moment's ply or null
  training: up to 3 objects {task, why, feature}: concrete practice; feature is one of
    predicted (puzzles predicted for the player), think (learning when to think long), mistakes (puzzles from their
    own mistakes), plan (weekly plan), coach (ask the coach), prepare (opening preparation), or null."""


def facts(report: dict) -> dict:
    """The engine's findings in a compact form for the model (and for checking what it writes)."""
    sides = report["sides"]
    def side(c):
        s = sides[c]
        return {"accuracy": s["accuracy"], "grade": s["grade"], "avg_centipawn_loss": s["acpl"],
                "counts": {k: v for k, v in s["counts"].items() if v},
                "phases": {p: {"grade": v["grade"], "accuracy": v["accuracy"], "moves": v["moves"]} for p, v in s["phases"].items() if v["moves"]}}
    op = report["opening"]
    left = op.get("left_book")
    you = report.get("you")

    def by(c):
        return c if not you else f"{c} (you)" if c == you else f"{c} (your opponent)"

    return {
        "white": report.get("white"), "black": report.get("black"), "result": report.get("result"), "you": report.get("you"),
        "termination": report.get("termination") or None,
        "opening": {"eco": op.get("eco"), "name": op.get("name"),
                    "left_known_theory_at": None if left is None else f"move {left // 2 + 1} ({'White' if left % 2 == 0 else 'Black'})"},
        "players": {"white": side("white"), "black": side("black")},
        "key_moments": [{"ply": k["ply"], "move": k["label"], "by": by(k["color"]), "class": k["class"],
                         "winning_chances_lost_pct": round(k["loss"]), "better_was": k["best_san"], "engine_line": " ".join(k["line"]),
                         "strongest_reply_after_it": k["reply"], "reasons": k["tags"], "phase": k["phase"]} for k in report["key_moments"]],
        "great_moves": [f"{m['move_number']}{'.' if m['color'] == 'white' else '...'}{m['san']} ({m['class']}, {m['color']})"
                        for m in report["moves"] if m["class"] in ("brilliant", "great")][:6],
        "turning_point": (report.get("turning_point") or {}).get("label"),
        "verdict": report.get("verdict"),
        "moves": len(report["moves"]),
    }


def _grounding(report: dict) -> Grounding:
    g = Grounding()
    parts = [m["san"] for m in report["moves"]] + [m["best_san"] or "" for m in report["moves"]]
    for k in report["key_moments"]:
        parts += k["line"] + [k["reply"] or "", k["label"]]
    g.add_context(" ".join(parts))
    return g


def _all_text(r: AIReview) -> str:
    return " ".join([r.headline, r.summary, r.opening, r.middlegame, r.endgame, *r.strengths,
                     *(f"{x.title} {x.detail}" for x in r.lessons), *(f"{x.task} {x.why}" for x in r.training)])


def _unverified(g: Grounding, r: AIReview) -> set[str]:
    """Moves and evaluations in the write-up that the engine's facts don't contain."""
    problems = g.check(_all_text(r))
    return set(problems["moves"]) | set(problems["evals"])


def _parse(text: str) -> AIReview:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON")
    return AIReview.model_validate(json.loads(text[start:end + 1]))


_SENT = re.compile(r"(?<=[.!?])\s+")


def _drop_bad(r: AIReview, bad: set[str]) -> AIReview:
    """Remove every sentence that mentions a move (or evaluation) we couldn't verify."""
    def clean(text: str) -> str:
        return " ".join(s for s in _SENT.split(text) if not any(re.search(rf"(?<![A-Za-z0-9]){re.escape(b)}", s) for b in bad)).strip()
    d = r.model_dump()
    for k in ("headline", "summary", "opening", "middlegame", "endgame"):
        d[k] = clean(d[k])
    d["strengths"] = [clean(s) for s in d["strengths"] if clean(s)]
    d["lessons"] = [x | {"detail": clean(x["detail"])} for x in d["lessons"] if clean(x["title"]) and clean(x["detail"])]
    d["training"] = [x for x in d["training"] if clean(x["task"])]
    if not d["headline"]:
        d["headline"] = "Your game, reviewed"
    return AIReview.model_validate(d)


def write(report: dict, backends: list[Backend], http=None) -> tuple[dict, int]:
    """(review, tokens used). Tries each model in turn; the built-in coach if none works."""
    fx = facts(report)
    prompt = "Facts (from Stockfish):\n" + json.dumps(fx, ensure_ascii=False)
    g = _grounding(report)
    used = 0
    for backend in backends:
        try:
            text, t = json_completion(backend, SYSTEM, prompt, http=http)
            used += t
            rev = _parse(text)
            bad = _unverified(g, rev)
            if bad:
                fix = (prompt + "\n\nYour previous draft mentioned moves or evaluations that are not in the facts: " + ", ".join(sorted(bad))
                       + ". Rewrite the whole JSON using only moves that appear in the facts, and no numeric evaluations.")
                text, t = json_completion(backend, SYSTEM, fix, http=http)
                used += t
                rev = _parse(text)
                bad = _unverified(g, rev)
            if bad:
                rev = _drop_bad(rev, bad)
            return rev.model_dump() | {"source": "ai", "model": backend.label, "grounded": True, "removed": sorted(bad)}, used
        except (LLMError, ValidationError, ValueError):
            continue
    return builtin(report) | {"source": "built-in", "model": None, "grounded": True, "removed": []}, used


# ---- the built-in coach: the same sections, written from the facts -------------------------------------------
LESSONS = {
    "allowed_mate": ("Check your king before every move",
                     "{label} let your opponent force mate. Before you move, look at every check your opponent could give next."),
    "missed_mate": ("Look for checkmate first",
                    "At {label} there was a forced mate. When the enemy king is short of squares, try every check before anything else."),
    "hanging_piece": ("Ask what your move leaves unprotected",
                      "After {label}, {reply} won material. Before you let go of a piece, check what your opponent's next move can take."),
    "missed_tactic": ("Checks, captures and threats first",
                      "At {label} the forcing {best} was stronger. Look at the forcing moves before the quiet ones."),
    "let_slip": ("Finish the job when you're winning",
                 "At {label} you were winning and it slipped. When ahead, trade pieces, remove the opponent's threats, and keep it simple."),
    "missed_punish": ("Punish your opponent's mistakes",
                      "Your opponent's move before {label} was a mistake, and {best} would have punished it. "
                      "When the other side errs, stop and look for the refutation."),
    "time_trouble": ("Save time for the critical moments",
                     "{label} came in time trouble. Spend less on routine moves so you have time when the position gets sharp."),
}
GRADE_WORDS = {"A": "excellent", "B": "good", "C": "decent but uneven", "D": "shaky", "F": "where most of the damage was done"}


def builtin(report: dict) -> dict:
    you = report.get("you")
    sides = report["sides"]
    me = you or ("white" if (sides["white"]["accuracy"] or 0) <= (sides["black"]["accuracy"] or 0) else "black")
    opp = "black" if me == "white" else "white"
    s, o = sides[me], sides[opp]
    who = "You" if you else me.capitalize()
    res = report.get("result")
    outcome = ("drew" if res == "1/2-1/2" else "won" if (res == "1-0") == (me == "white") else "lost") if res in ("1-0", "0-1", "1/2-1/2") else "played"
    mine = [k for k in report["key_moments"] if k["color"] == me]
    worst = max(mine, key=lambda k: k["loss"], default=None)
    summary = [f"{who} played {me.capitalize()} and {outcome}."]
    if s["accuracy"] is not None:
        summary.append(f"Accuracy {s['accuracy']}% (grade {s['grade']}) against {o['accuracy']}% for {'your opponent' if you else opp.capitalize()}.")
    if worst:
        summary.append(f"The costliest moment was {worst['label']}, which gave away {round(worst['loss'])}% of the winning chances"
                       + (f"; {worst['best_san']} was the move." if worst["best_san"] else "."))
    great = [m for m in report["moves"] if m["color"] == me and m["class"] in ("brilliant", "great")]
    if great:
        g = great[0]
        summary.append(f"There were good moments too, like {g['move_number']}{'.' if g['color'] == 'white' else '...'}{g['san']}.")
    op = report["opening"]
    phases = {}
    for p in ("opening", "middlegame", "endgame"):
        ph = s["phases"][p]
        if not ph["moves"]:
            phases[p] = ""
            continue
        text = f"{p.capitalize()}: {ph['accuracy']}% accuracy" + (f", grade {ph['grade']} ({GRADE_WORDS[ph['grade']]})." if ph["grade"] else ".")
        if p == "opening" and op.get("name"):
            lb = op.get("left_book")
            text = f"{op['name']} ({op['eco']})" + (f", in known theory until move {lb // 2 + 1}. " if lb is not None else ". ") + text
        bad = [k for k in mine if k["phase"] == p]
        if bad:
            text += f" The key slip here: {bad[0]['label']}."
        phases[p] = text
    strengths = []
    if great:
        strengths.append(f"Found {len(great)} strong move{'s' if len(great) > 1 else ''} the engine rates as the only good choice.")
    best_phase = max((p for p in s["phases"] if s["phases"][p]["grade"]), key=lambda p: s["phases"][p]["accuracy"] or 0, default=None)
    if best_phase and s["phases"][best_phase]["grade"] in ("A", "B"):
        strengths.append(f"A solid {best_phase}: {s['phases'][best_phase]['accuracy']}% accuracy.")
    if not s["counts"].get("blunder"):
        strengths.append("No blunders.")
    lessons = []
    for k in sorted(mine, key=lambda k: -k["loss"])[:3]:
        tag = next((t for t in k["tags"] if t in LESSONS), None)
        title, tpl = LESSONS.get(tag, ("Compare your candidate moves",
                                       "At {label}, {best} was better. Before committing, compare two or three moves and check your opponent's best reply."))
        lessons.append({"title": title, "detail": tpl.format(label=k["label"], reply=k["reply"] or "the reply", best=k["best_san"] or "another move"),
                        "ply": k["ply"]})
    if not lessons:
        lessons.append({"title": "Keep it up", "detail": "No big mistakes in this game. Review the closest calls on the graph to find the small gains.",
                        "ply": None})
    tags = {t for k in mine for t in k["tags"]}
    training = []
    if tags & {"hanging_piece", "missed_tactic", "missed_mate", "allowed_mate"}:
        training.append({"task": "Solve today's puzzles predicted for you", "why": "They target the kind of move you missed here.", "feature": "predicted"})
    if "time_trouble" in tags or "let_slip" in tags:
        training.append({"task": "Drill the critical moments you played too fast", "why": "Recognising when to think long saves games like this.",
                         "feature": "think"})
    weak = [p for p in ("opening", "middlegame", "endgame") if s["phases"][p]["grade"] in ("D", "F")]
    if weak:
        training.append({"task": f"Work on your {weak[0]}", "why": "It was the weakest phase of this game.",
                         "feature": "prepare" if weak[0] == "opening" else "mistakes"})
    if not training:
        training.append({"task": "Review your own mistakes as puzzles", "why": "Turning slips into habits is the fastest way to improve.", "feature": "mistakes"})
    return {"headline": report.get("verdict") or "Your game, reviewed", "summary": " ".join(summary), **phases,
            "strengths": strengths[:3], "lessons": lessons, "training": training[:3]}
