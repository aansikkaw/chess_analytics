"""Structured coach answers, validated with Pydantic.

The model doesn't get to reply in free text. It must finish by calling the `final_answer`
tool with three fields, and only that validated object is shown to the user:

    summary    - the direct answer, one to three sentences
    evidence   - up to four supporting points, each optionally linked to an OKF concept
    next_step  - one concrete thing to do

So reasoning, "let me check..." narration and tool chatter never reach the screen,
and every answer has the same professional shape. `clean_text` is a safety net for
the rare case where a model ignores the tool and answers in prose.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MAX_EVIDENCE = 4

_THINK_RE = re.compile(r"<(think|thinking|reasoning|analysis)>.*?</\1>", re.S | re.I)
_OPEN_THINK_RE = re.compile(r"^.*?</(think|thinking|reasoning|analysis)>", re.S | re.I)  # unclosed opener
_META_LINE_RE = re.compile(
    r"^\s*(let me|let's|i will|i'll|i need to|i should|first,? i|now i|okay[,.]|ok[,.]|alright[,.]|we need to|"
    r"the user (asks|wants|is asking)|i'm going to|i am going to|based on the tool|using the tools?|calling|looking at the data)\b",
    re.I,
)


def strip_reasoning(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    if re.search(r"</(think|thinking|reasoning|analysis)>", text, re.I):
        text = _OPEN_THINK_RE.sub("", text)
    return text.strip()


def clean_text(text: str) -> str:
    """Fallback for prose answers: drop reasoning blocks and lines that narrate the process."""
    lines = [ln for ln in strip_reasoning(text).splitlines() if not _META_LINE_RE.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", strip_reasoning(str(text))).strip()


class Evidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    point: str = Field(..., min_length=3, max_length=400, description="One supporting fact, with any moves and evaluations exactly as read.")
    concept_id: str | None = Field(None, description="The OKF concept this point comes from, e.g. /moments/abc-56 or /skills/endgames.")
    label: str | None = Field(None, max_length=80, description="Short link text, e.g. '29...hxg6 vs bob'.")

    @field_validator("point", "label", mode="before")
    @classmethod
    def _tidy(cls, v):
        return _one_line(v) if isinstance(v, str) else v

    @field_validator("concept_id", mode="before")
    @classmethod
    def _norm_id(cls, v):
        if not v or not isinstance(v, str):
            return None
        v = v.strip().removesuffix(".md")
        return v if v.startswith("/") else "/" + v


DEFAULT_NEXT_STEP = "Do your due puzzles in Train, then ask me about one of the positions above."


class CoachAnswer(BaseModel):
    """What the user sees. Lenient where it can be (trims lists), strict where it matters."""

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(..., min_length=10, max_length=900, description="The direct answer in one to three sentences.")
    evidence: list[Evidence] = Field(default_factory=list, description=f"Up to {MAX_EVIDENCE} supporting points.")
    next_step: str = Field(DEFAULT_NEXT_STEP, min_length=5, max_length=400, description="One concrete thing to do next.")

    @field_validator("summary", "next_step", mode="before")
    @classmethod
    def _tidy(cls, v):
        return strip_reasoning(str(v)).strip() if v is not None else v

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, v):
        if v is None:
            return []
        if isinstance(v, str):  # some models send one string
            v = [ln.lstrip("-*• ").strip() for ln in v.splitlines() if ln.strip()]
        out = []
        for item in list(v)[:MAX_EVIDENCE]:
            out.append({"point": item} if isinstance(item, str) else item)
        return out

    @model_validator(mode="after")
    def _no_process_talk(self):
        if _META_LINE_RE.match(self.summary):
            raise ValueError("summary must state the answer directly, not describe what you're doing")
        return self

    def to_markdown(self) -> str:
        parts = [self.summary]
        if self.evidence:
            parts.append("\n".join(
                f"- {e.point}" + (f" ([{e.label or 'details'}]({e.concept_id}.md))" if e.concept_id else "") for e in self.evidence
            ))
        parts.append(f"**Next step:** {self.next_step}")
        return "\n\n".join(parts)


FINAL_ANSWER_SPEC = {
    "name": "final_answer",
    "description": "Your answer to the player. Call once, when done. No reasoning or process talk.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Direct answer, 1-3 sentences."},
            "evidence": {
                "type": "array",
                "description": f"Up to {MAX_EVIDENCE} supporting points.",
                "items": {
                    "type": "object",
                    "properties": {
                        "point": {"type": "string"},
                        "concept_id": {"type": "string", "description": "e.g. /moments/abc-56"},
                        "label": {"type": "string", "description": "short link text"},
                    },
                    "required": ["point"],
                },
            },
            "next_step": {"type": "string", "description": "One concrete action."},
        },
        "required": ["summary"],  # next_step has a default: a missing field shouldn't get a whole answer rejected
    },
}


def parse_answer(args: dict) -> CoachAnswer:
    """Validate final_answer arguments. Raises ValueError with a model-readable message."""
    try:
        return CoachAnswer.model_validate(args or {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'answer'}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"final_answer was invalid ({problems}). Fix it and call final_answer again.") from None


def answer_from_text(text: str, next_step: str = "Ask a follow-up question about one of the positions above.") -> CoachAnswer:
    """Wrap a prose answer (offline coach, or a model that ignored the tool) in the same structure."""
    body = clean_text(text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()] or ["I couldn't form an answer to that."]
    evidence = [Evidence.model_construct(point=_one_line(p), concept_id=None, label=None) for p in paras[1:1 + MAX_EVIDENCE]]
    return CoachAnswer.model_construct(summary=paras[0], evidence=evidence, next_step=next_step)
