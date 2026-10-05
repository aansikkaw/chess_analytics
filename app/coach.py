"""The coach agent.

An LLM (Claude via the Anthropic API) acts as the orchestrator. It can't see
the player's data directly: it has to call tools, and every concrete chess
claim it makes must come from a tool result. That is the guard against the
classic failure of chess chatbots, confidently inventing moves.

With no ANTHROPIC_API_KEY set, `Coach` falls back to a rule-based coach that
answers from the same tools, so the app is fully usable offline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import chess
import chess.engine
import httpx

from .analysis import GameAnalysis, MoveAnalysis, win_percent
from .engine import Engine
from .llm import Backend, LLMError, openai_chat, openai_tool_specs, parse_tool_args
from .profile import SKILLS, insights, rating_dna, weekly_plan

MAX_AGENT_STEPS = 6
MAX_HISTORY_TURNS = 10
OPENAI_TIMEOUT = httpx.Timeout(300.0, connect=10.0)  # local CPU models can be slow
DEEP_LIMIT = chess.engine.Limit(depth=18, time=0.6)


class ToolError(Exception):
    pass


def _pawns(cp: int) -> str:
    if abs(cp) >= 9000:
        return "mate" if cp > 0 else "getting mated"
    return f"{cp / 100:+.1f}"


class CoachTools:
    """Everything the coach is allowed to know, as callable tools."""

    SPECS: list[dict] = [
        {
            "name": "get_rating_dna",
            "description": "The player's Rating DNA: overall rating and accuracy, plus a per-skill rating "
                           "(openings, middlegame, endgames, tactics, time management, converting wins, defending), "
                           "with share of moves and share of lost winning chances in each.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_insights",
            "description": "Ranked plain-language findings about the player's games (weakest skill, conversion rate, "
                           "time-trouble blunder rate, most common mistake pattern, weakest opening).",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_critical_moments",
            "description": "The player's most costly moves, biggest first. Each has the position (FEN), the move played, "
                           "the engine's best move and line, evals before/after, phase, clock and pattern tags. "
                           "Optionally filter to one skill.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "enum": list(SKILLS.keys())},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
            },
        },
        {
            "name": "analyse_position",
            "description": "Run Stockfish on a position. Returns the evaluation and best line in SAN. If `move` is given "
                           "(SAN or UCI), also evaluates that move and how much worse it is than the best one. "
                           "Use this to verify ANY concrete move or line before mentioning it.",
            "input_schema": {
                "type": "object",
                "properties": {"fen": {"type": "string"}, "move": {"type": "string"}},
                "required": ["fen"],
            },
        },
        {
            "name": "get_study_plan",
            "description": "The player's 7-day study plan built from their weakest skills and due puzzles.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]

    def __init__(self, games: list[GameAnalysis], engine: Engine | None, due_puzzles: int = 0, rating_override: int | None = None):
        self.games = games
        self.engine = engine
        self.due_puzzles = due_puzzles
        self.dna = rating_dna(games, rating_override)
        self._opponent = {g.game_id: g.opponent for g in games}

    # -- tools ------------------------------------------------------------
    def get_rating_dna(self) -> dict:
        return self.dna

    def get_insights(self) -> list[dict]:
        return insights(self.games, self.dna)

    def get_study_plan(self) -> dict:
        return weekly_plan(self.dna, self.due_puzzles)

    def opponent_of(self, game_id: str) -> str:
        return self._opponent.get(game_id, "your opponent")

    def critical_moves(self, skill: str | None = None, limit: int = 5) -> list[MoveAnalysis]:
        moves = [m for g in self.games for m in g.moves if m.classification in ("mistake", "blunder")]
        if skill:
            if skill not in SKILLS:
                raise ToolError(f"Unknown skill '{skill}'. Use one of: {', '.join(SKILLS)}.")
            moves = [m for m in moves if SKILLS[skill][1](m)]
        return sorted(moves, key=lambda m: m.win_loss, reverse=True)[: max(1, min(limit, 10))]

    def list_critical_moments(self, skill: str | None = None, limit: int = 5) -> list[dict]:
        return [self._moment(m) for m in self.critical_moves(skill, limit)]

    def _moment(self, m: MoveAnalysis) -> dict:
        return {
            "game_id": m.game_id,
            "opponent": self._opponent.get(m.game_id, "?"),
            "move": f"{m.move_number}{'.' if m.color == 'white' else '...'}",
            "you_played": m.played_san,
            "engine_best": m.best_san,
            "engine_line": m.best_line,
            "eval_before": _pawns(m.cp_before),
            "eval_after": _pawns(m.cp_after),
            "winning_chances_lost": m.win_loss,
            "classification": m.classification,
            "phase": m.phase,
            "clock_seconds": m.clock_before,
            "patterns": m.tags,
            "fen": m.fen_before,
        }

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
        result: dict[str, Any] = {
            "side_to_move": "white" if side == chess.WHITE else "black",
            "eval_for_side_to_move": _pawns(best.cp_for(side)),
            "best_line": line,
        }
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
            after = self.engine.evaluate(board, DEEP_LIMIT)
            cp_after = after.cp_for(side)
            result["move"] = {
                "san": san,
                "eval_after": _pawns(cp_after),
                "winning_chances_lost": round(max(0.0, win_percent(best.cp_for(side)) - win_percent(cp_after)), 1),
            }
        return result

    def run(self, name: str, args: dict) -> Any:
        if name == "get_rating_dna":
            return self.get_rating_dna()
        if name == "get_insights":
            return self.get_insights()
        if name == "list_critical_moments":
            return self.list_critical_moments(args.get("skill"), int(args.get("limit", 5)))
        if name == "analyse_position":
            return self.analyse_position(args["fen"], args.get("move"))
        if name == "get_study_plan":
            return self.get_study_plan()
        raise ToolError(f"Unknown tool {name}")


SYSTEM_PROMPT = """You are a chess coach for {username}, rated about {rating}. You coach from their own games.

