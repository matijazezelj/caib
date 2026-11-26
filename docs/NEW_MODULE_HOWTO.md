# Adding a module to CAIB

Every log source is a module that ships everything end-to-end. This is the
complete recipe, distilled from building `aws_cloudtrail`, `gcp_audit`,
`cloudflare_audit`, `azure_activity`, `github_audit`, `google_workspace` and
`slack_audit` — plus ready-to-build recipes for `zoom` and `knowbe4` at the end
(the `slack_audit` section records what changed from its recipe).

## Definition of done (from the spec — all of it, in one commit series)

- [ ] Collector config (`vector/modules/<m>.yaml`) **or** poller class
      (`pollers/caib_pollers/modules/<m>.py`)
- [ ] `modules/<m>/MAPPING.md` — envelope field table + every attrs key with its
      vendor source path (rules may only use documented attrs, never `raw`)
- [ ] `modules/<m>/API_NOTES.md` — endpoints/auth/pagination/rate limits as
      **verified**, plus an explicit "to verify live" checklist for anything assumed
- [ ] ≥2 sanitized fixtures in `modules/<m>/fixtures/` (real shapes; if
      unobtainable mark SYNTHETIC in API_NOTES and replace at first live run)
- [ ] Fixture tests green (`tests/test_<m>*.py`)
- [ ] Dashboard JSON in `grafana/provisioning/dashboards/json/`
- [ ] ≥3 seed rules **plus the dead-man rule** in `detect/rules/<m>/`
- [ ] `modules/<m>/README.md` — vendor-side setup (copy-paste CLI where
      possible) + CAIB env vars
- [ ] Compose service behind a profile named exactly `<m>`; env vars in
      `.env.example`; row in the top-level README module matrix
- [ ] Seeding support in `scripts/demo_seed.py` if the module should appear in
      `make demo`

Golden rule: **verify vendor API details against current docs before
implementing** — write what you confirmed into API_NOTES.md, and log anything
that contradicts assumptions in DECISIONS.md.

## 1. Choose the path

- **Poller** (most SaaS APIs): OAuth/token auth, cursors, pagination, 429s.
  The framework owns all of that; the module is one `fetch()` method.
- **Push through Vector** (cloud-native delivery: S3/SQS, Pub/Sub): use it when
  the vendor pushes to infrastructure — Vector's sources and disk buffers are
  the hard part you don't want to rewrite.

## 2. The envelope contract (the only producer interface)

Every producer writes this shape into `ingest.landing.payload`; the loader is
the only code that maps envelope → columns:

```jsonc
{
  "event_time": "2026-08-23T10:41:07Z",   // required, RFC3339 UTC
  "event_id":   "abc-123",                // vendor's stable id, or null → canonical hash
  "actor":      "who did it, or null",
  "src_ip":     "string or null (unparseable becomes NULL, not an error)",
  "action":     "vendor's verb, unmodified — required",
  "target":     "object acted on, or null",
  "outcome":    "success | failure | unknown",
  "severity":   "info|low|medium|high|critical — map conservatively, default info",
  "attrs":      { "flat": "snake_case, every key documented in MAPPING.md" },
  "raw":        { "original vendor event, verbatim" }
}
```

Dedup: `sha256(module:event_id)`, derived by the loader. No stable vendor id →
leave `event_id` null and the loader hashes `event_time|actor|action|target` —
make sure that tuple is actually unique for your source (see the zoom recipe).

## 3a. Poller path

Copy `azure_activity.py` (OAuth client-credentials) or `cloudflare_audit.py`
(static token) as your starting point. You implement exactly one thing:

```python
class MyPoller:
    module = "my_module"
    interval_s = 300
    def fetch(self, cursor: dict | None, ctx: Ctx) -> FetchResult: ...
POLLER = MyPoller()
```

What the framework already does — don't reimplement any of it: cursor
persistence (one transaction with the landing insert, so mid-batch kills are
safe), HTTP retries with backoff/jitter/Retry-After via `ctx.get()`, envelope
validation → dead_letter, `--once`, structured per-cycle logging, first-run
backfill start (`CAIB_<MODULE>_BACKFILL`, default now()-24h) via
`ctx.backfill_start`, and `OAuth2ClientCredentials` for token caching.

The proven cursor pattern (windowed + in-window pagination):

```
between windows:  {"since": <iso>}
while paging:     {"since": ..., "before": ..., "page_cursor"/"next_link": ...}
```

Seal windows at `now() - lag` (pick the lag from the vendor's ingestion delay;
300s is a good default). The next window starts exactly at the previous
`before`. Overlap is harmless (dedup), gaps are not. Set `has_more=True` while
a page cursor remains — the framework loops immediately (that's also how
backfill drains).

Crash rules: a bad token or exhausted retries should **crash the process** —
compose restarts it, the cursor is untouched, and the dead-man rule is the
pager if it keeps crashing. Never swallow auth errors.

Tests to copy from `tests/test_azure_activity.py` / `test_cloudflare_audit.py`:
envelope mapping per fixture record, pagination via a stubbed ctx, an
end-to-end `run_cycle → loader → re-poll → dedup` against a real throwaway DB.

