"""Slack Audit Logs API poller (Enterprise organizations only).

Built from Slack's docs on 2026-11-26 without a live org; every assumption is
in modules/slack_audit/API_NOTES.md with a verify-live checklist, and the
fixtures are SYNTHETIC.

Polls GET https://api.slack.com/audit/v1/logs in [oldest, latest] windows of
unix seconds with a safety lag; inside a window, pages via
response_metadata.next_cursor. Both bounds are inclusive and integer, so the
next window starts AT the previous `latest`: one second of overlap that dedup
absorbs, never a gap. Slack's cursors expire ("do not persist cursors for
hours or days"), so a 400 on a persisted page cursor restarts the window from
its start instead of crash-looping; auth errors still crash.
"""

import datetime
import os

import httpx

from caib_pollers.framework import Ctx, FetchResult

API_URL = "https://api.slack.com/audit/v1/logs"
# Documented maximum is 9999. One page is one transaction and one HTTP call
# under a 30s timeout, so ask for less; Tier 3 (50 calls/min, org-wide) is
# plenty of headroom for draining a backlog at this size.
PAGE_LIMIT = 1000
# Unknown indexing delay (see API_NOTES); don't read the last few minutes so a
# window is complete when we seal it.
WINDOW_LAG_S = 300
# Actions that record a denied attempt rather than a completed one.
FAILURE_ACTIONS = frozenset({"user_login_failed", "file_download_blocked"})


def _rfc3339(unix) -> str | None:
    """date_create (unix seconds) -> RFC3339 UTC. Anything unparseable gives
    None, which dead-letters the one record instead of killing the batch."""
    if isinstance(unix, bool):
        return None
    try:
        ts = datetime.datetime.fromtimestamp(float(unix), datetime.UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _inner(obj: dict) -> dict:
    """Slack nests the typed object under its own type name:
    {"type": "channel", "channel": {...}}."""
    inner = obj.get(obj.get("type") or "")
    return inner if isinstance(inner, dict) else {}


def _outcome(action: str) -> str:
    if action in FAILURE_ACTIONS:
        return "failure"
    if action == "anomaly":  # a detection about an actor, not an action taken
        return "unknown"
    return "success"


def envelope(entry: dict) -> dict:
    actor = entry.get("actor") or {}
    actor_user = _inner(actor)
    entity = entry.get("entity") or {}
    entity_obj = _inner(entity)
    context = entry.get("context") or {}
    location = context.get("location") or {}
    app = context.get("app") or {}
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    reasons = details.get("reason")
    action = entry.get("action") or "unknown"

    # The message entity has no id (team/channel/timestamp); fall back so the
    # target still says something.
    target = (
        entity_obj.get("id")
        or (
            f"{entity_obj.get('channel')}/{entity_obj.get('timestamp')}"
            if entity_obj.get("channel") and entity_obj.get("timestamp")
            else None
        )
        or entity_obj.get("name")
    )
    attrs = {
        "actor_type": actor.get("type"),
        "actor_id": actor_user.get("id"),
        "actor_team": actor_user.get("team"),
        "entity_type": entity.get("type"),
        "entity_name": entity_obj.get("name")
        or entity_obj.get("title")
        or entity_obj.get("email"),
        "channel_privacy": entity_obj.get("privacy"),
        "location_type": location.get("type"),
        "location_id": location.get("id"),
        "location_name": location.get("name"),
        "location_domain": location.get("domain"),
        "user_agent": context.get("ua"),
        "session_id": context.get("session_id"),
        "app_id": app.get("id"),
        "app_name": app.get("name"),
        "anomaly_reasons": ",".join(str(r) for r in reasons)
        if isinstance(reasons, list) and reasons
        else None,
    }
    return {
        "event_time": _rfc3339(entry.get("date_create")),
        "event_id": entry.get("id"),
        "actor": actor_user.get("email") or actor_user.get("id") or actor.get("type"),
        "src_ip": context.get("ip_address"),
        "action": action,
        "target": target,
        "outcome": _outcome(action),
        "severity": "info",
        "attrs": {k: v for k, v in attrs.items() if v not in (None, "")},
        "raw": entry,
    }


def _unix(dt: datetime.datetime) -> int:
    if dt.tzinfo is None:  # a naive CAIB_SLACK_AUDIT_BACKFILL means UTC
        dt = dt.replace(tzinfo=datetime.UTC)
    return int(dt.timestamp())


class SlackAuditPoller:
    module = "slack_audit"
    interval_s = 300

    def fetch(self, cursor: dict | None, ctx: Ctx) -> FetchResult:
        token = os.environ.get("CAIB_SLACK_AUDIT_TOKEN", "")
        if not token:
            raise SystemExit(
                "slack_audit: CAIB_SLACK_AUDIT_TOKEN is empty — set an Enterprise "
                "org owner's user token (xoxp-) with auditlogs:read in .env"
            )

        if cursor and cursor.get("page_cursor"):
            since, before = int(cursor["since"]), int(cursor["before"])
            page_cursor = cursor["page_cursor"]
        else:
            raw_since = (cursor or {}).get("since")
            since = int(raw_since) if raw_since is not None else _unix(ctx.backfill_start)
            before = _unix(datetime.datetime.now(datetime.UTC)) - WINDOW_LAG_S
            page_cursor = None
            if before <= since:  # window not open yet; keep waiting
                return FetchResult([], {"since": since}, has_more=False)

        params: dict = {"oldest": since, "latest": before, "limit": PAGE_LIMIT}
        if page_cursor:
            params["cursor"] = page_cursor
        try:
            resp = ctx.get(
                API_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except httpx.HTTPStatusError as exc:
            if page_cursor and exc.response.status_code == 400:
                # Expired/invalid cursor (persisted across a restart): re-read
                # the window from its start; dedup absorbs what we already have.
                ctx.log.warning(
                    "slack_audit: page cursor rejected (HTTP 400), restarting window %d..%d",
                    since, before,
                )
                return FetchResult([], {"since": since}, has_more=True)
            raise

        body = resp.json()
        if body.get("ok") is False or body.get("error"):
            # Web-API style error in a 200 body: crash loud, cursor untouched.
            raise RuntimeError(f"slack_audit: API error {body.get('error')!r}")
        entries = body.get("entries") or []
        envelopes = [envelope(e) for e in entries]
        next_page = (body.get("response_metadata") or {}).get("next_cursor")

        if next_page:
            return FetchResult(
                envelopes,
                {"since": since, "before": before, "page_cursor": next_page},
                has_more=True,
            )
        # Window sealed: the next one starts at this one's (inclusive) latest.
        return FetchResult(envelopes, {"since": before}, has_more=False)


POLLER = SlackAuditPoller()
