"""Azure Activity Log poller (subscription management plane — the CloudTrail /
GCP Audit analog). Not in the original spec; added per user direction to cover
all three major clouds. See modules/azure_activity/API_NOTES.md.

Polls the ARM Activity Log REST API in [since, before] windows with a safety
lag; pages via the response's nextLink (a complete URL). Auth is OAuth2 client
credentials against Entra ID, token cached with early refresh.
"""

import datetime
import os

from caib_pollers.framework import Ctx, FetchResult, OAuth2ClientCredentials

ARM_BASE = "https://management.azure.com"
API_VERSION = "2015-04-01"
# The Activity Log ingests with a few minutes of delay; don't read the last
# few minutes so a window is complete when we seal it.
WINDOW_LAG = datetime.timedelta(seconds=300)
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

OUTCOME = {"Succeeded": "success", "Failed": "failure"}


def envelope(record: dict) -> dict:
    status = (record.get("status") or {}).get("value")
    http_req = record.get("httpRequest") or {}
    attrs = {
        "category": (record.get("category") or {}).get("value"),
        "resource_group": record.get("resourceGroupName"),
        "resource_provider": (record.get("resourceProviderName") or {}).get("value"),
        "level": record.get("level"),
        "status": status,  # raw status incl. Started/Accepted
        "sub_status": (record.get("subStatus") or {}).get("value"),
    }
    return {
        "event_time": record.get("eventTimestamp"),
        "event_id": record.get("eventDataId"),
        "actor": record.get("caller"),
        "src_ip": http_req.get("clientIpAddress"),
        "action": (record.get("operationName") or {}).get("value") or "unknown",
        "target": record.get("resourceId"),
        # Started/Accepted phases map to unknown; rules filter on
        # attrs.status = 'Succeeded' where completion matters.
        "outcome": OUTCOME.get(status, "unknown"),
        "severity": "info",
        "attrs": {k: v for k, v in attrs.items() if v not in (None, "")},
        "raw": record,
    }


class AzureActivityPoller:
    module = "azure_activity"
    interval_s = 300

    def __init__(self):
        self._oauth: OAuth2ClientCredentials | None = None

    def _auth_headers(self, ctx: Ctx) -> dict:
        if self._oauth is None:
            tenant = os.environ["CAIB_AZURE_TENANT_ID"]
            self._oauth = OAuth2ClientCredentials(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": os.environ["CAIB_AZURE_CLIENT_ID"],
                    "client_secret": os.environ["CAIB_AZURE_CLIENT_SECRET"],
                    "scope": "https://management.azure.com/.default",
                },
            )
        return {"Authorization": f"Bearer {self._oauth.token(ctx.http)}"}

    def fetch(self, cursor: dict | None, ctx: Ctx) -> FetchResult:
        subscription = os.environ["CAIB_AZURE_SUBSCRIPTION_ID"]
        headers = self._auth_headers(ctx)

        if cursor and cursor.get("next_link"):
            since, before = cursor["since"], cursor["before"]
            resp = ctx.get(cursor["next_link"], headers=headers)
        else:
            since = (cursor or {}).get("since") or ctx.backfill_start.strftime(TIME_FMT)
            before = (datetime.datetime.now(datetime.UTC) - WINDOW_LAG).strftime(TIME_FMT)
            if before <= since:  # window not open yet
                return FetchResult([], {"since": since}, has_more=False)
            resp = ctx.get(
                f"{ARM_BASE}/subscriptions/{subscription}/providers"
                "/Microsoft.Insights/eventtypes/management/values",
                params={
                    "api-version": API_VERSION,
                    "$filter": (
                        f"eventTimestamp ge '{since}' and eventTimestamp le '{before}'"
                    ),
                },
                headers=headers,
            )

        body = resp.json()
        records = body.get("value") or []
        envelopes = [envelope(r) for r in records]
        if next_link := body.get("nextLink"):
            return FetchResult(
                envelopes,
                {"since": since, "before": before, "next_link": next_link},
                has_more=True,
            )
        return FetchResult(envelopes, {"since": before}, has_more=False)


POLLER = AzureActivityPoller()
