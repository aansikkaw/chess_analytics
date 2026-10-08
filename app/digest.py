"""The weekly email: three numbers that keep players coming back.

For each linked account with games: current rating, accuracy over the last 7 days against
before, the skill that moved most, and how many puzzles are waiting. Sent to verified users
who haven't opted out, at most once a week, with a one-click unsubscribe link.
"""

from __future__ import annotations

import datetime as dt
import time
from html import escape

from .mailer import BRAND, Mailer, _html
from .profile import SKILLS, rating_dna
from .progress import progress_report
from .store import Store
from .sync import PLATFORM_LABELS, player_key


def _date(s: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(s[:10].replace("-", "."), "%Y.%m.%d").date()
    except ValueError:
        return None


def account_digest(store: Store, account: dict, today: dt.date | None = None) -> dict | None:
    games = store.load_games(player_key(account))
    if not games:
        return None
    today = today or dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    recent = [g for g in games if (d := _date(g.played_at)) and d > week_ago]
    older = [g for g in games if (d := _date(g.played_at)) and d <= week_ago]
    acc = lambda gs: round(sum(g.accuracy for g in gs) / len(gs), 1) if gs else None  # noqa: E731
    dna = rating_dna(games)
    weakest = min((s for s in dna["skills"] if not s["low_confidence"]), key=lambda s: s["rating"], default=None)
    trend = (progress_report(games) or {}).get("trend") or {}
    moved = trend.get("most_improved")
    return {
        "handle": account["handle"], "platform": PLATFORM_LABELS.get(account["platform"], account["platform"]),
        "rating": dna["base_rating"], "games_this_week": len(recent),
        "accuracy_this_week": acc(recent), "accuracy_before": acc(older),
        "most_improved": SKILLS[moved][0] if moved in SKILLS else None,
        "weakest": weakest["label"] if weakest else None,
        "puzzles_due": store.count_due(player_key(account)),
    }


def compose(items: list[dict], app_url: str, unsubscribe_url: str) -> tuple[str, str, str]:
    lines, paras = [], []
    for d in items:
        if d["accuracy_this_week"] is not None and d["accuracy_before"] is not None:
            delta = d["accuracy_this_week"] - d["accuracy_before"]
            acc = f"{d['accuracy_this_week']}% accuracy this week ({'+' if delta >= 0 else ''}{delta:.1f} vs before)"
        elif d["accuracy_this_week"] is not None:
            acc = f"{d['accuracy_this_week']}% accuracy this week"
        else:
            acc = "no games this week"
        bits = [f"Rating {d['rating']}", acc]
        if d["most_improved"]:
            bits.append(f"most improved: {d['most_improved']}")
        if d["weakest"]:
            bits.append(f"focus next: {d['weakest']}")
        bits.append(f"{d['puzzles_due']} puzzles waiting")
        lines.append(f"{d['platform']} · {d['handle']}: " + "; ".join(bits) + ".")
        paras.append(f"<b>{escape(d['platform'])} · {escape(d['handle'])}</b><br>" + "<br>".join(escape(b) for b in bits))
    subject = "Your week in chess"
    text = ("Here's your week:\n\n" + "\n".join(lines) + f"\n\nOpen your dashboard: {app_url}\n\n"
            f"Don't want these emails? Unsubscribe: {unsubscribe_url}")
    html = _html("Your week in chess", paras, ("Open my dashboard", app_url),
                 f"You get this weekly summary from {BRAND}. <a href='{escape(unsubscribe_url)}'>Unsubscribe</a>.")
    return subject, text, html


def send_digests(store: Store, mailer: Mailer, public_url: str, limit: int = 500) -> int:
    sent = 0
    for user in store.digest_candidates()[:limit]:
        items = [d for a in store.accounts(user["id"]) if (d := account_digest(store, a))]
        if not items:  # nothing to report this week; check again next week rather than every run
            store.update_user(user["id"], last_digest_at=time.time())
            continue
        unsub = f"{public_url}/api/digest/unsubscribe?token={store.unsubscribe_token(user['id'])}"
        subject, text, html = compose(items, public_url, unsub)
        headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
        if mailer.send(user["email"], subject, text, html, headers=headers):
            store.update_user(user["id"], last_digest_at=time.time())
            sent += 1
    return sent
