# slack_audit — API notes

Built 2026-11-26 **without a live Enterprise org or token**. Sources, read that
day:

- Using the Audit Logs API — <https://docs.slack.dev/admins/audit-logs-api/>
- Audit Logs API methods & actions —
  <https://docs.slack.dev/reference/audit-logs-api/methods-actions-reference/>
- Anomalous events — <https://docs.slack.dev/reference/audit-logs-api/anomalous-events-reference/>
- Web API pagination (cursors) — <https://docs.slack.dev/apis/web-api/pagination/>

Each fact below is tagged with how it is known:

- **[docs]**: stated in Slack's current docs.
- **[live, no auth]**: checked against `api.slack.com` on 2026-11-26 without a
  token (the public `/schemas` and `/actions` endpoints, and error responses).
- **[assumed]**: inferred, not stated anywhere. Each one has a line in the
  verify-live checklist at the bottom.

## Plan / edition

- **[docs]** Enterprise plan only ("Enterprise organization", formerly
  Enterprise Grid). On anything else the API answers `feature_not_enabled` /
  `invalid_workspace` / `team_not_authorized`.
- **[docs]** The Slack Developer Program can provision a free "fully-featured"
  Enterprise sandbox. **[assumed]** that the sandbox serves `/audit/v1/logs`;
  if it does, live verification no longer needs a production org.

## Auth

- **[docs]** `Authorization: Bearer xoxp-...`: a **user** token with the
  `auditlogs:read` user scope, from an app installed **on the Enterprise
  organization** (not a workspace inside it) **by an Org Owner**. Tokens from
  Org *Admins* are explicitly not supported (`user_not_authorized`).
- **[docs]** Installing needs an OAuth flow: add the user scope, activate public
  distribution, then open the shareable URL as the Org Owner and pick the org
  in the install dropdown. Slack notes a bot scope may be needed to get the org
  install to go through.
- **[live, no auth]** No `Authorization` header: HTTP **400** with
  `content-type: application/json` and an **empty body**. An invalid `xoxp-`
  token: HTTP **401**, empty body. `Ctx.get` raises on both, so the poller
  crashes loudly, which is what we want.
- **[assumed]** Errors arrive as HTTP 4xx, not as `{"ok": false}` in a 200.
  The poller handles both: a 200 body with `ok: false` or `error` raises.
- **[assumed]** The token does not expire. Slack user tokens without token
  rotation are long-lived, but they die when the owner is deactivated or the
  app is uninstalled. The dead-man rule is the pager for that.

## Endpoint

- **[docs]** `GET https://api.slack.com/audit/v1/logs`. The host is
  `api.slack.com`, not the `slack.com/api/` Web API family.
- **[docs] [live, no auth]** `GET /audit/v1/schemas` and `GET /audit/v1/actions`
  need no auth. Both returned 200 JSON on 2026-11-26. `/actions` listed about
  900 action names across 29 entity groups, and every action literal used in
  `detect/rules/slack_audit/*.yaml` and in the fixtures was checked against it:

  ```sh
  curl -s https://api.slack.com/audit/v1/actions \
    | jq -r '.actions[][]' | grep -x -e anomaly -e role_change_to_admin  # etc.
  ```

## Parameters, cursoring, time fields

- **[docs]** `oldest` and `latest` are integer unix seconds, **both inclusive**.
  "Data is not available prior to March 2018". A range before the feature was
  enabled, or in the future, gives `invalid_range`.
- **[docs]** `limit` maximum is **9999**, and the limit is "optimistic": you can
  get *more* than `limit` when several entries share a timestamp. The module
  asks for 1000, because each page is one transaction and one HTTP call under
  a 30 s timeout.
- **[docs]** Default order is **descending** (newest first). That doesn't matter
  here because a window is only sealed after its last page.
- **[docs]** Pagination: Slack's standard cursor scheme. Pass
  `response_metadata.next_cursor` back as `cursor`. An empty, null or absent
  `next_cursor` means done.
  **[assumed]** `/audit/v1/logs` uses exactly that envelope (the audit docs only
  show `entries` and link to the generic cursor docs), and the window
  filters must be re-sent with each `cursor`. The poller re-sends them.
- **[docs]** "Cursors expire … do not persist cursors for hours or days."
  The framework persists the page cursor, so a restart after a long outage can
  present an expired cursor. The poller treats **HTTP 400 while paging** as
  `invalid_cursor` and restarts the window from `since` (dedup absorbs the
  re-read). 401/403 still crash.
- **[docs]** `date_create` is integer unix seconds, mapped to RFC3339 UTC.
- Optional server-side filters `action` (comma list, max 30), `actor`, `entity`
  exist. The module doesn't use them, because detection wants everything.

Poller cursor dict: `{"since": <unix>}` between windows;
`{"since": <unix>, "before": <unix>, "page_cursor": str}` while paging. A window
is `[since, before]` with `before = now() - 300s`. The next window starts **at**
the previous `before`, not `before + 1`. Both bounds are inclusive, so one second
overlaps and dedup absorbs it, while `+1` could open a gap if Slack compares
sub-second internal times.

## Rate limits

- **[docs]** Tier 3: about 50 calls/minute with bursts, counted **per
  organization**, not per app. A second SIEM polling the same org shares the
  budget. The framework honours `Retry-After` on 429.
- At `interval_s = 300` the steady state is a few calls per 5 minutes. A
  backfill drains at most 1000 entries per call.