## 3b. Push path (Vector)

Copy `vector/modules/gcp_audit.yaml` (one event per message) or
`aws_cloudtrail.yaml` (one object containing many records → array fan-out).
The remap MUST end with the event(s) assigned to `.` as
`{"received_at": now(), "module": "<m>", "payload": <envelope>}` — an array at
the root fans out to one landing row per element.

VRL gotchas that cost us real debugging time (all Vector 0.57.0):

- **`if` expressions are not valid as object-literal values.** Compute into a
  variable first, then reference it.
- **A remap's output is `.`** — a trailing object literal that isn't assigned
  to `.` is silently discarded.
- `??` on an infallible expression is a compile error (E651) — only coalesce
  things that can actually fail.
- `compact({...})` drops null values — build attrs with nullable lookups and
  compact once. Booleans (`false`) survive compact.
- Statement-position `{` starts a block, not an object literal.

Compose: each push module gets its own Vector service
(`vector-<m>`) behind its profile, sharing `vector/vector.yaml` (the postgres
sink) plus its module file, with its own disk-buffer volume. Copy
`VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION: "true"` into its
`environment:`: since Vector 0.57 `${CAIB_*}` in the config stays a literal
string without it (`tests/test_compose_env.py` checks every Vector service).

Tests to copy from `tests/test_vector_gcp.py` / `test_vector_cloudtrail.py`:

1. `vector validate --no-environment` with dummy env — catches config/VRL
   compile errors.
2. `vector test` with a generated tests-yaml (fan-out sources only).
3. stdin → real remap → postgres sink → loader, asserting mapped columns in
   the DB. If the source itself is risky (framing, compression), emulate it —
   the CloudTrail LocalStack test is the template (note: containers must share
   a docker network; `host.docker.internal` can't reach loopback-bound host
   ports on Linux).

## 4. Rules

Read `detect/rules/README.md` first (the SQL contract, the three shapes, the
`%%` escaping). Non-negotiables:

- **Every module ships a dead-man rule.** Window sized to the source's normal
  chatter (CloudTrail 2h, clouds 6h, sparse SaaS 24h).
- Rules read only attrs documented in MAPPING.md. If a detection needs a field
  that's only in `raw`, extend the collector to surface it as an attr (that's
  what `policy_arn`/`world_open`/`roles_added` are).
- Where a rule is based on a SigmaHQ rule, cite its real ID, title, author and
  link plus the Detection Rule License 1.1 (see `detect/rules/README.md`).

## 5. Dashboard

Copy `caib-module-cloudflare-audit.json`, change uid/title/module filter, keep
the pattern: events/hour by outcome (from `caib.events_5m`), top actors
(`caib.actors_1h`), top actions, recent failures, 1–2 module-specific panels
(list them in the module README), and the **Raw event stream** panel. Panels
wider than 7 days must read the caggs, not `caib.events`.

## 6. Wiring checklist

`docker-compose.yml` service behind profile `<m>` → `.env.example` vars
(commented, no secrets) → README module matrix row → `scripts/demo_seed.py`
seeding (import the poller's `envelope()`; VRL-based modules need a small
python mirror like `ct_envelope`) → `uv run pytest` green → `make demo` still
works.

---

# Recipes for the deferred modules

Best-known details as of 2026-08-24, **not yet verified against live APIs** —
verifying them (and recording results in API_NOTES.md) is step one of each
build. Table stakes from the spec plus implementation notes.

## slack_audit (poller) — BUILT (SYNTHETIC, live verification pending)

Built 2026-11-26: `pollers/caib_pollers/modules/slack_audit.py`, and
`modules/slack_audit/API_NOTES.md` is now the source of truth. The recipe was
checked against docs.slack.dev and the unauthenticated `/audit/v1/schemas` and
`/actions` endpoints. Corrections to the 2026-08-24 version:

- **Auth**: the token must belong to an Enterprise **Org Owner**. Org Admin
  tokens are explicitly not supported (`user_not_authorized`). The rest stood:
  an org-level install, a user token with `auditlogs:read`, and `Bearer xoxp-...`.
- **There is no `user_role_changed` action.** The new role is in the action
  name: `role_change_to_admin`, `role_change_to_owner`, `owner_transferred`,
  `role_assigned` (custom admin roles), and so on. No `details` parsing is
  needed, and the planned `details_type` attr was dropped: `details` has no
  documented `type` key.
- **Params**: `oldest`/`latest` are **both inclusive**, so the next window
  starts AT the previous `latest` (1s overlap, dedup absorbs it). Default order
  is **descending**. `limit` max 9999 is confirmed, but it is "optimistic" (it can
  return more when timestamps tie). The module asks for 1000.
- **Cursors expire** (Slack: "do not persist cursors for hours or days"). The
  framework persists them, so a 400 while paging restarts the window instead of
  crash-looping.
- **Rate limit**: Tier 3, about 50 calls/min, counted **per organization**, so
  it is shared with any other SIEM polling the same org.
