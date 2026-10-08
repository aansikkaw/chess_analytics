"""The coach: navigates the player's OKF knowledge bundle, cites concepts, and is checked.

Knowledge comes from two OKF v0.2 bundles mounted together:
  /            the player's bundle (profile, skills, patterns, critical moments, games, ...)
  /knowledge   chess principles (hand-authored; unverified until a strong player reviews them)

How an answer is produced:
  1. Navigate. The model starts from a brief built from /player.md, then uses OKF-style
     tools: read_index (progressive disclosure), read_concept, search_concepts, find_moments
     (frontmatter query), plus analyse_position for live Stockfish checks.
  2. Cite. Claims about the player link to the concept they came from, as plain markdown
     links, e.g. [29...hxg6 vs bob](/moments/abc123-56.md).
  3. Verify. Every move, evaluation and concept link in the answer must appear in what was
     read. If not, the model gets one rewrite; anything still unverified is flagged.

With no LLM configured, a rule-based coach answers from the same bundle with the same links.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import chess
import chess.engine
import httpx

from .analysis import GameAnalysis, win_percent
from .answer import FINAL_ANSWER_SPEC, CoachAnswer, answer_from_text, clean_text, parse_answer
from .engine import Engine
from .grounding import Grounding, concept_links, repair_instruction, strip_unknown_links
from .llm import Backend, LLMError, openai_chat, openai_tool_specs, parse_tool_args
from .okf import Bundle, Concept
from .player_bundle import PRO_PREFIXES
from .profile import SKILLS, insights, rating_dna, weekly_plan

MAX_AGENT_STEPS = 8
MAX_REPAIRS = 1
MAX_HISTORY_TURNS = 10
OPENAI_TIMEOUT = httpx.Timeout(300.0, connect=10.0)  # local CPU models (Ollama) can be slow
HOSTED_TIMEOUT = httpx.Timeout(60.0, connect=10.0)  # hosted APIs: give up in time to try the fallback
DEEP_LIMIT = chess.engine.Limit(depth=18, time=0.6)
SEVERITY = {"inaccuracy": 0, "mistake": 1, "blunder": 2}


class ToolError(Exception):
    pass


def _clamp(value, default: int, lo: int, hi: int) -> int:
    """Models sometimes send limits out of range, as strings, or not at all."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


TOOL_RESULT_CHARS = 3500  # one tool result, as sent to the model (~900 tokens)
HISTORY_BUDGET_CHARS = 14000  # tool results kept in full per request; older ones get shortened beyond this
SHORTENED_CHARS = 500
_LLM_DROP = {"game_url", "trust", "stale", "resource", "played_uci", "best_uci"}  # UI-only fields the model doesn't need


def _lean(value):
    """Drop empty and UI-only fields so tool results cost fewer tokens."""
    if isinstance(value, dict):
        return {k: _lean(v) for k, v in value.items() if k not in _LLM_DROP and v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_lean(v) for v in value]
    return value


def compact_result(value, limit: int = TOOL_RESULT_CHARS) -> str:
    """Serialise a tool result compactly and keep it under `limit` characters while staying valid JSON."""
    value = _lean(value)
    dump = lambda v: v if isinstance(v, str) else json.dumps(v, default=str, separators=(",", ":"))  # noqa: E731
    text = dump(value)
    if len(text) <= limit:
        return text
    if isinstance(value, list):
        items = list(value)
        while len(items) > 1 and len(dump(items)) > limit:
            items.pop()
        text = dump(items)
    elif isinstance(value, dict) and isinstance(value.get("body"), str):
        over = len(text) - limit
        value = {**value, "body": value["body"][: max(200, len(value["body"]) - over - 20)] + " …"}
        text = dump(value)
    return text if len(text) <= limit else text[: limit - 15] + " …(truncated)"


