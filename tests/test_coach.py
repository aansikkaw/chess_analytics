"""The coach on OKF: tools, Pro visibility, concept-link citations, grounding and the repair loop."""

import json
from types import SimpleNamespace

import httpx
import pytest

from app import llm
from app.coach import Coach, CoachTools, OfflineCoach, ToolError, clean_history, compact_result, shrink_history
from app.config import load_settings
from app.grounding import Grounding, concept_links, moves_in
from app.llm import Backend, LLMError, openai_chat, parse_tool_args, rate_limit_wait, request_extras, resolve_backend
from tests.conftest import needs_engine


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Rate-limit waits are recorded, not slept."""
    waits = []
    monkeypatch.setattr(llm, "_sleep", waits.append)
    return waits


# ---- grounding ---------------------------------------------------------------------------
def test_moves_in_ignores_bare_squares_but_catches_claims():
    text = "The e4 pawn is weak. Play Nf3, then 12.d5 or 14...exd5 and O-O. Qxf7# ends it."
    assert moves_in(text) == {"Nf3", "d5", "exd5", "O-O", "Qxf7"}


def test_grounding_checks_moves_evals_and_links():
    g = Grounding(known_concepts={"/moments/abc-41", "/player"})
    g.add_context(json.dumps({"best": "Nf3", "line": ["Nf3", "d5", "exd5"], "eval_before": "+0.6"}))
    assert g.check("At [move 21](/moments/abc-41.md) Nf3 was right (+0.6), then 12.d5.")["ok"]
    bad = g.check("Qh5 wins at once (+4.7), see [this](/moments/zzz-10.md).")
    assert bad["moves"] == ["Qh5"] and bad["evals"] == ["+4.7"] and bad["links"] == ["/moments/zzz-10"]


def test_concept_links_only_bundle_paths():
    assert concept_links("[a](/x/y.md) and [b](https://lichess.org/x.md)") == [("a", "/x/y")]


# ---- tools over the bundle --------------------------------------------------------------------
@needs_engine
def test_tools_navigate_the_bundle(demo_games, demo_bundle):
    t = CoachTools(demo_games, demo_bundle, None, pro=True)
    assert "Subdirectories" in t.read_index("/")
    p = t.read_concept("/player")
    assert p["frontmatter"]["type"] == "Player Profile" and p["trust"] == "machine-confirmed"
    hits = t.search_concepts("time trouble", limit=5)
    assert hits and all("id" in h for h in hits)
    ms = t.find_moments(min_severity="blunder", limit=10)
    assert all(m["fen"] for m in ms)
    assert [m for m in ms] == sorted(ms, key=lambda m: -float(demo_bundle.get(m["id"]).frontmatter["winning_chances_lost"]))
    principle = t.read_concept("/knowledge/principles/clock-management")
    assert principle["trust"] == "unverified"


@needs_engine
def test_pro_concepts_hidden_from_free(demo_games, demo_bundle):
    free = CoachTools(demo_games, demo_bundle, None, pro=False)
    try:
        free.read_concept("/progress")
        raise AssertionError("free users must not read Pro concepts")
    except ToolError:
        pass
    assert "/progress" not in free.known and "progress.md" not in free.read_index("/")
    assert all(not h["id"].startswith(("/openings", "/progress", "/recurring", "/prep")) for h in free.search_concepts("opening progress", limit=10))


@needs_engine
def test_offline_coach_links_real_concepts(demo_games, demo_bundle):
    tools = CoachTools(demo_games, demo_bundle, None)
    r = Coach("demo_player", tools, None).chat("show me my worst blunder")
    links = concept_links(r.reply)
    assert links and all(demo_bundle.get(cid) for _, cid in links)
    first = links[0][1]
    assert first.startswith("/moments/") and r.citations[first]["fen"]


# ---- the verify-and-repair loop with a scripted model ---------------------------------------------
def _llm(script, seen):
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"choices": [{"message": script(body)}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def _call(name, args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _final(summary, next_step="Drill it in the Puzzles tab.", evidence=None):
    args = {"summary": summary, "next_step": next_step, "evidence": evidence or []}
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "f1", "type": "function", "function": {"name": "final_answer", "arguments": json.dumps(args)}}]}


@needs_engine
def test_hallucination_is_rewritten(demo_games, demo_bundle):
    seen = []

    def script(body):
        msgs = body["messages"]
        tools = [x for x in msgs if x["role"] == "tool"]
        if not tools:
            return _call("find_moments", {"min_severity": "blunder", "limit": 2})
        m = json.loads(tools[0]["content"])[0]
        ev = [{"point": f"You played {m['you_played']}; {m['engine_best']} was better.", "concept_id": m["id"], "label": m["move"]}]
        if tools[-1]["content"].startswith("Grounding check failed"):
            return _final(f"Your costliest moment came against {m['opponent']}.", evidence=ev)
        return _final("Qh5 wins on the spot, +9.9.", evidence=ev + [{"point": "See this", "concept_id": "/moments/fake-1"}])

    coach = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("openai", "m", "http://x/v1", "k"), http=_llm(script, seen))
    r = coach.chat("worst blunder?")
    assert r.grounded and "Qh5" not in r.reply and r.citations
    assert r.answer["summary"].startswith("Your costliest moment") and r.answer["evidence"][0]["concept_id"].startswith("/moments/")
    repair = [x for x in seen[-1]["messages"] if x["role"] == "tool" and x["content"].startswith("Grounding check failed")]
    assert repair and "Qh5" in repair[0]["content"]


@needs_engine
def test_persistent_hallucination_is_flagged(demo_games, demo_bundle):
    def script(body):
        last = body["messages"][-1]
        if last["role"] == "tool" or last["content"].startswith("Grounding"):
            return {"role": "assistant", "content": "You should have played Qh5 there."}
        return _call("read_concept", {"id": "/player"})

    r = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("openai", "m", "http://x/v1", "k"), http=_llm(script, [])).chat("advice?")
    assert not r.grounded and r.unverified == ["Qh5"] and "Couldn't verify" in r.reply


@needs_engine
def test_brief_is_player_concept(demo_games, demo_bundle):
    seen = []
    Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("ollama", "m", "http://x/v1", "ollama"),
          http=_llm(lambda b: {"role": "assistant", "content": "Keep studying."}, seen)).chat("hi")
    system = seen[0]["messages"][0]["content"]
    assert "# Brief: /player.md" in system and "/moments/" in system and "OKF bundle" in system


@needs_engine
def test_provider_error_falls_back(demo_games, demo_bundle):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})))
    r = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("openai", "m", "http://x/v1", "k"), http=http).chat("study plan?")
    assert r.mode == "fallback" and "rate limit" in r.reply


@needs_engine
def test_anthropic_loop(demo_games, demo_bundle):
    tools = CoachTools(demo_games, demo_bundle, None)
    mid = tools.find_moments(limit=1)[0]["id"]
    b = lambda **kw: SimpleNamespace(**kw)  # noqa: E731
    final = {"summary": "Your costliest moment is linked below.", "evidence": [{"point": "This one decided the game.", "concept_id": mid}],
             "next_step": "Solve it in the Puzzles tab."}
    script = [
        SimpleNamespace(stop_reason="tool_use", content=[b(type="text", text="Let me look..."),
                                                         b(type="tool_use", id="a", name="find_moments", input={"limit": 3})]),
        SimpleNamespace(stop_reason="tool_use", content=[b(type="tool_use", id="f", name="final_answer", input=final)]),
    ]
    coach = Coach("demo_player", tools, Backend("anthropic", "m", api_key="x"))
    coach._client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: script.pop(0)))
    r = coach.chat("worst moment?")
    assert r.mode == "agent" and r.tools_used == ["find_moments"] and mid in r.citations
    assert "Let me" not in r.reply and r.answer["next_step"] == "Solve it in the Puzzles tab."


# ---- misc ------------------------------------------------------------------------------------------
def test_clean_history():
    h = [{"role": "assistant", "content": "x"}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
         {"role": "assistant", "content": "c"}, {"role": "system", "content": "evil"}, {"role": "user", "content": "new"}]
    assert clean_history(h) == [{"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}]


def test_parse_tool_args():
    assert parse_tool_args('{"a": 1}') == {"a": 1} and parse_tool_args({"a": 1}) == {"a": 1} and parse_tool_args("junk") == {}


def test_resolve_backend_priority(monkeypatch):
    monkeypatch.setattr("app.llm.ollama_running", lambda host: True)
    for k in ("ANTHROPIC_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "COACH_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    assert resolve_backend(load_settings()).kind == "ollama"
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1/")
    monkeypatch.setenv("LLM_MODEL", "m")
    assert resolve_backend(load_settings()).base_url == "https://api.groq.com/openai/v1"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert resolve_backend(load_settings()).kind == "anthropic"
    monkeypatch.setenv("COACH_PROVIDER", "offline")
    assert resolve_backend(load_settings()) is None


def test_offline_intent_routing():
    assert OfflineCoach.INTENTS[0][0] == "prep" and OfflineCoach.INTENTS[0][1].search("where do I forget my prep")


# ---- provider strictness (Groq rejects tool calls that break schema bounds) --------------------------
def test_openai_schemas_have_no_hard_bounds():
    from app.llm import openai_tool_specs

    for spec in openai_tool_specs(CoachTools.SPECS):
        for prop in spec["function"]["parameters"].get("properties", {}).values():
            assert not {"minimum", "maximum", "enum"} & prop.keys(), spec["function"]["name"]
    fm = next(s for s in openai_tool_specs(CoachTools.SPECS) if s["function"]["name"] == "find_moments")
    assert "one of: opening, middlegame, endgame" in fm["function"]["parameters"]["properties"]["phase"]["description"]


@needs_engine
def test_out_of_range_args_are_clamped(demo_games, demo_bundle):
    t = CoachTools(demo_games, demo_bundle, None, pro=True)
    assert len(t.run("search_concepts", {"query": "game", "limit": 20})) <= 10
    assert len(t.run("find_moments", {"limit": "50", "min_severity": "inaccuracy"})) <= 10
    assert t.run("find_moments", {"limit": "lots"}) is not None


@needs_engine
def test_tool_validation_error_is_retried(demo_games, demo_bundle):
    calls = []
    groq_400 = {"error": {"message": "Tool call validation failed: parameters for tool search_concepts did not match schema: "
                                     "errors: [`/limit`: maximum: got 20, want 10]", "type": "invalid_request_error", "code": "tool_use_failed"}}

    def handler(request):
        calls.append(json.loads(request.content)["temperature"])
        if len(calls) == 1:
            return httpx.Response(400, json=groq_400)
        return httpx.Response(200, json={"choices": [{"message": _final("Your profile shows where to start.")}]})

    coach = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("openai", "m", "http://x/v1", "k"),
                  http=httpx.Client(transport=httpx.MockTransport(handler)))
    r = coach.chat("hi")
    assert r.mode == "agent" and calls == [0.3, 0.0]


@needs_engine
def test_repeated_tool_validation_error_falls_back_cleanly(demo_games, demo_bundle):
    bad = httpx.Response(400, json={"error": {"message": "Tool call validation failed", "code": "tool_use_failed"}})
    coach = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("openai", "m", "http://x/v1", "k"),
                  http=httpx.Client(transport=httpx.MockTransport(lambda r: bad)))
    r = coach.chat("study plan?")
    assert r.mode == "fallback" and "invalid tool call" in r.reply



# ---- structured answers (Pydantic) ----------------------------------------------------------------------
def test_answer_model_strips_reasoning_and_trims():
    from app.answer import CoachAnswer, clean_text, parse_answer

    a = parse_answer({"summary": "<think>the user wants x, let me check</think>Your endgames cost the most points.",
                      "evidence": [f"point {i}" for i in range(7)], "next_step": "Drill rook endings.", "extra": 1})
    assert a.summary == "Your endgames cost the most points." and len(a.evidence) == 4
    assert isinstance(a, CoachAnswer) and a.to_markdown().endswith("**Next step:** Drill rook endings.")
    assert clean_text("Let me look at your games.\nYour clock is the problem.\nI'll check the data.") == "Your clock is the problem."
    for bad in ({"summary": "Let me check your games first.", "next_step": "x x x x"}, {"summary": "ok", "next_step": "Do it."}, {}):
        try:
            parse_answer(bad)
            raise AssertionError(f"should reject {bad}")
        except ValueError as exc:
            assert "final_answer was invalid" in str(exc)


@needs_engine
def test_invalid_final_answer_is_sent_back(demo_games, demo_bundle):
    seen = []

    def script(body):
        tools = [x for x in body["messages"] if x["role"] == "tool"]
        if not tools:
            return _final("Let me look at your games.")  # process talk in the summary -> rejected
        return _final("Time trouble costs you the most rating points.", next_step="Keep 20% of your clock until move 30.")

    r = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("openai", "m", "http://x/v1", "k"),
              http=_llm(script, seen)).chat("what costs me most?")
    assert r.answer["summary"].startswith("Time trouble") and "Let me" not in r.reply
    assert "final_answer was invalid" in [x for x in seen[-1]["messages"] if x["role"] == "tool"][0]["content"]


@needs_engine
def test_plain_text_gets_nudged_then_structured(demo_games, demo_bundle):
    seen = []

    def script(body):
        last = body["messages"][-1]
        if last["role"] == "user" and last["content"].startswith("Reply by calling final_answer"):
            return _final("Your weakest area is tactics.", next_step="Do your due puzzles today.")
        return {"role": "assistant", "content": "<think>hmm</think>Okay, the user asks about weaknesses. Tactics."}

    r = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("openai", "m", "http://x/v1", "k"),
              http=_llm(script, seen)).chat("weakness?")
    assert r.answer == {"summary": "Your weakest area is tactics.", "evidence": [], "next_step": "Do your due puzzles today."}
    assert "<think>" not in json.dumps(seen[-1]["messages"])  # reasoning isn't even fed back to the model


@needs_engine
def test_prose_fallback_is_cleaned(demo_games, demo_bundle):
    r = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), Backend("openai", "m", "http://x/v1", "k"),
              http=_llm(lambda b: {"role": "assistant", "content": "<think>x</think>Let me check.\nYour clock management needs work."}, [])).chat("?")
    assert r.answer["summary"] == "Your clock management needs work." and "Let me" not in r.reply


@needs_engine
def test_offline_answers_are_structured(demo_games, demo_bundle):
    r = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), None).chat("what should I study?")
    assert r.answer["summary"].startswith("This week") and r.answer["next_step"].startswith("Start Day 1")


# ---- free-tier friendliness: rate limits and token budget -------------------------------------------
GROQ_TPM = ("Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, "
            "Used 7100, Requested 2400. Please try again in 11.25s.")
GROQ_TPD = "Rate limit reached for model `openai/gpt-oss-120b` on tokens per day (TPD): Limit 200000. Please try again in 7m12s."
OK = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
B = Backend("openai", "openai/gpt-oss-120b", "http://x/v1", "k")


def test_rate_limit_wait_parsing():
    assert rate_limit_wait(httpx.Response(429, headers={"retry-after": "3"})) == 3.0
    assert rate_limit_wait(httpx.Response(429, json={"error": {"message": GROQ_TPM}})) == 11.25
    assert rate_limit_wait(httpx.Response(429, text="Please try again in 1m2.5s")) == 62.5
    assert rate_limit_wait(httpx.Response(429, text="try again in 450ms")) == 0.5
    assert rate_limit_wait(httpx.Response(429, json={"error": {"message": GROQ_TPD}})) is None


def test_per_minute_limit_waits_then_succeeds(no_sleep):
    replies = iter([httpx.Response(429, json={"error": {"message": GROQ_TPM}}), httpx.Response(200, json=OK)])
    http = httpx.Client(transport=httpx.MockTransport(lambda r: next(replies)))
    assert openai_chat(B, [], [], http)["content"] == "hi"
    assert no_sleep == [11.75]


def test_daily_limit_fails_fast_with_clear_message(no_sleep):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {"message": GROQ_TPD}})))
    with pytest.raises(LLMError, match="daily allowance"):
        openai_chat(B, [], [], http)
    assert no_sleep == []


def test_long_wait_is_not_attempted(no_sleep):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "90"})))
    with pytest.raises(LLMError, match="per-minute"):
        openai_chat(B, [], [], http)
    assert no_sleep == []


def test_gpt_oss_reasons_briefly():
    seen = []

    def handler(r):
        seen.append(json.loads(r.content))
        return httpx.Response(200, json=OK)
    openai_chat(B, [], [], httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen[0]["reasoning_effort"] == "low" and seen[0]["max_tokens"] == llm.MAX_OUTPUT_TOKENS
    assert "reasoning_effort" not in request_extras(Backend("ollama", "llama3.2", "http://x/v1", "ollama"))


def test_compact_result_stays_valid_json():
    cards = [{"id": f"/moments/g-{i}", "fen": "x" * 200, "game_url": "https://lichess.org/abc", "clock_seconds": None}
             for i in range(30)]
    text = compact_result(cards, limit=1500)
    out = json.loads(text)
    assert len(text) <= 1500 and 1 <= len(out) < 30
    assert "game_url" not in out[0] and "clock_seconds" not in out[0]
    doc = json.loads(compact_result({"id": "/player", "body": "word " * 2000}, limit=1200))
    assert doc["body"].endswith("…")


def test_old_tool_results_are_shortened():
    msgs = [{"role": "system", "content": "s"}] + [{"role": "tool", "tool_call_id": str(i), "content": "y" * 6000} for i in range(4)]
    shrink_history(msgs, budget=14000)
    sizes = [len(m["content"]) for m in msgs[1:]]
    assert sizes[-1] == sizes[-2] == 6000 and sizes[0] < 700
    assert sum(sizes) <= 14000 + 1000


@needs_engine
def test_a_typical_question_fits_a_free_tier_minute(demo_games, demo_bundle):
    """A heavy 4-call question: every request fits easily in an 8K tokens/minute budget, and the whole question
    stays near it (bursts above it are absorbed by the wait-and-retry in openai_chat)."""
    sizes = []

    def script(body):
        sizes.append(len(json.dumps(body)))
        tools = [m for m in body["messages"] if m["role"] == "tool"]
        if len(tools) == 0:
            return _call("read_index", {"path": "/"})
        if len(tools) == 1:
            return _call("find_moments", {"min_severity": "inaccuracy", "limit": 10})
        if len(tools) == 2:
            return _call("read_concept", {"id": json.loads(tools[-1]["content"])[0]["id"]})
        return _final("Your middlegame decisions cost you the most points.")
    Coach("demo_player", CoachTools(demo_games, demo_bundle, None), B, http=_llm(script, [])).chat("where do I lose points?")
    assert max(sizes) / 4 < 3000 and sum(sizes) / 4 < 9500  # ~4 characters per token; was ~11,300 before trimming


@needs_engine
def test_second_provider_answers_when_the_first_is_out_of_quota(demo_games, demo_bundle):
    """Groq's daily limit hit -> the fallback provider (e.g. Gemini) answers; both labels are named correctly."""
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "api.groq.com":
            return httpx.Response(429, json={"error": {"message": GROQ_TPD}})
        return httpx.Response(200, json={"choices": [{"message": _final("Your middlegame costs you the most points.")}]})
    groq = Backend("openai", "openai/gpt-oss-120b", "https://api.groq.com/openai/v1", "k")
    gemini = Backend("openai", "gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1beta/openai", "k2")
    coach = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), [groq, gemini],
                  http=httpx.Client(transport=httpx.MockTransport(handler)))
    r = coach.chat("Where do I lose points?")
    assert r.mode == "agent" and r.model == "Gemini · gemini-2.5-flash" and hosts == ["api.groq.com", "generativelanguage.googleapis.com"]
    assert r.answer["summary"].startswith("Your middlegame")


@needs_engine
def test_both_providers_down_falls_back_to_offline(demo_games, demo_bundle):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="down")))
    backends = [Backend("openai", "a", "https://api.groq.com/openai/v1", "k"), Backend("openai", "b", "https://openrouter.ai/api/v1", "k")]
    r = Coach("demo_player", CoachTools(demo_games, demo_bundle, None), backends, http=http).chat("study plan?")
    assert r.mode == "fallback" and "tried both" in r.notice


def test_resolve_backends_adds_the_fallback():
    import dataclasses

    from app.llm import resolve_backends

    s = dataclasses.replace(load_settings(), coach_provider="auto", anthropic_api_key=None, llm_base_url="https://api.groq.com/openai/v1",
                            llm_model="openai/gpt-oss-120b", llm_api_key="k", llm_fallback_base_url="https://openrouter.ai/api/v1",
                            llm_fallback_model="openai/gpt-oss-120b:free", llm_fallback_api_key="k2")
    assert [b.label for b in resolve_backends(s)] == ["Groq · openai/gpt-oss-120b", "OpenRouter · openai/gpt-oss-120b:free"]
    assert resolve_backends(dataclasses.replace(s, coach_provider="offline")) == []
