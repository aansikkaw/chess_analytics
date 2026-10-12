"""Privacy policy, terms and refund policy, served at /privacy, /terms and /refunds.

These are a sensible starting draft written for a small Indian online service (DPDP Act 2023, IT Act 2000,
Consumer Protection (E-Commerce) Rules 2020). They are NOT legal advice: have a lawyer read them before you
charge money. Until then a "draft" note shows on every page; set LEGAL_REVIEWED=1 once they've been checked.

Settings:  LEGAL_ENTITY (who runs the service), SUPPORT_EMAIL (the contact and grievance address),
           LEGAL_CITY (courts with jurisdiction), LEGAL_UPDATED (date shown, e.g. 2026-10-11).
"""

from __future__ import annotations

import os
from html import escape

from .config import Settings

PAGES = ("privacy", "terms", "refunds")
TITLES = {"privacy": "Privacy policy", "terms": "Terms of service", "refunds": "Refund and cancellation policy"}


def _ctx(settings: Settings) -> dict:
    return {
        "entity": escape(os.getenv("LEGAL_ENTITY", "").strip() or "the team behind Plateau Breaker"),
        "contact": escape(settings.support_email or "the support form in the app"),
        "city": escape(os.getenv("LEGAL_CITY", "").strip() or "New Delhi"),
        "updated": escape(os.getenv("LEGAL_UPDATED", "").strip() or "October 2026"),
        "url": escape(settings.public_url),
        "reviewed": os.getenv("LEGAL_REVIEWED", "0") == "1",
    }


def _privacy(c: dict) -> str:
    return f"""
<p>Plateau Breaker is run by {c['entity']} ("we"). This policy says what we collect, why, who else sees it,
and how you can see, download or delete it. Plateau Breaker is for people aged 18 and over.</p>

<h2>What we collect</h2>
<ul>
<li><b>Your account:</b> your email address and a scrambled (hashed) form of your password. We never see or store the password itself.</li>
<li><b>Chess usernames you link</b> and the <b>public games</b> we download from Lichess or Chess.com for them, or PGN files you upload.</li>
<li><b>What we work out from your games:</b> engine analysis, skill ratings, puzzles, repertoire reports and your training progress.</li>
<li><b>Questions you ask the AI coach</b> and its answers, and how many you ask each day (for plan limits).</li>
<li><b>Opponent Library and Twins:</b> public games of players you scout, and practice bots (Twins) fitted to
those games. Games you upload for an over-the-board opponent stay private to you.</li>
<li><b>Coach rosters:</b> if you're a coach, the names and chess usernames of students you add, notes you write and homework you set.</li>
<li><b>Support messages</b> you send us.</li>
<li><b>Technical data:</b> your IP address (to stop abuse such as password guessing), error reports, and, if enabled,
privacy-friendly page analytics that use no cookies.</li>
</ul>

<h2>Why we use it</h2>
<p>To run the service you asked for (analysing games and coaching you), to keep accounts secure, to email you
about your account (confirmation, password resets and, if you leave it on, a weekly summary), to answer support
messages, and to fix bugs. We do not sell your data and we do not use it for advertising.</p>

<h2>Who else processes it</h2>
<ul>
<li><b>AI model provider</b> (for example Groq): when you ask the coach a question, the question and a summary of your
games are sent to the model to write the answer. We don't send your email address or password.</li>
<li><b>Email delivery provider</b> (for example Brevo or Resend): to deliver account emails.</li>
<li><b>Error monitoring</b> (Sentry, if enabled) and <b>analytics</b> (Plausible or PostHog, if enabled).</li>
<li><b>Hosting provider</b> where our server and backups live.</li>
<li><b>Payment provider</b> (for example Razorpay), once paid plans take payments online. We never see your full card details.</li>
</ul>
<p>Some of these companies may process data outside India. We only use providers that protect it under contract.</p>

<h2>How long we keep it</h2>
<p>We keep your data while your account is open. When you delete your account, your games, puzzles, reports and
account details are deleted at once; copies in our encrypted backups are overwritten within 30 days. We keep a
short security log of account actions (without your email) for up to one year.</p>

<h2>Your rights</h2>
<ul>
<li><b>See and download your data:</b> Account → <i>Download my data</i> gives you everything in one file.</li>
<li><b>Correct it:</b> change your linked accounts and settings in the app, or write to us.</li>
<li><b>Delete it:</b> Account → <i>Delete account</i> removes everything.</li>
<li><b>Withdraw consent:</b> deleting your account withdraws it. You can also turn off the weekly email at any time.</li>
<li><b>Coaches' students:</b> a student (or their parent or guardian) can ask the coach, or us, to remove them from a roster.</li>
</ul>

<h2>Security</h2>
<p>Passwords are hashed with scrypt, sessions use secure cookies, connections are encrypted (HTTPS), sign-in
attempts are rate-limited, and backups are encrypted.</p>

<h2>Grievance officer and contact</h2>
<p>For any privacy question or complaint, write to {c['contact']}. We reply within 7 days and resolve
complaints within 30 days. If you're not satisfied, you may complain to the Data Protection Board of India.</p>

<h2>Changes</h2>
<p>If we change this policy in a way that matters, we'll tell you by email or in the app before it takes effect.</p>
"""