- **Errors** (checked live, no auth): a missing token gets HTTP 400 and a bad
  token gets HTTP 401, both with an empty body.
- **`file_downloaded` also fires on in-app views**, so ≥10 per 30 min was far too
  noisy. The rule now needs ≥50 *distinct* files per actor in 30 minutes.
- **Better seed rule found**: Slack's own `anomaly` action (Tor, IOC-listed
  IPs, session-fingerprint mismatch, scraping) with `details.reason[]`. It is
  surfaced as `attrs.anomaly_reasons`.
- `pref.%` alone was too broad: there are 100+ prefs, mostly cosmetic. The
  auth-settings rule names specific actions instead (SSO, 2FA, session length,
  IP ranges, IdP config). A separate rule covers exports
  (`manual_export_started`, `corporate_exports_enabled`, …).
- **Sandbox**: the Slack Developer Program offers a free Enterprise sandbox.
  Whether it serves audit logs is the first item on the verify-live list, and
  it may remove the need for a production Grid org.

## zoom (poller, two streams under one module)

- **Auth**: Server-to-Server OAuth — `POST https://zoom.us/oauth/token?grant_
  type=account_credentials&account_id=<id>` with Basic auth
  `client_id:client_secret`. Use the framework's `OAuth2ClientCredentials`
  with `basic_auth=` (built for exactly this). Token TTL 3600s.
- **Streams** (both under module `zoom`, distinguished by `attrs.stream`; the
  cursor dict holds one sub-cursor per stream; `fetch()` should pull one page
  from each per call and set `has_more` if either has more):
  - `GET /v2/report/operationlogs?from=YYYY-MM-DD&to=YYYY-MM-DD&page_size=300
    &next_page_token=...` → `operation_logs[]: {time, operator, category_type,
    action, operation_detail}`. Admin/settings changes.
  - `GET /v2/report/activities?from&to&page_size&next_page_token` →
    `activity_logs[]: {email, time, type, ip_address, client_type, version}`.
    Sign-in/out events.
- **Date-window quirk**: `from`/`to` are dates, max ~1 month span. Poll the
  current day repeatedly and advance the date cursor at day rollover —
  re-fetching the same day is fine because dedup absorbs it, BUT:
- **No stable event id** → canonical hash. `event_time|actor|action|target`
  must be filled so the tuple is unique: for operationlogs put
  `operation_detail` in target; for activities put `client_type` in target or
  accept that two identical sign-ins in the same second collapse (document the
  choice in MAPPING.md).
- **Mapping**: operationlogs — `operator`→actor, `action`→action,
  `operation_detail`→target, attrs `{stream: "operationlogs", category_type}`;
  activities — `email`→actor, `type`→action ("Sign in"/"Sign out"),
  `ip_address`→src_ip, attrs `{stream: "activities", client_type, version}`.
  Outcome `success`, severity `info`.
- **Seed rules** (spec lists exactly these two + dead-man): security/account
  settings changed (`attrs.stream='operationlogs' AND category_type IN
  ('Account','Security')`); admin role grants (`category_type='Role'`);
  dead-man 24h (ops logs are sparse). New-geo-per-actor needs state — out of
  scope per spec, do not attempt.
- **Verify live**: exact `category_type` vocabulary; `page_size` max;
  report-API rate limits (they're low — keep `interval_s` ≥ 300); whether
  `/report/*` requires a paid plan tier.

## knowbe4 (poller)

- **Endpoint**: User Events API — `GET https://api.events.knowbe4.com/events`
  (US) / `https://api-eu.events.knowbe4.com/events` (EU) — region host from
  env. (This is separate from the Reporting API on api.knowbe4.com.)
- **Auth**: `Authorization: Bearer <key>` — key from the KnowBe4 console
  (Account Settings → API).
- **Params**: `occurred_date` (YYYY-MM-DD) or `from_date`/`to_date` range —
  **confirm which the current API accepts**; `page`/`per_page` (max 100)
  paging; `event_type` filter available.
- **Item shape**: `{id, user{email, id}, event_type{name, description},
  occurred_date, risk_level/risk_score?, source}` under a `data[]` envelope
  with `meta` paging info.
- **Mapping**: `id`→event_id (dedup); `occurred_date`→event_time (midnight
  UTC if date-only — document it); `user.email`→actor; `event_type.name`→
  action; outcome `unknown`; attrs: `risk_score`, `source`. src_ip: usually
  absent → null.
- **Cursoring**: date-based windows (a day at a time) + page numbers; dedup
  absorbs same-day re-fetches. Very low volume — `interval_s` 900 is plenty.
- **Seed rules** (spec lists exactly these two + dead-man): repeat
  phishing-test failure per user (threshold on phishing-failure event types,
  ≥2 in 30d — long lookback, this is a slow signal); training past-due burst;
  dead-man 24h.
- **Verify live**: exact event_type names for phishing failures (the fixture
  you capture decides the rule's filter); pagination param names; rate limits
  (documented as strict — respect Retry-After, which the framework does).