def shrink_history(messages: list[dict], budget: int = HISTORY_BUDGET_CHARS) -> None:
    """Shorten older tool results once the conversation gets long (free tiers count every token re-sent).

    The newest results stay whole. Facts from shortened results are still known to the grounding
    check, and the model can re-read a concept if it needs the detail again.
    """
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    total = sum(len(m["content"]) for m in tool_msgs)
    for m in tool_msgs[:-2]:
        if total <= budget:
            break
        if len(m["content"]) > SHORTENED_CHARS:
            total -= len(m["content"]) - SHORTENED_CHARS
            m["content"] = m["content"][:SHORTENED_CHARS] + " …(earlier result shortened; read it again if you need it)"


def _pawns(cp: int) -> str:
    if abs(cp) >= 9000:
        return "mate" if cp > 0 else "getting mated"
    return f"{cp / 100:+.1f}"


SKILL_FILTERS = {
    "openings": {"phase": "opening"},
    "middlegame": {"phase": "middlegame"},
    "endgames": {"phase": "endgame"},
    "tactics": {"pattern": "missed_tactic"},
    "time": {"pattern": "time_trouble"},
    "converting": {"pattern": "conversion"},
    "defending": {"query": "defending"},
}


def moment_card(c: Concept) -> dict:
    """What the UI needs to show a Critical Moment on a board."""
    f = c.frontmatter
    return {"id": c.id, "type": c.type, "title": c.title, "opponent": f.get("opponent"), "date": f.get("date"),
            "opening": f.get("opening"), "move": f.get("move"), "you_played": f.get("played"), "engine_best": f.get("best"),
            "engine_line": f.get("line") or [], "eval_before": f.get("eval_before"), "eval_after": f.get("eval_after"),
            "phase": f.get("phase"), "patterns": f.get("patterns") or [], "clock_seconds": f.get("clock_seconds"),
            "fen": f.get("fen"), "game_url": f.get("resource"), "trust": c.trust,
            "played_uci": f.get("played_uci"), "best_uci": f.get("best_uci")}


