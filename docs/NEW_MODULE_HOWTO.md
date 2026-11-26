# Adding a module to CAIB

Every log source is a module that ships everything end-to-end. This is the
complete recipe, distilled from building `aws_cloudtrail`, `gcp_audit`,
`cloudflare_audit`, and `azure_activity` — plus ready-to-build recipes for
`slack_audit`, `zoom`, and `knowbe4` at the end.

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
- Where a rule maps to a public Sigma rule, note the Sigma ID in a comment.

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

## slack_audit (poller)

- **Endpoint**: `GET https://api.slack.com/audit/v1/logs` — note: hosted on
  `api.slack.com` directly, not the usual `slack.com/api/` methods family.
- **Auth**: Enterprise Grid **org-level** user token with `auditlogs:read`
  (install the app at org level; a workspace token will not work). Header
  `Authorization: Bearer xoxp-...`. Testing needs a Grid org — this is why the
  module was deferred.
- **Params**: `oldest`/`latest` (unix seconds), `limit` (max 9999), `cursor`
  for pagination; optional server-side filters `action`, `actor`, `entity`.
- **Pagination**: `response_metadata.next_cursor` (empty string = done) — same
  windowed pattern as cloudflare_audit, with unix-seconds windows.
- **Entry shape**: `{id, date_create (unix), action, actor{type, user{id,
  email, name}}, entity{type, <type-specific object>}, context{location{type,
  id, name, domain}, ua, ip_address, session_id}, details{...varies}}`.
- **Mapping**: `id`→event_id (dedup); `date_create`→event_time (convert unix →
  RFC3339); `actor.user.email` (fallback `actor.user.id`)→actor;
  `context.ip_address`→src_ip; `action`→action; entity's inner object id/name
  →target; outcome `success` (entries record completed actions); attrs:
  `entity_type`, `location_type`, `location_name`, `location_domain`,
  `details_type` (from `details.type` when present — verify this carries the
  new role for `user_role_changed`).
- **Seed rules**: `user_role_changed` where details indicate admin/owner;
  `app_installed` / `app_scopes_expanded`; org/workspace preference changes
  (action LIKE `pref.%` — covers sharing-settings changes; verify exact pref
  action names live); bulk `file_downloaded` per actor (threshold, ≥10/30m);
  dead-man 6h (Grid orgs are chatty; small orgs may want 24h).
- **Verify live**: max `limit`; whether `details` is stable enough for the
  role-change rule; rate-limit tier of the audit endpoint; whether entries can
  arrive out of order (affects window lag — start with 300s).

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