Rules:
- Look before you speak: call tools to get the player's data. Never guess their stats.
- Every concrete move, line or evaluation you mention must come from a tool result. If you want to
  suggest a move that isn't in a result yet, call analyse_position first. Never invent variations.
- Explain the *why* in human terms (plans, piece activity, king safety, pawn structure, calculation
  habits), pitched at a {rating} player. Engine numbers support the explanation; they aren't the explanation.
- Refer to specific games by opponent and move number when you use a critical moment.
- End with one concrete thing to do next.
- Keep answers under 180 words unless asked for more. Use SAN for moves."""


@dataclass
class CoachReply:
    reply: str
    mode: str  # "agent", "offline", or "fallback" (LLM failed, offline answer given)
    tools_used: list[str] = field(default_factory=list)


def clean_history(history: list[dict] | None) -> list[dict]:
    """Keep only well-formed, alternating text turns that start with the user."""
    out: list[dict] = []
    for turn in (history or [])[-2 * MAX_HISTORY_TURNS:]:
        role, content = turn.get("role"), turn.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": content[:4000]}  # keep the latest of a repeated role
        else:
            out.append({"role": role, "content": content[:4000]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    if out and out[-1]["role"] == "user":  # the new message will be the user turn
        out.pop()
    return out


def context_brief(tools: CoachTools) -> str:
    """Verified facts handed to smaller open models up front.

    Small local models are unreliable at deciding to call tools, so we give them
    the essentials directly. Tools stay available for anything deeper.
    """
    d = tools.dna
    skills = ", ".join(
        f"{s['label']} {s['rating']}" + (" (few moves, low confidence)" if s["low_confidence"] else "") for s in d["skills"]
    )
    insights_ = "\n".join(f"- {i['stat']} {i['text']}" for i in tools.get_insights()[:4]) or "- none yet"
    moments = "\n".join(
        f"- vs {m['opponent']}, move {m['move']} {m['you_played']}: eval {m['eval_before']} -> {m['eval_after']}; "
        f"engine preferred {m['engine_best']} (line: {' '.join(m['engine_line'][:4])}); patterns: {', '.join(m['patterns']) or 'none'}"
        for m in tools.list_critical_moments(limit=3)
    ) or "- none"
    return (
        "\n\nVerified data about this player (from Stockfish analysis of their games):\n"
        f"Overall: {d['base_rating']} rating, {d['overall_accuracy']}% accuracy over {d['games']} games.\n"
        f"Skill ratings: {skills}.\nKey findings:\n{insights_}\nCostliest moments:\n{moments}\n"
        "Use only these facts or tool results. You may call tools for more detail."
    )


class Coach:
    """Routes a chat message to the configured LLM backend, or the offline coach.

    If the LLM fails (network, rate limit, missing model), the user still gets an
    answer from the offline coach plus a one-line note saying why.
    """

    def __init__(self, username: str, tools: CoachTools, backend: Backend | None, http: httpx.Client | None = None):
        self.username = username
        self.tools = tools
        self.backend = backend
        self._http = http
        self._client = None  # Anthropic SDK client, created lazily

    def chat(self, message: str, history: list[dict] | None = None) -> CoachReply:
        message = (message or "").strip()[:2000]
        if not message:
            return CoachReply("Ask me anything about your games.", "offline")
        if self.backend is None:
            return OfflineCoach(self.tools).answer(message)
        try:
            if self.backend.kind == "anthropic":
                return self._anthropic_agent(message, clean_history(history))
            return self._openai_agent(message, clean_history(history))
        except Exception as exc:  # noqa: BLE001 - any provider failure falls back to the offline coach
            fallback = OfflineCoach(self.tools).answer(message)
            reason = str(exc) if isinstance(exc, LLMError) else f"{type(exc).__name__}: {str(exc)[:160]}"
            reason = reason.rstrip(". ") + "."
            fallback.reply = f"(AI coach unavailable: {reason} Showing the built-in coach instead.)\n\n{fallback.reply}"
            fallback.mode = "fallback"
            return fallback

    def _system(self) -> str:
        return SYSTEM_PROMPT.format(username=self.username, rating=self.tools.dna["base_rating"])

    def _run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            return json.dumps(self.tools.run(name, args), default=str), False
        except (ToolError, KeyError, ValueError, TypeError) as exc:
            return f"Tool error: {exc}", True

    # ---- Claude ----------------------------------------------------------
    def _anthropic_agent(self, message: str, history: list[dict]) -> CoachReply:
        if self._client is None:
            import anthropic  # imported lazily so other providers need no SDK

            self._client = anthropic.Anthropic(api_key=self.backend.api_key)
        messages: list[dict] = history + [{"role": "user", "content": message}]
        used: list[str] = []
        for _ in range(MAX_AGENT_STEPS):
            resp = self._client.messages.create(
                model=self.backend.model, max_tokens=1200, system=self._system(), tools=CoachTools.SPECS, messages=messages
            )
            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if b.type == "text").strip()
                return CoachReply(text or "I couldn't form an answer to that.", "agent", used)
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                used.append(block.name)
                content, is_error = self._run_tool(block.name, block.input or {})
                result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                if is_error:
                    result["is_error"] = True
                results.append(result)
            messages.append({"role": "user", "content": results})
        return CoachReply("That needed more digging than I can do in one answer. Try a narrower question.", "agent", used)

    # ---- OpenAI-compatible (Ollama, Groq, Gemini, OpenRouter, ...) ---------
    def _openai_agent(self, message: str, history: list[dict]) -> CoachReply:
        http = self._http or httpx.Client(timeout=OPENAI_TIMEOUT)
        tools = openai_tool_specs(CoachTools.SPECS)
        messages: list[dict] = [{"role": "system", "content": self._system() + context_brief(self.tools)}]
        messages += history + [{"role": "user", "content": message}]
        used: list[str] = []
        try:
            for _ in range(MAX_AGENT_STEPS):
                msg = openai_chat(self.backend, messages, tools, http)
                calls = msg.get("tool_calls") or []
                if not calls:
                    text = (msg.get("content") or "").strip()
                    return CoachReply(text or "I couldn't form an answer to that.", "agent", used)
                messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for i, call in enumerate(calls):
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    used.append(name)
                    content, _ = self._run_tool(name, parse_tool_args(fn.get("arguments")))
                    messages.append({"role": "tool", "tool_call_id": call.get("id") or f"call_{i}", "content": content})
        finally:
            if self._http is None:
                http.close()
        return CoachReply("That needed more digging than I can do in one answer. Try a narrower question.", "agent", used)


class OfflineCoach:
    """Keyword-routed answers built from the same tools. No LLM needed."""

    INTENTS = [
        ("plan", re.compile(r"\b(study|plan|work on|improve|practice|train)", re.I)),
        ("time", re.compile(r"\b(time|clock|flag|blitz)", re.I)),
        ("converting", re.compile(r"\b(convert|winning|won position|throw|blow)", re.I)),
        ("openings", re.compile(r"\bopening", re.I)),
        ("endgames", re.compile(r"\bend ?game", re.I)),
        ("tactics", re.compile(r"\b(tactic|calculat|combination)", re.I)),
        ("middlegame", re.compile(r"\bmiddle ?game", re.I)),
        ("moment", re.compile(r"\b(worst|biggest|mistake|blunder|why|lose|losing|lost)", re.I)),
    ]

    def __init__(self, tools: CoachTools):
        self.t = tools

    def answer(self, message: str) -> CoachReply:
        intent = next((name for name, rx in self.INTENTS if rx.search(message)), "summary")
        handler = getattr(self, f"_{intent}", None) or (lambda: self._skill(intent))
        return CoachReply(handler(), "offline", [intent])

    def _skill_row(self, key: str) -> dict | None:
        return next((s for s in self.t.dna["skills"] if s["key"] == key), None)

    def _explain(self, m: MoveAnalysis) -> str:
        opp = self.t.opponent_of(m.game_id)
        dots = "." if m.color == "white" else "..."
        text = (f"Against {opp}, move {m.move_number}{dots} {m.played_san}: you were at {_pawns(m.cp_before)} "
                f"and dropped to {_pawns(m.cp_after)} ({m.win_loss:.0f} points of winning chances).")
        if m.best_san:
            line = " ".join(m.best_line[:4])
            text += f" The engine wanted {m.best_san}" + (f" ({line})." if line else ".")
        hints = {
            "hanging_piece": " Your opponent's best reply captures a piece: run a blunder check on every loose piece before you move.",
            "missed_tactic": " There was a forcing move (check, capture or promotion) available. Look at those first, every move.",
            "time_trouble": f" You had about {int(m.clock_before or 0)}s left. Clock management cost you here, not knowledge.",
            "conversion": " You were already better: simplify, trade pieces, and remove counterplay before going for more.",
            "missed_mate": " There was a forced mate on the board.",
        }
        for tag in m.tags:
            if tag in hints:
                text += hints[tag]
                break
        return text

    def _summary(self) -> str:
        ins = self.t.get_insights()
        d = self.t.dna
        head = f"From {d['games']} games: overall accuracy {d['overall_accuracy']}% around a {d['base_rating']} rating."
        if ins:
            head += f" The headline: {ins[0]['stat']} {ins[0]['text']}"
        return head + " Ask me what to study, why you lose won games, or about your openings, endgames or clock."

    def _plan(self) -> str:
        plan = self.t.get_study_plan()
        day1 = "; ".join(f"{i['task']} ({i['minutes']} min)" for i in plan["days"][0]["items"])
        focus = " and ".join(plan["focus"]) or "general play"
        return f"This week, focus on {focus}. Day 1: {day1}. The full 7-day plan is in the Plan tab."

    @staticmethod
    def _caveat(row: dict) -> str:
        if not row.get("low_confidence"):
            return ""
        return " That's too few moves to trust yet; import more games for a reliable read."

    def _time(self) -> str:
        row = self._skill_row("time")
        moves = self.t.critical_moves("time", 1)
        if not row:
            return "Your games don't include clock data, so I can't judge time management. Lichess imports include it."
        txt = f"Under time pressure your play rates about {row['rating']} ({row['moves']} moves)." + self._caveat(row)
        if moves:
            txt += " Costliest example: " + self._explain(moves[0])
        return txt + " Try a reserve rule: never drop below 20% of your clock before move 30."

    def _converting(self) -> str:
        failed = [g for g in self.t.games if g.conversion_failure]
        had = [g for g in self.t.games if g.peak_cp >= 300]
        txt = f"You reached +3 or better in {len(had)} games and didn't win {len(failed)} of them."
        moves = self.t.critical_moves("converting", 1)
        if moves:
            txt += " " + self._explain(moves[0])
        return txt

    def _moment(self) -> str:
        moves = self.t.critical_moves(None, 1)
        return self._explain(moves[0]) if moves else "I didn't find any big mistakes in these games. Nice."

    def _skill(self, key: str) -> str:
        row = self._skill_row(key)
        if not row:
            return self._summary()
        d = self.t.dna
        txt = (f"{row['label']}: about {row['rating']} vs your overall {d['base_rating']}, "
               f"{row['accuracy']}% accuracy over {row['moves']} moves.") + self._caveat(row)
        moves = self.t.critical_moves(key, 1)
        if moves:
            txt += " Costliest example: " + self._explain(moves[0])
        return txt