class CoachTools:
    """Everything the coach is allowed to know, as callable tools over the OKF bundle."""

    SPECS: list[dict] = [
        {
            "name": "read_index",
            "description": "Read an OKF index.md: the list of concepts and subdirectories at a path, each with a one-line "
                           "description. Start at '/' (the player's bundle) or '/knowledge' (chess principles).",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "read_concept",
            "description": "Read one concept by id, e.g. '/skills/endgames', '/patterns/time_trouble', '/moments/abc123-56', "
                           "'/knowledge/principles/clock-management'. Returns frontmatter and body.",
            "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        },
        {
            "name": "search_concepts",
            "description": "Keyword search across concepts (title, description, tags, body). Optional `type` filter, e.g. "
                           "'Critical Moment', 'Skill Assessment', 'Mistake Pattern', 'Game', 'Opening Line', 'Principle'.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "type": {"type": "string"},
                                                             "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
                             "required": ["query"]},
        },
        {
            "name": "find_moments",
            "description": "Structured query over Critical Moment frontmatter, costliest first. Use for precise filters.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "phase": {"type": "string", "enum": ["opening", "middlegame", "endgame"]},
                    "pattern": {"type": "string", "enum": ["missed_tactic", "hanging_piece", "time_trouble", "conversion", "missed_mate"]},
                    "color": {"type": "string", "enum": ["white", "black"]},
                    "time_class": {"type": "string", "enum": ["bullet", "blitz", "rapid", "classical"]},
                    "opponent": {"type": "string"}, "opening": {"type": "string"},
                    "min_severity": {"type": "string", "enum": ["mistake", "blunder"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
            },
        },
        {
            "name": "analyse_position",
            "description": "Stockfish on a FEN: evaluation and best line. With `move`, also evaluates that move. "
                           "Use it before recommending any move you haven't read.",
            "input_schema": {"type": "object", "properties": {"fen": {"type": "string"}, "move": {"type": "string"}}, "required": ["fen"]},
        },
        {
            "name": "get_study_plan",
            "description": "The player's 7-day study plan from their weakest skills.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]

    def __init__(self, games: list[GameAnalysis], bundle: Bundle, engine: Engine | None, due_puzzles: int = 0,
                 rating_override: int | None = None, pro: bool = False):
        self.games = games
        self.bundle = bundle
        self.engine = engine
        self.due_puzzles = due_puzzles
        self.pro = pro
        self.dna = rating_dna(games, rating_override)
        self.specs = self.SPECS
        self.known = {cid for cid in bundle.concepts if self.visible(cid)}

    # -- access control: Pro concepts are invisible to Free users
    def visible(self, cid: str) -> bool:
        return self.pro or not cid.startswith(PRO_PREFIXES)

    def _concept(self, cid: str) -> Concept:
        c = self.bundle.get(cid)
        if c is None or not self.visible(c.id):
            raise ToolError(f"No concept '{cid}'. Use read_index or search_concepts to find valid ids.")
        return c

    # -- tools
    def read_index(self, path: str = "/") -> str:
        text = self.bundle.index(path or "/")
        if text is None:
            raise ToolError(f"No index at '{path}'.")
        if not self.pro:  # hide Pro entries from Free users
            text = "\n".join(ln for ln in text.splitlines() if not any(f"({p.strip('/')}" in ln or f"(/{p.strip('/')}" in ln
                                                                       for p in PRO_PREFIXES))
        return text

    def read_concept(self, id: str) -> dict:
        c = self._concept(id)
        return {"id": c.id, "frontmatter": c.frontmatter, "body": c.body, "trust": c.trust, "stale": c.stale}

    def search_concepts(self, query: str, type: str | None = None, limit: int = 6) -> list[dict]:
        limit = _clamp(limit, 6, 1, 10)
        hits = [c for c in self.bundle.search(query, type=type, limit=limit * 3) if self.visible(c.id)][:limit]
        return [c.summary() for c in hits]

    def find_moments(self, phase=None, pattern=None, color=None, time_class=None, opponent=None, opening=None,
                     min_severity="mistake", limit=5) -> list[dict]:
        floor = SEVERITY.get(min_severity or "mistake", 1)
        out = []
        for c in self.bundle.find(type="Critical Moment"):
            f = c.frontmatter
            if SEVERITY.get(f.get("classification"), 0) < floor:
                continue
            if phase and f.get("phase") != phase:
                continue
            if pattern and pattern not in (f.get("patterns") or []):
                continue
            if color and f.get("color") != color:
                continue
            if time_class and f.get("time_class") != time_class:
                continue
            if opponent and opponent.lower() not in str(f.get("opponent", "")).lower():
                continue
            if opening and opening.lower() not in str(f.get("opening", "")).lower():
                continue
            out.append(c)
        out.sort(key=lambda c: float(c.frontmatter.get("winning_chances_lost") or 0), reverse=True)
        return [moment_card(c) for c in out[: max(1, min(int(limit), 10))]]

    def get_study_plan(self) -> dict:
        return weekly_plan(self.dna, self.due_puzzles)

    def get_insights(self) -> list[dict]:
        return insights(self.games, self.dna)

    def critical(self, skill: str | None = None, limit: int = 5) -> list[dict]:
        if skill and skill not in SKILLS:
            raise ToolError(f"Unknown skill '{skill}'.")
        f = dict(SKILL_FILTERS.get(skill, {})) if skill else {}
        query = f.pop("query", None)
        if query:
            hits = [c for c in self.bundle.search(query, type="Critical Moment", limit=limit)]
            return [moment_card(c) for c in hits]
        return self.find_moments(limit=limit, **f)

    def analyse_position(self, fen: str, move: str | None = None) -> dict:
        if self.engine is None:
            raise ToolError("The engine isn't available on this server.")
        try:
            board = chess.Board(fen)
        except ValueError as exc:
            raise ToolError(f"Invalid FEN: {exc}") from exc
        if not board.is_valid():
            raise ToolError("That FEN isn't a legal chess position.")
        side = board.turn
        best = self.engine.evaluate(board, DEEP_LIMIT)
        line, b = [], board.copy()
        for uci in best.pv[:6]:
            mv = chess.Move.from_uci(uci)
            line.append(b.san(mv))
            b.push(mv)
        result: dict[str, Any] = {"side_to_move": "white" if side == chess.WHITE else "black",
                                  "eval_for_side_to_move": _pawns(best.cp_for(side)), "best_line": line}
        if move:
            try:
                mv = board.parse_san(move)
            except ValueError:
                try:
                    mv = chess.Move.from_uci(move)
                except ValueError as exc:
                    raise ToolError(f"Couldn't read the move '{move}'.") from exc
                if mv not in board.legal_moves:
                    raise ToolError(f"'{move}' is not legal in this position.") from None
            san = board.san(mv)
            board.push(mv)
            cp_after = self.engine.evaluate(board, DEEP_LIMIT).cp_for(side)
            result["move"] = {"san": san, "eval_after": _pawns(cp_after),
                              "winning_chances_lost": round(max(0.0, win_percent(best.cp_for(side)) - win_percent(cp_after)), 1)}
        return result

    def run(self, name: str, args: dict) -> Any:
        args = args or {}
        if name == "read_index":
            return self.read_index(args.get("path", "/"))
        if name == "read_concept":
            return self.read_concept(args["id"])
        if name == "search_concepts":
            return self.search_concepts(str(args.get("query", "")), args.get("type"), _clamp(args.get("limit"), 6, 1, 10))
        if name == "find_moments":
            clean = {k: v for k, v in args.items() if k in {"phase", "pattern", "color", "time_class", "opponent", "opening", "min_severity"}}
            return self.find_moments(**clean, limit=_clamp(args.get("limit"), 5, 1, 10))
        if name == "analyse_position":
            return self.analyse_position(args["fen"], args.get("move"))
        if name == "get_study_plan":
            return self.get_study_plan()
        raise ToolError(f"Unknown tool {name}")


SYSTEM_PROMPT = """You are a professional chess coach for {username}, rated about {rating}. You coach only from their own games.

Your knowledge is an OKF bundle (markdown concepts with frontmatter). The brief below is /player.md.
Gather facts with read_index, read_concept, search_concepts and find_moments; follow links between concepts.
Be economical: the brief often has enough to answer, so use as few tool calls as you need (usually none to two).

How to answer:
- When you have the facts, call final_answer exactly once. Never reply in plain text.
- final_answer.summary answers the question directly in one to three sentences. Never describe your process,
  your reasoning or the tools you used ("Let me...", "I found...", "Based on the data...").
- final_answer.evidence: up to four points, each tied to the concept it came from (concept_id, e.g. /moments/abc-56).
- final_answer.next_step: one concrete action for the player.

Rules (answers are automatically checked; unverified content is rejected):
- Every move, line and evaluation you mention must appear in something you read. To suggest a move you haven't
  read, call analyse_position on the FEN first. Never invent variations.
- For the *why*, use the principle concepts under /knowledge (general advice, unverified until reviewed).
- Pitch explanations at a {rating} player in a calm, professional tone. Use SAN for moves. Keep it brief."""

NUDGE = ("Reply by calling final_answer with summary, evidence and next_step. Do not write the answer as plain text, "
         "and do not include your reasoning.")


def context_brief(tools: CoachTools) -> str:
    """/player.md plus the costliest moments, so even small models start from real data."""
    player = tools.bundle.get("/player")
    text = player.body if player else ""
    if not tools.pro:
        text = "\n".join(ln for ln in text.splitlines() if "(Pro)" not in ln)
    moments = "\n".join(
        f"- [{m['move']} vs {m['opponent']}]({m['id']}.md): {m['eval_before']} -> {m['eval_after']}; engine preferred "
        f"{m['engine_best']} (line: {' '.join(m['engine_line'][:4])}); patterns: {', '.join(m['patterns']) or 'none'}"
        for m in tools.find_moments(limit=4)
    ) or "- none"
    return f"\n\n# Brief: /player.md\n\n{text}\n\n# Costliest moments\n\n{moments}"


@dataclass
class CoachReply:
    reply: str  # markdown, also used as chat history
    mode: str  # "agent", "offline", or "fallback" (LLM failed, offline answer given)
    tools_used: list[str] = field(default_factory=list)
    citations: dict = field(default_factory=dict)  # concept id -> card
    grounded: bool = True
    unverified: list[str] = field(default_factory=list)
    answer: dict | None = None  # the validated CoachAnswer the UI renders
    notice: str | None = None  # e.g. why the AI coach fell back
    model: str | None = None  # label of the model that answered


def clean_history(history: list[dict] | None) -> list[dict]:
    """Keep only well-formed, alternating text turns that start with the user."""
    out: list[dict] = []
    for turn in (history or [])[-2 * MAX_HISTORY_TURNS:]:
        role, content = turn.get("role"), turn.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": content[:4000]}
        else:
            out.append({"role": role, "content": content[:4000]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    if out and out[-1]["role"] == "user":
        out.pop()
    return out


@dataclass
class _Run:
    grounding: Grounding
    used: list[str] = field(default_factory=list)
    repairs: int = 0
    nudges: int = 0


class Coach:
    """Routes a chat message to the configured LLM, validates and verifies the answer, or uses the offline coach."""

    def __init__(self, username: str, tools: CoachTools, backend: Backend | list[Backend] | None, http: httpx.Client | None = None):
        self.username = username
        self.tools = tools
        # A list means "try these in order": e.g. Groq first, then a second provider if Groq is down or rate-limited.
        self.backends = [b for b in (backend if isinstance(backend, list) else [backend]) if b]
        self.backend = self.backends[0] if self.backends else None
        self._http = http
        self._client = None  # Anthropic SDK client, created lazily
        self.specs = tools.specs + [FINAL_ANSWER_SPEC]

    def chat(self, message: str, history: list[dict] | None = None) -> CoachReply:
        message = (message or "").strip()[:2000]
        if not message:
            return self._finish(CoachReply("Ask me anything about your games.", "offline"))
        if not self.backends:
            return self._finish(OfflineCoach(self.tools).answer(message))
        last_exc: Exception | None = None
        for i, backend in enumerate(self.backends):
            if i:  # a different provider needs its own client
                self._client = None
            self.backend = backend
            try:
                if backend.kind == "anthropic":
                    reply = self._anthropic_agent(message, clean_history(history))
                else:
                    reply = self._openai_agent(message, clean_history(history))
                reply.model = backend.label
                return self._finish(reply)
            except Exception as exc:  # noqa: BLE001 - try the next provider, then fall back to the offline coach
                last_exc = exc
        fallback = OfflineCoach(self.tools).answer(message)
        reason = (str(last_exc) if isinstance(last_exc, LLMError) else f"{type(last_exc).__name__}: {str(last_exc)[:160]}").rstrip(". ") + "."
        tried = " (tried both of your model providers)" if len(self.backends) > 1 else ""
        fallback.notice = f"AI coach unavailable{tried}: {reason} Showing the built-in coach instead."
        fallback.reply = f"({fallback.notice})\n\n{fallback.reply}"
        fallback.mode = "fallback"
        return self._finish(fallback)

    # ---- helpers ------------------------------------------------------------------------
    def _new_run(self) -> tuple[_Run, str]:
        g = Grounding(known_concepts=set(self.tools.known))
        brief = context_brief(self.tools)
        g.add_context(brief)
        return _Run(g), brief

    def _finish(self, reply: CoachReply) -> CoachReply:
        """Drop links to unknown concepts; make sure there's a structured answer; attach concept cards."""
        if reply.answer is None:
            reply.answer = answer_from_text(reply.reply).model_dump()
        known = self.tools.known
        reply.reply = strip_unknown_links(reply.reply, known)
        a = reply.answer
        a["summary"] = strip_unknown_links(a["summary"], known)
        for e in a.get("evidence") or []:
            if e.get("concept_id") and e["concept_id"] not in known:
                e["concept_id"] = None
        ids = [cid for _, cid in concept_links(reply.reply)] + [e["concept_id"] for e in a.get("evidence") or [] if e.get("concept_id")]
        for cid in dict.fromkeys(ids):
            c = self.tools.bundle.get(cid)
            if c and cid not in reply.citations:
                reply.citations[cid] = moment_card(c) if c.type == "Critical Moment" else {**c.summary(), "body": c.body[:1500]}
        return reply

    def _accept(self, ans: CoachAnswer, problems: dict, run: _Run) -> CoachReply:
        items = problems["moves"] + problems["evals"]
        text = ans.to_markdown()
        if items:
            text += f"\n\n⚠ Couldn't verify against your games or the engine: {', '.join(items)}. Treat these with caution."
        return CoachReply(text, "agent", list(run.used), grounded=not items, unverified=items, answer=ans.model_dump())

    def _try_final(self, args: dict, run: _Run) -> tuple[CoachReply | None, str]:
        """Validate a final_answer call, then ground it. Returns (accepted reply, message for the model)."""
        try:
            ans = parse_answer(args)
        except ValueError as exc:
            return None, str(exc)
        problems = run.grounding.check(ans.to_markdown())
        if problems["ok"] or run.repairs >= MAX_REPAIRS:
            return self._accept(ans, problems, run), "ok"
        run.repairs += 1
        return None, repair_instruction(problems) + " Then call final_answer again."

    def _from_prose(self, text: str, run: _Run) -> CoachReply:
        """Last resort when a model answers in prose despite the nudge: clean it, structure it, flag anything unverified."""
        ans = answer_from_text(text)
        return self._accept(ans, run.grounding.check(ans.to_markdown()), run)

    def _system(self, brief: str) -> str:
        return SYSTEM_PROMPT.format(username=self.username, rating=self.tools.dna["base_rating"]) + brief

    def _run_tool(self, name: str, args: dict, run: _Run) -> tuple[str, bool]:
        run.used.append(name)
        try:
            result = self.tools.run(name, args)
            run.grounding.add_context(json.dumps(result, default=str))  # grounding sees everything, even if trimmed
            content = compact_result(result)
            return content, False
        except (ToolError, KeyError, ValueError, TypeError) as exc:
            return f"Tool error: {exc}", True

    # ---- Claude --------------------------------------------------------------------------
    def _anthropic_agent(self, message: str, history: list[dict]) -> CoachReply:
        if self._client is None:
            import anthropic  # imported lazily so other providers need no SDK

            self._client = anthropic.Anthropic(api_key=self.backend.api_key)
        run, brief = self._new_run()
        messages: list[dict] = history + [{"role": "user", "content": message}]
        for _ in range(MAX_AGENT_STEPS):
            resp = self._client.messages.create(
                model=self.backend.model, max_tokens=1500, system=self._system(brief), tools=self.specs, messages=messages
            )
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                text = "".join(b.text for b in resp.content if b.type == "text").strip()
                if run.nudges < 1:
                    run.nudges += 1
                    messages += [{"role": "assistant", "content": text or "(no answer)"}, {"role": "user", "content": NUDGE}]
                    continue
                return self._from_prose(text, run)
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in tool_uses:
                if block.name == "final_answer":
                    reply, msg = self._try_final(block.input or {}, run)
                    if reply:
                        return reply
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": msg, "is_error": True})
                    continue
                content, is_error = self._run_tool(block.name, block.input or {}, run)
                result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                if is_error:
                    result["is_error"] = True
                results.append(result)
            messages.append({"role": "user", "content": results})
        return CoachReply("That needed more digging than I can do in one answer. Try a narrower question.", "agent", run.used)

    # ---- OpenAI-compatible (Groq, Gemini, OpenRouter, Ollama, ...) -------------------------
    def _openai_agent(self, message: str, history: list[dict]) -> CoachReply:
        http = self._http or httpx.Client(timeout=OPENAI_TIMEOUT if self.backend.kind == "ollama" else HOSTED_TIMEOUT)
        # With another provider still to try, don't sit out this one's rate limit: switch straight away.
        wait = self.backend is self.backends[-1]
        run, brief = self._new_run()
        tools = openai_tool_specs(self.specs)
        messages: list[dict] = [{"role": "system", "content": self._system(brief)}]
        messages += history + [{"role": "user", "content": message}]
        try:
            for _ in range(MAX_AGENT_STEPS):
                shrink_history(messages)
                msg = openai_chat(self.backend, messages, tools, http, wait_for_limits=wait)
                calls = msg.get("tool_calls") or []
                if not calls:
                    text = msg.get("content") or ""
                    if run.nudges < 1:
                        run.nudges += 1
                        messages += [{"role": "assistant", "content": clean_text(text) or "(no answer)"},
                                     {"role": "user", "content": NUDGE}]
                        continue
                    return self._from_prose(text, run)
                # Keep only the tool calls in history: any reasoning text the model emitted alongside is dropped.
                messages.append({"role": "assistant", "content": "", "tool_calls": calls})
                for i, call in enumerate(calls):
                    fn = call.get("function", {})
                    name, args = fn.get("name", ""), parse_tool_args(fn.get("arguments"))
                    call_id = call.get("id") or f"call_{i}"
                    if name == "final_answer":
                        reply, note = self._try_final(args, run)
                        if reply:
                            return reply
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": note})
                        continue
                    content, _ = self._run_tool(name, args, run)
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        finally:
            if self._http is None:
                http.close()
        return CoachReply("That needed more digging than I can do in one answer. Try a narrower question.", "agent", run.used)


class OfflineCoach:
    """Keyword-routed answers built from the same bundle, with the same concept links. No LLM needed."""

    INTENTS = [
        ("prep", re.compile(r"\b(prep|repertoire|chessbase|preparation)", re.I)),
        ("plan", re.compile(r"\b(study|plan|work on|improve|practice|train)", re.I)),
        ("time", re.compile(r"\b(time|clock|flag|blitz)", re.I)),
        ("converting", re.compile(r"\b(convert|winning|won position|throw|blow)", re.I)),
        ("openings", re.compile(r"\bopening", re.I)),
        ("endgames", re.compile(r"\bend ?game", re.I)),
        ("tactics", re.compile(r"\b(tactic|calculat|combination)", re.I)),
        ("middlegame", re.compile(r"\bmiddle ?game", re.I)),
        ("moment", re.compile(r"\b(worst|biggest|mistake|blunder|why|lose|losing|lost)", re.I)),
    ]
    HINTS = {
        "hanging_piece": ("Your opponent's best reply captures a piece.", "blunder-check"),
        "missed_tactic": ("There was a forcing move (check, capture or promotion) available.", "forcing-moves-first"),
        "time_trouble": ("You were short on time; clock management cost you here, not knowledge.", "clock-management"),
        "conversion": ("You were already better: reduce counterplay before going for more.", "converting-advantages"),
        "missed_mate": ("There was a forced mate on the board.", "mating-patterns"),
    }
    SKILL_CONCEPT = {"openings": "/skills/openings", "middlegame": "/skills/middlegame", "endgames": "/skills/endgames",
                     "tactics": "/skills/tactics", "time": "/skills/time", "converting": "/skills/converting",
                     "defending": "/skills/defending"}

    def __init__(self, tools: CoachTools):
        self.t = tools

    NEXT_STEPS = {
        "plan": "Start Day 1 of your plan in the Plan tab.",
        "time": "In your next game, keep 20% of your clock in reserve until move 30.",
        "converting": "Replay the linked position and find the move that keeps control before reading the engine line.",
        "openings": "Open the Openings tab and review the line you score worst in.",
        "prep": "Open the Prep tab and drill the positions where you left your preparation.",
        "endgames": "Solve today's endgame positions in the Puzzles tab.",
        "tactics": "Do your due puzzles, checking every check, capture and threat before moving.",
        "middlegame": "Pick the linked game and write down the plan you would choose at the critical moment.",
        "moment": "Solve this position in the Puzzles tab, then do a blunder check before every capture in your next game.",
        "defending": "Replay the linked game from the critical moment and look for active counterplay first.",
        "summary": "Ask what to study this week, or about one phase of the game.",
    }

    def answer(self, message: str) -> CoachReply:
        intent = next((name for name, rx in self.INTENTS if rx.search(message)), "summary")
        handler = getattr(self, f"_{intent}", None) or (lambda: self._skill(intent))
        text = handler()
        ans = answer_from_text(text, self.NEXT_STEPS.get(intent, self.NEXT_STEPS["summary"]))
        return CoachReply(ans.to_markdown(), "offline", [intent], answer=ans.model_dump())

    def _skill_row(self, key: str) -> dict | None:
        return next((s for s in self.t.dna["skills"] if s["key"] == key), None)

    def _principle(self, key: str) -> str:
        c = self.t.bundle.get(f"/knowledge/principles/{key}")
        return f" See [{c.title}]({c.id}.md)." if c else ""

    def _explain(self, m: dict) -> str:
        text = (f"[{m['move']} vs {m['opponent']}]({m['id']}.md): you were at {m['eval_before']} and dropped to {m['eval_after']}.")
        if m["engine_best"]:
            line = " ".join(m["engine_line"][:4])
            text += f" The engine wanted {m['engine_best']}" + (f" ({line})." if line else ".")
        for tag in m["patterns"]:
            if tag in self.HINTS:
                hint, principle = self.HINTS[tag]
                text += f" {hint}{self._principle(principle)}"
                break
        return text

    @staticmethod
    def _caveat(row: dict) -> str:
        return " That's too few moves to trust yet; import more games for a reliable read." if row.get("low_confidence") else ""

    def _summary(self) -> str:
        ins = self.t.get_insights()
        d = self.t.dna
        head = f"From {d['games']} games: overall accuracy {d['overall_accuracy']}% around a {d['base_rating']} rating ([profile](/player.md))."
        if ins:
            head += f" The headline: {ins[0]['stat']} {ins[0]['text']}"
        return head + " Ask me what to study, why you lose won games, or about your openings, endgames, clock or prep."

    def _plan(self) -> str:
        plan = self.t.get_study_plan()
        day1 = "; ".join(f"{i['task']} ({i['minutes']} min)" for i in plan["days"][0]["items"])
        focus = " and ".join(plan["focus"]) or "general play"
        return f"This week, focus on {focus}. Day 1: {day1}. The full 7-day plan is in the Plan tab."

    def _time(self) -> str:
        row = self._skill_row("time")
        if not row:
            return "Your games don't include clock data, so I can't judge time management."
        txt = f"Under time pressure your play rates about {row['rating']} ([details](/skills/time.md))." + self._caveat(row)
        moments = self.t.critical("time", 1)
        if moments:
            txt += " Costliest example: " + self._explain(moments[0])
        return txt + self._principle("clock-management")

    def _converting(self) -> str:
        failed = [g for g in self.t.games if g.conversion_failure]
        had = [g for g in self.t.games if g.peak_cp >= 300]
        txt = f"You reached +3 or better in {len(had)} games and didn't win {len(failed)} of them."
        moments = self.t.critical("converting", 1)
        return txt + (" " + self._explain(moments[0]) if moments else "")

    def _moment(self) -> str:
        moments = self.t.critical(None, 1)
        return self._explain(moments[0]) if moments else "I didn't find any big mistakes in these games. Nice."

    def _prep(self) -> str:
        if not self.t.pro:
            return "Comparing your ChessBase repertoire with your games is part of Pro (Prep tab)."
        checks = [c for c in self.t.bundle.find(type="Repertoire Check")]
        if not checks:
            return "Upload your ChessBase repertoire (exported as PGN) in the Prep tab, and I'll compare it with your games."
        c = checks[0]
        return f"{c.description} Details: [{c.title}]({c.id}.md).{self._principle('repertoire-maintenance')}"

    def _skill(self, key: str) -> str:
        row = self._skill_row(key)
        if not row:
            return self._summary()
        d = self.t.dna
        txt = (f"[{row['label']}]({self.SKILL_CONCEPT[key]}.md): about {row['rating']} vs your overall {d['base_rating']}, "
               f"{row['accuracy']}% accuracy over {row['moves']} moves.") + self._caveat(row)
        moments = self.t.critical(key, 1)
        return txt + (" Costliest example: " + self._explain(moments[0]) if moments else "")
