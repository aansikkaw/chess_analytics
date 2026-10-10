"""Which LLM powers the coach, and the OpenAI-compatible agent loop.

Providers, in the order `COACH_PROVIDER=auto` tries them:
  1. anthropic : Claude, if ANTHROPIC_API_KEY is set
  2. openai    : any OpenAI-compatible endpoint, if LLM_BASE_URL + LLM_MODEL are set
                 (Groq, Google Gemini, OpenRouter, Together, LM Studio, ...)
  3. ollama    : a local Ollama server, if one is running (free, no key)
  4. offline   : the rule-based coach, always available

Ollama speaks the same OpenAI-compatible protocol at /v1, so providers 2 and 3
share one code path.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import httpx

from .config import Settings

_OLLAMA_CHECK_TTL = 30.0  # seconds to cache "is Ollama running?"
MAX_RATE_WAIT = 30.0  # wait this long at most for a per-minute limit to reset, then give up
RATE_RETRIES = 2
MAX_OUTPUT_TOKENS = 2048  # the answer form is short; this also keeps free-tier token estimates low
_sleep = time.sleep  # swapped out in tests
_ollama_cache: dict[str, tuple[float, bool]] = {}


@dataclass(frozen=True)
class Backend:
    kind: str  # anthropic | openai | ollama
    model: str
    base_url: str | None = None
    api_key: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "openai":
            host = (self.base_url or "").split("//")[-1].split("/")[0]
            name = next((v for k, v in PROVIDER_NAMES.items() if k in host), host or "OpenAI-compatible API")
        else:
            name = {"anthropic": "Claude", "ollama": "Ollama (local)"}[self.kind]
        return f"{name} · {self.model}"


PROVIDER_NAMES = {"groq.com": "Groq", "googleapis.com": "Gemini", "openrouter.ai": "OpenRouter", "together": "Together",
                  "cerebras.ai": "Cerebras", "mistral.ai": "Mistral", "deepinfra": "DeepInfra", "openai.com": "OpenAI",
                  "fireworks.ai": "Fireworks"}


def ollama_running(host: str) -> bool:
    now = time.monotonic()
    hit = _ollama_cache.get(host)
    if hit and now - hit[0] < _OLLAMA_CHECK_TTL:
        return hit[1]
    try:
        ok = httpx.get(f"{host}/api/tags", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        ok = False
    _ollama_cache[host] = (now, ok)
    return ok


def resolve_backend(s: Settings) -> Backend | None:
    """Pick the coach's LLM from settings. None means the offline coach."""
    p = s.coach_provider
    if p == "offline":
        return None
    if p in ("auto", "anthropic") and s.anthropic_api_key:
        return Backend("anthropic", s.coach_model, api_key=s.anthropic_api_key)
    if p in ("auto", "openai") and s.llm_base_url and s.llm_model:
        return Backend("openai", s.llm_model, s.llm_base_url, s.llm_api_key)
    if p in ("auto", "ollama") and ollama_running(s.ollama_host):
        return Backend("ollama", s.ollama_model, f"{s.ollama_host}/v1", "ollama")
    return None


def resolve_backends(s: Settings) -> list[Backend]:
    """The primary model, then a fallback provider if one is configured (LLM_FALLBACK_*)."""
    out: list[Backend] = []
    primary = resolve_backend(s)
    if primary:
        out.append(primary)
    if s.coach_provider != "offline" and s.llm_fallback_base_url and s.llm_fallback_model:
        fb = Backend("openai", s.llm_fallback_model, s.llm_fallback_base_url, s.llm_fallback_api_key)
        if fb not in out:
            out.append(fb)
    return out


class LLMError(RuntimeError):
    """The LLM provider failed in a way the user can act on."""


class ToolCallRejected(LLMError):
    """The provider rejected the model's tool call. Carries the provider's reason and what the model tried to write,
    so the coach can salvage an answer or tell the model exactly what to fix."""

    def __init__(self, message: str, detail: str = "", failed_generation: str = ""):
        super().__init__(message)
        self.detail = detail
        self.failed_generation = failed_generation


def relax_schema(schema: dict) -> dict:
    """Strip numeric bounds and enums, folding them into descriptions.

    Some providers (Groq, for example) reject the *whole request* when a model's tool call
    breaks a schema bound, e.g. `limit: 20` against `maximum: 10`. The tools clamp and
    validate their own arguments, so the schema only needs to *guide* the model.
    """
    out = dict(schema)
    props = {}
    for name, prop in (schema.get("properties") or {}).items():
        p = dict(prop)
        hints = []
        if "enum" in p:
            hints.append("one of: " + ", ".join(map(str, p.pop("enum"))))
        lo, hi = p.pop("minimum", None), p.pop("maximum", None)
        if lo is not None or hi is not None:
            hints.append(f"between {lo if lo is not None else '...'} and {hi if hi is not None else '...'}")
        if hints:
            p["description"] = (p.get("description", "") + " (" + "; ".join(hints) + ")").strip()
        props[name] = p
    if props:
        out["properties"] = props
    return out


