"""Kill switches: pause one part of the service without a redeploy.

  python scripts/admin.py switch                    show every switch
  python scripts/admin.py switch ai_coach off       pause the AI coach (the built-in coach answers instead)
  python scripts/admin.py switch imports on         turn syncing back on

Switches live in the database, so they reach every web process and worker within a few seconds, and the
status page shows what's paused. Everything is on unless switched off.
"""

from __future__ import annotations

import threading
import time

SWITCHES: dict[str, str] = {
    "ai_coach": "AI coach (when off, the built-in coach answers)",
    "imports": "Syncing and uploading games",
    "signups": "New accounts",
    "chesscom": "Fetching games from Chess.com",
    "emails": "Sending email (when off, emails are saved to outbox/)",
}
PAUSED_MESSAGES = {
    "ai_coach": "The AI coach is paused for maintenance, so the built-in coach is answering.",
    "imports": "Syncing is paused for maintenance. Your games are safe; try again a little later.",
    "signups": "New accounts are paused for a short while. Please try again later.",
    "chesscom": "Chess.com syncing is paused for now. Lichess and PGN uploads still work.",
}
CACHE_S = 5.0
_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()


def is_on(store, name: str) -> bool:
    now = time.time()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    try:
        row = store.kv_get(f"switch:{name}")
    except Exception:  # noqa: BLE001 - a broken read must never switch things off
        row = None
    on = True if row is None else bool(row.get("on", True))
    with _lock:
        _cache[name] = (now, on)
    return on


def set_switch(store, name: str, on: bool, actor: str = "admin") -> None:
    if name not in SWITCHES:
        raise KeyError(name)
    store.kv_set(f"switch:{name}", {"on": on, "by": actor})
    store.audit(actor, "switch_on" if on else "switch_off", name)
    with _lock:
        _cache.pop(name, None)


def paused(store) -> list[str]:
    return [n for n in SWITCHES if not is_on(store, n)]


def clear_cache() -> None:
    with _lock:
        _cache.clear()
