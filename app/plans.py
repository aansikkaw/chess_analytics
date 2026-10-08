"""Plans: what each one includes (entitlements) and how it's sold (offers).

Entitlements live in PLANS and are enforced server-side. Offers are what the Plans page
shows: Free, Pro (monthly or yearly), the Event Pass (Pro for 10 days) and the Coach plan
(waitlist for now).

Payments aren't wired up yet. Users press a button that stores an upgrade request; an admin
grants it with `python scripts/admin.py grant <email>` (or `--days 10` for an Event Pass).
Emails listed in ADMIN_EMAILS get Pro and admin rights on signup.
"""

from __future__ import annotations

import os
import time

PLANS: dict[str, dict] = {
    "free": {
        "name": "Free",
        "price": "Free",
        "max_games_per_sync": 200,
        "max_accounts": 2,
        "coach_messages_per_day": 15,
        "scouts_per_day": 0,
        "features": {"dna", "puzzles", "plan", "coach"},
        "blurb": "Rating DNA, puzzles from your own mistakes, a weekly plan, ChessBase/PGN upload and 15 coach questions a day.",
    },
    "pro": {
        "name": "Pro",
        "price": os.getenv("PRO_PRICE_LABEL", "₹399 / $6 a month"),
        "max_games_per_sync": 500,
        "max_accounts": 6,
        "coach_messages_per_day": 200,
        "scouts_per_day": 25,
        "features": {"dna", "puzzles", "plan", "coach", "scout", "repertoire", "progress", "autosync", "prep"},
        "blurb": "Everything in Free, plus Prep Check for your ChessBase repertoire, Opponent Scout, Repertoire Leaks, "
                 "Progress tracking and Auto-sync.",
    },
}

PLANS["coach"] = {
    **PLANS["pro"],
    "name": "Coach",
    "price": os.getenv("COACH_PRICE_LABEL", "₹2,499 / $29 a month"),
    "max_students": int(os.getenv("COACH_MAX_STUDENTS", "20")),
    "features": PLANS["pro"]["features"] | {"students"},
    "blurb": "Everything in Pro, plus a dashboard for up to 20 students: every student's Rating DNA, weak spots and homework.",
}

FEATURE_NAMES = {
    "students": "Coach dashboard",
    "scout": "Opponent Scout",
    "repertoire": "Repertoire Leaks",
    "progress": "Progress tracking",
    "autosync": "Auto-sync",
    "prep": "Prep Check",
}

EVENT_PASS_DAYS = 10

# What the Plans page sells. Prices are labels until payments are connected; change them with env vars.
OFFERS: list[dict] = [
    {"id": "free", "name": "Free", "grants": "free", "tagline": "See what's costing you rating.",
     "price": {"inr": "₹0", "usd": "$0", "period": ""},
     "points": ["Rating DNA across 7 skills", "Puzzles from your own mistakes", "A 7-day study plan",
                "Up to 200 games per sync, 2 linked accounts", "15 coach questions a day"]},
    {"id": "pro", "name": "Pro", "grants": "pro", "tagline": "Prepare for real opponents and track your progress.", "featured": True,
     "price": {"inr": os.getenv("PRO_PRICE_INR", "₹399"), "usd": os.getenv("PRO_PRICE_USD", "$6"), "period": "month",
               "annual_inr": os.getenv("PRO_ANNUAL_INR", "₹2,999"), "annual_usd": os.getenv("PRO_ANNUAL_USD", "$49")},
     "points": ["Everything in Free", "Prep Check: your ChessBase repertoire vs your games", "Opponent Scout (25 a day)",
                "Repertoire Leaks and recurring mistakes", "Progress tracking and Auto-sync", "Up to 500 games per sync, 200 coach questions a day"]},
    {"id": "event", "name": "Event Pass", "grants": "pro", "tagline": f"Pro for {EVENT_PASS_DAYS} days, for one tournament.",
     "price": {"inr": os.getenv("EVENT_PRICE_INR", "₹149"), "usd": os.getenv("EVENT_PRICE_USD", "$3"), "period": "one-off"},
     "points": ["Scout every opponent in your event", "Prep Check before round one", f"All Pro features for {EVENT_PASS_DAYS} days",
                "No subscription"]},
    {"id": "coach", "name": "Coach", "grants": "coach", "tagline": "For coaches and academies.",
     "price": {"inr": os.getenv("COACH_PRICE_INR", "₹2,499"), "usd": os.getenv("COACH_PRICE_USD", "$29"), "period": "month"},
     "points": ["Everything in Pro for you", "Up to 20 students, each analysed from up to 500 games",
                "Squad heatmap: every student's Rating DNA and weak spots", "Homework you assign, and they tick off",
                "Students who accept your invite get Pro free"]},
]
OFFER_IDS = {o["id"] for o in OFFERS}


def effective_plan(user: dict) -> str:
    """The plan in force now: a time-limited plan (Event Pass) falls back to Free when it expires."""
    plan = user.get("plan", "free")
    expires = user.get("plan_expires_at")
    if plan != "free" and expires and expires < time.time():
        return "free"
    return plan if plan in PLANS else "free"


def plan_of(user: dict) -> dict:
    return PLANS[effective_plan(user)]


def is_pro(user: dict) -> bool:
    """Pro features: Pro, an Event Pass, the Coach plan, or a coach's student."""
    return effective_plan(user) in ("pro", "coach")


def is_coach(user: dict) -> bool:
    return effective_plan(user) == "coach"


def has_feature(user: dict, feature: str) -> bool:
    return feature in plan_of(user)["features"]


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def public_plans() -> list[dict]:
    out = []
    for o in OFFERS:
        ent = PLANS.get(o["grants"] or "pro", PLANS["pro"])
        out.append({**o, "max_games_per_sync": ent["max_games_per_sync"], "coach_messages_per_day": ent["coach_messages_per_day"],
                    "features": sorted(ent["features"])})
    return out
