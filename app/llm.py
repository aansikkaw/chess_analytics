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
import time
from dataclasses import dataclass

import httpx

from .config import Settings

_OLLAMA_CHECK_TTL = 30.0  # seconds to cache "is Ollama running?"
_ollama_cache: dict[str, tuple[float, bool]] = {}


@dataclass(frozen=True)
class Backend:
    kind: str  # anthropic | openai | ollama
    model: str
    base_url: str | None = None
    api_key: str | None = None

    @property
    def label(self) -> str:
        names = {"anthropic": "Claude", "openai": "OpenAI-compatible API", "ollama": "Ollama (local)"}
        return f"{names[self.kind]} · {self.model}"


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


class LLMError(RuntimeError):
    """The LLM provider failed in a way the user can act on."""


def openai_tool_specs(specs: list[dict]) -> list[dict]:
    """Convert Anthropic-style tool specs to the OpenAI `tools` format."""
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in specs
    ]


def openai_chat(backend: Backend, messages: list[dict], tools: list[dict], http: httpx.Client) -> dict:
    """One /chat/completions call. Returns the assistant message dict."""
    headers = {"Authorization": f"Bearer {backend.api_key}"} if backend.api_key else {}
    try:
        r = http.post(
            f"{backend.base_url}/chat/completions",
            headers=headers,
            json={"model": backend.model, "messages": messages, "tools": tools, "temperature": 0.3},
        )
    except httpx.TimeoutException as exc:
        raise LLMError("The model took too long to answer. A smaller model or a bigger machine will help.") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"Couldn't reach the model at {backend.base_url}: {exc}") from exc
    if r.status_code == 404 and backend.kind == "ollama":
        raise LLMError(f"Ollama doesn't have the model '{backend.model}'. Run: ollama pull {backend.model}")
    if r.status_code in (401, 403):
        raise LLMError("The API key was rejected. Check LLM_API_KEY.")
    if r.status_code == 429:
        raise LLMError("The provider's rate limit was hit. Wait a minute and try again.")
    if r.status_code >= 400:
        raise LLMError(f"The model provider returned an error ({r.status_code}): {r.text[:200]}")
    try:
        return r.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LLMError("The model returned a response I couldn't read.") from exc


def parse_tool_args(raw) -> dict:
    """Tool arguments arrive as a JSON string (OpenAI) or an object (some Ollama versions)."""
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}