def _terms(c: dict) -> str:
    return f"""
<p>These terms are an agreement between you and {c['entity']} for using Plateau Breaker at {c['url']}.
By creating an account you agree to them and to our <a href="/privacy">privacy policy</a>.</p>

<h2>Who can use it</h2>
<p>You must be 18 or over. Coaches who add students under 18 confirm that a parent or guardian has agreed.</p>

<h2>Your account</h2>
<p>Keep your password private; you're responsible for activity on your account. Only link chess usernames that
are yours, or that you coach with the player's permission.</p>

<h2>Fair use</h2>
<p>Don't use the service to cheat in games, to break the rules of Lichess, Chess.com or any tournament, to attack
or overload the service, to scrape it, or to try to get the AI coach to do anything other than chess coaching.
We may suspend accounts that do.</p>

<h2>The AI coach and analysis</h2>
<p>Engine analysis and AI answers are training aids. They can be wrong. Use your own judgement; we are not
responsible for results in your games.</p>

<h2>Plans and payment</h2>
<p>Free features are free. Paid plans (Pro, Coach, Event Pass) give the features and limits shown on the Plans
page at the time you buy. Prices include applicable taxes unless shown otherwise. See our
<a href="/refunds">refund policy</a>.</p>

<h2>Your content</h2>
<p>Your games, notes and uploads stay yours. You let us store and process them only to run the service for you.</p>

<h2>Availability and changes</h2>
<p>We work to keep the service running (see <a href="/status">service status</a>) but can't promise it will
never be interrupted. We may change features; if a change takes away something you paid for, we'll offer a
fair refund.</p>

<h2>Liability</h2>
<p>To the extent the law allows, the service is provided "as is", and our total liability to you is limited to
the amount you paid us in the 12 months before the claim.</p>

<h2>Ending the agreement</h2>
<p>You can delete your account at any time. We may close accounts that break these terms, and will tell you why.</p>

<h2>Law</h2>
<p>These terms are governed by the laws of India. Courts in {c['city']} have jurisdiction.</p>

<h2>Contact</h2>
<p>{c['contact']}</p>
"""


def _refunds(c: dict) -> str:
    return f"""
<p>We want you to be happy with Plateau Breaker. This policy covers paid plans bought from {c['entity']}.</p>

<h2>Try before you pay</h2>
<p>The free plan and the instant preview let you see your own analysis before paying anything.</p>

<h2>Monthly and yearly plans (Pro, Coach)</h2>
<ul>
<li><b>First purchase:</b> if it isn't for you, ask within 7 days of paying for a full refund, no questions asked.</li>
<li><b>Cancelling:</b> you can cancel any time. Your plan stays active until the end of the period you've paid for, and isn't renewed.</li>
<li><b>Renewals:</b> we email you before a yearly plan renews. Ask within 7 days of a renewal you didn't mean to make and we'll refund it.</li>
</ul>

<h2>Event Pass</h2>
<p>An Event Pass can be refunded in full within 48 hours of buying it if you haven't run a Prep Check with it.</p>

<h2>Service problems</h2>
<p>If the service is unavailable for a long stretch, or a feature you paid for is removed, we'll refund the unused
part of your plan.</p>

<h2>How to ask</h2>
<p>Write to {c['contact']} from your account email. Approved refunds go back to the original payment method
within 5–7 working days.</p>
"""


BODIES = {"privacy": _privacy, "terms": _terms, "refunds": _refunds}


def render(page: str, settings: Settings) -> str:
    c = _ctx(settings)
    title = TITLES[page]
    draft = "" if c["reviewed"] else (
        "<p class='legal-draft'>Draft: this text hasn't been reviewed by a lawyer yet. It describes how the service "
        "works today, but its legal wording may change.</p>")
    nav = " · ".join(f"<a href='/{p}'>{TITLES[p]}</a>" for p in PAGES if p != page)
    return (
        "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{title} · Plateau Breaker</title><link rel=icon type=image/png href='/static/icons/favicon-32.png'>"
        "<link rel=stylesheet href='/static/app.css'>"
        "<body class='legal-page'><main class='legal wrap'>"
        "<p><a class='brand' href='/'><span class='rook' aria-hidden='true'>♜&#xFE0E;</span>Plateau Breaker</a></p>"
        f"<h1>{title}</h1><p class='small muted'>Last updated {c['updated']}</p>{draft}"
        f"{BODIES[page](c)}<hr><p class='small muted'>{nav} · <a href='/'>Back to the app</a></p></main></body></html>"
    )
