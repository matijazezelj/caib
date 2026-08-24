"""Cloudflare Audit Logs v2 poller.

Endpoint, pagination, and record shape verified 2026-08-24 — see
modules/cloudflare_audit/API_NOTES.md. Polls in [since, before) windows with a
safety lag; inside a window, pages via result_info.cursor with direction=asc.
The next window starts exactly at the previous `before`, so nothing is skipped
and nothing overlaps (dedup would absorb overlap anyway).
"""

import datetime
import os

from caib_pollers.framework import Ctx, FetchResult

API_BASE = "https://api.cloudflare.com/client/v4"
PAGE_LIMIT = 1000
# Cloudflare indexes audit records asynchronously; don't read the last few
# minutes so a window is complete when we seal it.
WINDOW_LAG = datetime.timedelta(seconds=300)
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _outcome(result) -> str:
    if isinstance(result, bool):
        return "success" if result else "failure"
    if isinstance(result, str) and result.lower() in ("success", "failure"):
        return result.lower()
    return "unknown"


def envelope(record: dict) -> dict:
    action = record.get("action") or {}
    actor = record.get("actor") or {}
    resource = record.get("resource") or {}
    zone = record.get("zone") or {}
    raw_http = record.get("raw") or {}
    attrs = {
        "actor_type": actor.get("type"),
        "actor_context": actor.get("context"),
        "resource_type": resource.get("type"),
        "resource_product": resource.get("product"),
        "resource_scope": resource.get("scope"),
        "zone_name": zone.get("name"),
        "http_method": raw_http.get("method"),
        "http_status": raw_http.get("status_code"),
        "http_uri": raw_http.get("uri"),
    }
    return {
        "event_time": action.get("time") or record.get("when"),
        "event_id": record.get("id"),
        "actor": actor.get("email") or actor.get("token_name") or actor.get("type"),
        "src_ip": actor.get("ip_address"),
        "action": action.get("type") or "unknown",
        "target": resource.get("id"),
        "outcome": _outcome(action.get("result")),
        "severity": "info",
        "attrs": {k: v for k, v in attrs.items() if v not in (None, "")},
        "raw": record,
    }


class CloudflareAuditPoller:
    module = "cloudflare_audit"
    interval_s = 300

    def fetch(self, cursor: dict | None, ctx: Ctx) -> FetchResult:
        token = os.environ["CAIB_CLOUDFLARE_API_TOKEN"]
        account_id = os.environ["CAIB_CLOUDFLARE_ACCOUNT_ID"]

        if cursor and cursor.get("page_cursor"):
            since, before = cursor["since"], cursor["before"]
            page_cursor = cursor["page_cursor"]
        else:
            since = (cursor or {}).get("since") or ctx.backfill_start.strftime(TIME_FMT)
            before = (datetime.datetime.now(datetime.UTC) - WINDOW_LAG).strftime(TIME_FMT)
            page_cursor = None
            if before <= since:  # window not open yet; keep waiting
                return FetchResult([], {"since": since}, has_more=False)

        params: dict = {
            "since": since,
            "before": before,
            "limit": PAGE_LIMIT,
            "direction": "asc",
        }
        if page_cursor:
            params["cursor"] = page_cursor

        resp = ctx.get(
            f"{API_BASE}/accounts/{account_id}/logs/audit",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        records = body.get("result") or []
        envelopes = [envelope(r) for r in records]
        next_page = (body.get("result_info") or {}).get("cursor")

        if next_page and records:
            return FetchResult(
                envelopes,
                {"since": since, "before": before, "page_cursor": next_page},
                has_more=True,
            )
        # Window sealed: the next one starts where this one ended.
        return FetchResult(envelopes, {"since": before}, has_more=False)


POLLER = CloudflareAuditPoller()
