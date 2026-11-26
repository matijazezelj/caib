# slack_audit

Polls the Slack Audit Logs API for one **Enterprise organization** (formerly
Enterprise Grid) into CAIB. Not available on Free, Pro or Business+ plans.

> **Status: SYNTHETIC.** This module was built from Slack's docs; it has not
> yet met a live org. See `API_NOTES.md` for the verify-live checklist.

## Vendor-side setup

You need to be an **Org Owner**. Org Admin tokens are rejected.

1. <https://api.slack.com/apps> → **Create New App** → From scratch. Pick any
   workspace in the org as the development workspace.
2. **OAuth & Permissions** → **User Token Scopes** → add `auditlogs:read`.
   If the org-level install later refuses, also add any harmless bot scope.
3. **Manage Distribution** → complete the checklist → **Activate Public
   Distribution**. Slack requires this for org-level installs; the app is not
   listed anywhere.
4. Open the **Sharable URL** while logged in as the Org Owner. In the
   install screen's top-right dropdown, choose the **organization**, not a
   workspace, then **Allow**.
5. Copy the resulting **User OAuth Token** (`xoxp-…`) from OAuth &
   Permissions.

Quick check (replace the token):

```sh
curl -s -H "Authorization: Bearer xoxp-…" \
  "https://api.slack.com/audit/v1/logs?limit=1" | jq .
```

## CAIB-side setup

In `.env`:

```
CAIB_SLACK_AUDIT_TOKEN=xoxp-...
# optional; first-run backfill start, default now()-24h:
CAIB_SLACK_AUDIT_BACKFILL=2026-08-01T00:00:00Z
```

Then:

```sh
docker compose --profile slack_audit up -d
```

One-shot drain (tests/demo): `caib-poller --module slack_audit --once`.

The rate limit (Tier 3, about 50 calls/min) is shared **across the whole org**.
If another SIEM already polls this org, both share the budget.

## Detection rules (`detect/rules/slack_audit/`)

| Rule | Severity | Fires on |
|---|---|---|
| `admin_role_granted` | high | `role_change_to_admin` / `_owner`, `owner_transferred`, `role_assigned` |
| `anomaly_detected` | high | Slack's own `anomaly` events (Tor, IOC IPs, session fingerprint, scraping, …) |
| `auth_settings_changed` | high | SSO / 2FA / session-length / IP-range / IdP config changes |
| `data_export_started` | high | standard, corporate, scheduled, channel or user exports; corporate exports enabled |
| `app_installed_or_scopes_expanded` | medium | `app_installed`, `app_scopes_expanded`, `org_app_upgraded_to_org_install` |
| `bulk_file_download` | medium | ≥ 50 distinct files `file_downloaded` by one actor in 30 min |
| `dead_man` | high | no slack_audit events for 6 h |

## Module-specific dashboard panels

- **Slack anomaly reasons (24h)**: anomaly events grouped by
  `details.reason`, with distinct actors.
- **Activity by workspace / org (24h)**: event volume per
  `context.location`, which shows which workspace in the org is busy.
