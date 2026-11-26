"""GitHub organization audit log poller (REST `GET /orgs/{org}/audit-log`).

GitHub Enterprise Cloud only: on Free/Team plans the endpoint is unavailable.
Built from the vendor's documentation without a live org — everything assumed
is in modules/github_audit/API_NOTES.md under "To verify live".

Polls in [since, before] windows with a safety lag, using the documented
`created:<from>..<to>` search qualifier (second precision, `+00:00` offset)
and `order=asc`. Inside a window it pages through the `after` cursor from the
Link header's rel="next" URL. The next window starts exactly at the previous
`before`; the range is inclusive at both ends, so the boundary second can be
fetched twice — dedup on `_document_id` absorbs that, and nothing is skipped.
"""

import datetime
import os
import urllib.parse

from caib_pollers.framework import Ctx, FetchResult

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100  # documented maximum
INCLUDE_VALUES = ("web", "git", "all")
# GitHub does not document the audit log's indexing delay; 300s is the
# framework default and an item on the verify-live checklist.
WINDOW_LAG = datetime.timedelta(seconds=300)
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Flat scalar fields copied from the event into attrs under the same name.
# Each one is listed in modules/github_audit/MAPPING.md with its source.
SCALAR_ATTRS = (
    "org",
    "business",
    "repo",
    "user",
    "team",
    "operation_type",
    "visibility",
    "previous_visibility",
    "public_repo",
    "permission",
    "old_permission",
    "repository_selection",
    "programmatic_access_type",
    "user_programmatic_access_name",
    "oauth_application_name",
    "integration",
    "transport_protocol_name",
    "user_agent",
    "actor_is_agent",
)


def _rfc3339(value) -> str | None:
    """`@timestamp` / `created_at` are epoch milliseconds (documented as
    integers). Strings pass through so a shape change fails in the loader's
    dead_letter, not here."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        ts = datetime.datetime.fromtimestamp(value / 1000, datetime.UTC)
        return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"
    return str(value)


def _scopes(record: dict) -> str | None:
    """Classic-token / OAuth scopes, from whichever field the event carries:
    `token_scopes` on API-request events, `managed_token_scopes` /
    `managed_oauth_scopes` on org_credential_authorization.grant. Lists are
    joined with ","; strings pass through."""
    for key in ("token_scopes", "managed_token_scopes", "managed_oauth_scopes"):
        value = record.get(key)
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        if value not in (None, ""):
            return str(value)
    return None


def envelope(record: dict) -> dict:
    action = record.get("action") or "unknown"
    location = record.get("actor_location") or {}
    attrs = {k: record.get(k) for k in SCALAR_ATTRS}
    attrs["category"] = action.split(".", 1)[0] if "." in action else None
    attrs["branch"] = record.get("name") if action.startswith("protected_branch.") else None
    attrs["actor_country"] = location.get("country_code") or location.get("country_name")
    attrs["token_scopes"] = _scopes(record)
    return {
        "event_time": _rfc3339(record.get("@timestamp") or record.get("created_at")),
        "event_id": record.get("_document_id"),
        "actor": record.get("actor"),
        # Only present when the enterprise enabled IP disclosure.
        "src_ip": record.get("actor_ip"),
        "action": action,
        "target": (
            record.get("user")
            or record.get("repo")
            or record.get("repository")
            or record.get("team")
            or record.get("name")
            or record.get("org")
        ),
        # The audit log records actions that happened; it has no result field.
        "outcome": "success",
        "severity": "info",
        "attrs": {
            k: v
            for k, v in attrs.items()
            if v not in (None, "") and isinstance(v, (str, int, float, bool))
        },
        "raw": record,
    }


def _after_cursor(resp) -> str | None:
    """The `after` query value of the Link header's rel="next" URL."""
    url = (resp.links.get("next") or {}).get("url")
    if not url:
        return None
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("after")
    return values[0] if values else None


def _phrase_time(iso: str) -> str:
    """RFC3339 'Z' -> the documented `THH:MM:SS+00:00` form."""
    return iso.replace("Z", "+00:00")


class GitHubAuditPoller:
    module = "github_audit"
    interval_s = 300

    def fetch(self, cursor: dict | None, ctx: Ctx) -> FetchResult:
        token = os.environ["CAIB_GITHUB_TOKEN"]
        org = os.environ["CAIB_GITHUB_ORG"]
        api_url = (os.environ.get("CAIB_GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")
        include = os.environ.get("CAIB_GITHUB_AUDIT_INCLUDE") or "web"
        if include not in INCLUDE_VALUES:
            raise ValueError(
                f"CAIB_GITHUB_AUDIT_INCLUDE must be one of {INCLUDE_VALUES}, got {include!r}"
            )

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
            "phrase": f"created:{_phrase_time(since)}..{_phrase_time(before)}",
            "include": include,
            "order": "asc",
            "per_page": PER_PAGE,
        }
        if page_cursor:
            params["after"] = page_cursor

        resp = ctx.get(
            f"{api_url}/orgs/{org}/audit-log",
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        records = resp.json() or []
        envelopes = [envelope(r) for r in records]
        next_after = _after_cursor(resp)

        if next_after and records:
            return FetchResult(
                envelopes,
                {"since": since, "before": before, "page_cursor": next_after},
                has_more=True,
            )
        # Window sealed: the next one starts where this one ended.
        return FetchResult(envelopes, {"since": before}, has_more=False)


POLLER = GitHubAuditPoller()