def openai_tool_specs(specs: list[dict]) -> list[dict]:
    """Convert Anthropic-style tool specs to the OpenAI `tools` format, with lenient schemas."""
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                          "parameters": relax_schema(t["input_schema"])}}
        for t in specs
    ]


def _is_tool_validation_error(r: httpx.Response) -> bool:
    text = r.text.lower()
    return r.status_code == 400 and ("tool call validation" in text or "tool_use_failed" in text or "failed to call a function" in text)


def rate_limit_wait(r: httpx.Response) -> float | None:
    """Seconds to wait before retrying a 429, or None if it's a daily limit (waiting won't help today)."""
    text = r.text
    if re.search(r"per day|\((?:TPD|RPD)\)", text, re.I):
        return None
    try:
        return max(0.5, float(r.headers.get("retry-after", "")))
    except ValueError:
        pass
    m = re.search(r"try again in\s+(?:(\d+)m)?\s*(?:([\d.]+)s|(\d+)ms)", text, re.I)  # Groq: "1m2.5s", "7.3s", "450ms"
    if m:
        mins, secs, ms = m.groups()
        return max(0.5, int(mins or 0) * 60 + float(secs or 0) + int(ms or 0) / 1000)
    return 20.0


def request_extras(backend: Backend) -> dict:
    """Provider-specific knobs. gpt-oss models think at length by default, and free tiers count those tokens."""
    extras: dict = {"max_tokens": MAX_OUTPUT_TOKENS}
    effort = os.environ.get("LLM_REASONING_EFFORT", "low" if "gpt-oss" in backend.model else "")
    if effort and backend.kind == "openai":
        extras["reasoning_effort"] = effort
    return extras


def openai_chat(backend: Backend, messages: list[dict], tools: list[dict], http: httpx.Client, wait_for_limits: bool = True,
                tool_choice: str | dict | None = None, max_tokens: int | None = None) -> dict:
    """One /chat/completions call. Returns the assistant message dict.

    - A malformed tool call rejected by the provider: retry once at temperature 0.
    - A per-minute rate limit (429): wait as long as the provider asks (up to MAX_RATE_WAIT), then retry.
    - A daily limit: fail straight away with a clear message.
    """
    headers = {"Authorization": f"Bearer {backend.api_key}"} if backend.api_key else {}
    body = {"model": backend.model, "messages": messages, "tools": tools, **request_extras(backend)}
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if max_tokens:
        body["max_tokens"] = max_tokens
    temperature, rate_retries, r = 0.3, 0, None
    for _ in range(2 + RATE_RETRIES):
        try:
            r = http.post(f"{backend.base_url}/chat/completions", headers=headers, json={**body, "temperature": temperature})
        except httpx.TimeoutException as exc:
            raise LLMError("The model took too long to answer. A smaller model or a bigger machine will help.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Couldn't reach the model at {backend.base_url}: {exc}") from exc
        if r.status_code == 429:
            wait = rate_limit_wait(r)
            if wait is None:
                raise LLMError("You've used up the provider's free daily allowance for this model. It resets within 24 hours; "
                               "until then, switch LLM_MODEL or use another provider.")
            if not wait_for_limits or wait > MAX_RATE_WAIT or rate_retries >= RATE_RETRIES:
                raise LLMError("The provider's per-minute rate limit was hit. Wait a minute and try again.")
            rate_retries += 1
            _sleep(wait + 0.5)
            continue
        if r.status_code == 400 and "tool_choice" in body and "tool_choice" in r.text:
            body.pop("tool_choice")  # a provider that can't force a tool: ask normally instead
            continue
        if _is_tool_validation_error(r) and temperature > 0:
            temperature = 0.0
            continue
        break
    if r.status_code == 404 and backend.kind == "ollama":
        raise LLMError(f"Ollama doesn't have the model '{backend.model}'. Run: ollama pull {backend.model}")
    if r.status_code in (401, 403):
        raise LLMError("The API key was rejected. Check LLM_API_KEY.")
    if r.status_code == 413:
        raise LLMError("That conversation is too long for this model's free tier. Start a new chat and ask again.")
    if r.status_code == 429:
        raise LLMError("The provider's per-minute rate limit was hit. Wait a minute and try again.")
    if _is_tool_validation_error(r):
        try:
            err = r.json().get("error") or {}
        except ValueError:
            err = {}
        raise ToolCallRejected("The model made an invalid tool call twice in a row. Try rephrasing, or use a larger model.",
                               detail=str(err.get("message") or "")[:500], failed_generation=str(err.get("failed_generation") or "")[:6000])
    if r.status_code >= 400:
        raise LLMError(f"The model provider returned an error ({r.status_code}): {r.text[:200]}")
    try:
        data = r.json()
        choice = data["choices"][0]
        msg = dict(choice["message"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError("The model returned a response I couldn't read.") from exc
    # Not sent back to the model: why it stopped, and what it used, for retries and the server log.
    msg["_finish"] = choice.get("finish_reason")
    msg["_usage"] = data.get("usage") or {}
    msg["_reasoned"] = bool(msg.pop("reasoning", None) or msg.pop("reasoning_content", None))
    return msg


def parse_tool_args(raw) -> dict:
    """Tool arguments arrive as a JSON string (OpenAI) or an object (some Ollama versions)."""
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}