## Retention / backfill window

- **[docs]** The only documented bound is "not available prior to March 2018".
  **[assumed]** How far back the API actually serves an org's data is not
  documented. First-run backfill defaults to `now() - 24h`
  (`CAIB_SLACK_AUDIT_BACKFILL` to override).

## Payload shape

- **[docs]** Response: `{"entries": [...], "response_metadata": {"next_cursor"}}`.
  Entry: `id` (UUID, used for dedup), `date_create`, `action`,
  `actor{type, user{id, name, email, team}}`,
  `entity{type, <type>{...}}`,
  `context{location{type, id, name, domain}, ua, ip_address, session_id, app?}`,
  `details?`.
- **[docs]** The typed object is nested under its own type name
  (`{"type": "channel", "channel": {...}}`). **[live, no auth]** `/schemas`
  lists the entity types: workspace, enterprise, user, file, channel, app,
  workflow, usergroup, barrier, message, role, account_type_role, canvas,
  workflow_v2, template, legal_hold_policy, work_object_record. `message` has
  no `id` (team/channel/timestamp), so `target` falls back to `channel/timestamp`.
- **[docs]** Special actors: `USLACKSECURITY` (credentials reset by Slack
  security) and `USLACKUSER` (placeholder when an event has no actor). For
  eDiscovery `file_downloaded`, the actor is the app's installer.
- **[docs]** `context.app` appears only on app-triggered events.
- **[docs]** `anomaly` entries carry `details.reason[]` (`ip_address`, `asn`,
  `tor`, `session_fingerprint`, `excessive_downloads`, `unexpected_scraping`,
  …), plus `previous_ip_address`, `previous_ua`, `ip_address_details[]`,
  `location` and `action_timestamp` (microseconds).
- **[docs]** `file_downloaded` means "downloaded **or viewed** within Slack", so
  volume is high. That is why the bulk rule counts distinct files.
- **[docs]** Slack has no `user_role_changed` action, which the original
  recipe assumed. Role changes are separate actions: `role_change_to_admin`,
  `role_change_to_owner`, `role_change_to_user`, `role_change_to_guest`,
  `owner_transferred`, `role_assigned` (custom admin roles).
- **[assumed]** The key names inside `details` for setting changes. Slack says
  "previous and new values", but the names are not documented; the fixture's
  `previous_value` / `new_value` are invented. No rule or attr reads them.
- **[docs]** When Slackbot acts for a user, `acting_agent` and `agent_message`
  are added. **[assumed]** Where they sit (top level or `details`) is
  undocumented, so they are not mapped yet.

## Fixtures

`fixtures/logs_page1.json` and `logs_page2.json` are **SYNTHETIC**: built
from the documented shapes and real action names, not captured from an org.
Each file carries a `_comment` saying so. Replace them with sanitized real
pages at first live run, then re-run `tests/test_slack_audit.py` and update
assertions that relied on invented values.

## To verify live (`caib-poller --module slack_audit --once`)

- [ ] **Edition:** does a Developer Program Enterprise sandbox serve
      `/audit/v1/logs`? (It would unblock everything below.)
- [ ] **Auth:** an Org Owner `xoxp-` token with `auditlogs:read` returns 200. Record
      the exact install steps that worked, including whether a bot scope was
      needed.
- [ ] **Auth errors:** wrong scope or an admin's token gives a 4xx status with
      an empty or JSON body. Record the body and confirm the poller crashes.
- [ ] **Endpoint/shape:** the response is `{"entries": [...], "response_metadata":
      {"next_cursor": ...}}`, with no `ok` key or a different wrapper.
- [ ] **Pagination:** `next_cursor` is `""` on the last page. Paging with
      `cursor` plus the same `oldest`/`latest` returns the next page and no
      duplicates.
- [ ] **Cursor expiry:** an old cursor gives HTTP 400 (`invalid_cursor`), which
      the poller relies on to restart the window. If it is another status,
      update `fetch()`.
- [ ] **Time fields:** `date_create` is an integer (not float/string). `oldest`
      and `latest` are inclusive.
- [ ] **Late arrival:** do entries, especially `anomaly`, appear with a
      `date_create` older than `now() - 300s`? If so, raise `WINDOW_LAG_S` or
      add a re-read overlap. A sealed window is never read again.
- [ ] **limit:** is 1000 honoured? Does 9999 work, and is it useful?
- [ ] **Rate limits:** the 429 carries `Retry-After`. Note the org-wide budget if
      another SIEM is also polling.
- [ ] **Retention:** how far back does `oldest` return data for this org?
      Record it, and decide whether a deeper first backfill is worth it.
- [ ] **Payload:** `actor.user.email` is present for normal users, and what bot
      or app actors look like. `context.ip_address` is a valid IP (the docs'
      own sample `1.23.45.678` is not).
- [ ] **details:** real key names for `pref.sso_setting_changed` and
      `pref.two_factor_auth_changed`. If stable, surface them as attrs and let
      `auth_settings_changed` tell weakening from hardening.
- [ ] **anomaly:** `details.reason` is always a list of strings.
- [ ] **Volume:** `file_downloaded` volume per user per 30 min, to tune
      `bulk_file_download` (50 distinct files). Overall daily volume, to
      confirm the 6h dead-man window.
- [ ] **Slackbot fields:** where `acting_agent` / `agent_message` sit, and
      whether they deserve attrs.
