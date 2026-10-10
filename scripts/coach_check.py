"""Ask the AI coach one question from the terminal and print every step: what the model was asked for,
why it stopped, how many tokens it used, and which tools it called. Use it when the coach falls back.

  python scripts/coach_check.py "What should I study this week?"
  python scripts/coach_check.py "Why do I lose won games?" --email you@example.com

It uses your real settings (LLM_* secrets) and your analysed games; nothing is changed in the database
except one coach question counted against nobody (the daily limit isn't used here).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import coach as coach_mod  # noqa: E402
from app.coach import Coach, CoachTools  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.engine import Engine, EngineUnavailable  # noqa: E402
from app.llm import resolve_backends  # noqa: E402
from app.store import Store  # noqa: E402
from app.sync import Importer, player_key  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 1
    question = argv[0]
    email = argv[argv.index("--email") + 1] if "--email" in argv and argv.index("--email") + 1 < len(argv) else None
    logging.basicConfig(level=logging.WARNING, format="  log: %(message)s")
    settings = load_settings()
    store = Store(settings.db_path)
    backends = resolve_backends(settings)
    if not backends:
        print("No AI model is configured (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL), so the coach uses the built-in coach.")
        return 1
    print("Models, in order:", ", ".join(b.label for b in backends))

    with store._conn() as c:
        rows = [dict(r) for r in c.execute("SELECT a.* FROM accounts a JOIN users u ON u.id = a.user_id"
                                           + (" WHERE u.email = ?" if email else ""), (email,) if email else ())]
    rows = [a for a in rows if store.count_games(player_key(a))]
    if not rows:
        print("No account with analysed games found" + (f" for {email}" if email else "") + ".")
        return 1
    acct = max(rows, key=lambda a: store.count_games(player_key(a)))
    games = store.load_games(player_key(acct))
    print(f"Account: {acct['handle']} ({acct['platform']}), {len(games)} games\n")
    bundle = Importer(store, settings).knowledge.load(acct)
    if bundle is None:
        print("This account has no knowledge bundle yet: sync it once in the app.")
        return 1
    try:
        engine = Engine(settings.stockfish_path, settings.engine_depth, settings.engine_time)
    except EngineUnavailable:
        engine = None

    step = {"n": 0}
    real_chat = coach_mod.openai_chat

    def traced(backend, messages, tools, http, **kw):
        step["n"] += 1
        forced = "final_answer" if kw.get("tool_choice") else "-"
        print(f"Step {step['n']}: {backend.model}  forced={forced}  max_tokens={kw.get('max_tokens') or 'default'}  "
              f"messages={len(messages)}")
        try:
            msg = real_chat(backend, messages, tools, http, **kw)
        except Exception as exc:  # noqa: BLE001
            print(f"  error: {type(exc).__name__}: {exc}")
            detail = getattr(exc, "detail", "")
            if detail:
                print(f"  provider said: {detail[:300]}")
            raise
        calls = [c.get("function", {}).get("name") for c in msg.get("tool_calls") or []]
        content = (msg.get("content") or "").strip()
        print(f"  finish={msg.get('_finish')}  usage={json.dumps(msg.get('_usage'))}  reasoned={msg.get('_reasoned')}")
        print(f"  tool calls: {calls or 'none'}  text: {content[:120]!r}")
        return msg

    coach_mod.openai_chat = traced
    try:
        r = Coach(acct["handle"], CoachTools(games, bundle, engine, store.count_due(player_key(acct)), pro=True), backends).chat(question)
    finally:
        coach_mod.openai_chat = real_chat
        if engine:
            engine.close()
    print(f"\nResult: mode={r.mode}  model={r.model}")
    if r.notice:
        print(f"Notice: {r.notice}")
    print(f"Answer: {r.answer['summary'] if r.answer else r.reply[:300]}")
    return 0 if r.mode == "agent" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
