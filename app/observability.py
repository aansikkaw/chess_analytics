"""Error monitoring (Sentry) and privacy-friendly analytics (Plausible or PostHog). Both optional.

* Sentry starts only when SENTRY_DSN is set and the `sentry-sdk` package is installed. Cookies,
  auth headers, emails and request bodies are scrubbed before anything leaves the server.
* Browser errors are sent to our own /api/client-error (no third-party script in the page)
  and forwarded to Sentry from the server.
* Analytics: the page loads Plausible or PostHog only if ANALYTICS is set, and never sends
  emails or usernames, just page views and a few named events (signup, sync, upgrade request).
"""

from __future__ import annotations

import logging
import re

from .config import Settings

log = logging.getLogger("plateau.obs")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_SENSITIVE_HEADERS = {"cookie", "authorization", "set-cookie", "x-forwarded-for"}
_state = {"sentry": False}


def scrub(text: str) -> str:
    return _EMAIL_RE.sub("[email]", text or "")


def _before_send(event: dict, hint: dict) -> dict:
    req = event.get("request") or {}
    req.pop("cookies", None)
    req.pop("data", None)  # request bodies can hold passwords and PGNs
    if req.get("query_string"):
        req["query_string"] = "[filtered]"  # may hold one-time tokens
    if isinstance(event.get("logentry"), dict):
        le = event["logentry"]
        le["message"] = scrub(str(le.get("message", "")))
        if le.get("params"):
            le["params"] = [scrub(str(x)) for x in le["params"]]
        if le.get("formatted"):
            le["formatted"] = scrub(str(le["formatted"]))
    crumbs = event.get("breadcrumbs")
    for b in (crumbs.get("values", []) if isinstance(crumbs, dict) else crumbs or []):
        if b.get("message"):
            b["message"] = scrub(b["message"])
        b.pop("data", None)
    headers = req.get("headers") or {}
    for h in list(headers):
        if h.lower() in _SENSITIVE_HEADERS:
            headers[h] = "[filtered]"
    if "user" in event:
        event["user"] = {"id": (event["user"] or {}).get("id")}
    for ex in (event.get("exception") or {}).get("values", []):
        if ex.get("value"):
            ex["value"] = scrub(ex["value"])
    if event.get("message"):
        event["message"] = scrub(event["message"])
    return event


def init_sentry(settings: Settings, component: str) -> bool:
    """Start Sentry if configured. Safe to call more than once."""
    if not settings.sentry_dsn or _state["sentry"]:
        return _state["sentry"]
    try:
        import sentry_sdk
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk isn't installed: pip install sentry-sdk")
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        release=f"plateau-breaker@{settings.app_version}",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        include_local_variables=False,  # stack-frame locals would include passwords and tokens
        max_request_body_size="never",
        before_send=_before_send,
    )
    sentry_sdk.set_tag("component", component)
    _state["sentry"] = True
    return True


def sentry_enabled() -> bool:
    return _state["sentry"]


def capture_client_error(report: dict) -> None:
    """A browser error relayed through /api/client-error."""
    msg = scrub(f"[browser] {report.get('message', 'error')}")[:500]
    log.warning("%s at %s:%s", msg, report.get("source", "?"), report.get("line", "?"))
    if not _state["sentry"]:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", "browser")
        for k in ("source", "line", "col", "page", "ua"):
            if report.get(k) is not None:
                scope.set_extra(k, scrub(str(report[k]))[:500])
        if report.get("stack"):
            scope.set_extra("stack", scrub(str(report["stack"]))[:4000])
        sentry_sdk.capture_message(msg, level="error")


def analytics_config(settings: Settings) -> dict:
    """What the page needs to load the chosen analytics tool (or nothing)."""
    if settings.analytics == "plausible" and settings.plausible_domain:
        return {"provider": "plausible", "domain": settings.plausible_domain, "src": settings.plausible_src}
    if settings.analytics == "posthog" and settings.posthog_key:
        return {"provider": "posthog", "key": settings.posthog_key, "host": settings.posthog_host}
    return {"provider": "none"}
