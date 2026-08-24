# CAIB — Cloud Audit In a Box

Build specification for Claude Code, **revision 4 — final paper revision; remaining open questions are answered empirically by Phase 1, not by further spec work**. Read this whole file before writing any code. Work phase by phase (see **Build Phases**); do not start a phase before the previous phase's acceptance criteria pass.

Revision 4 changes: an **Analyst Search** design (per-event `search_text` built by the loader, trgm-indexed on the hot tier by construction, BM25 optional behind a verification spike) replacing the vague "GIN on attrs" line; a **1M-event benchmark harness** in Phase 1 (`make bench`) so storage/throughput claims are measured, not asserted; the compression-ratio claim in the decisions table softened accordingly.

Revision 3 adds one thing: an optional **archive tier** (Phase 9) — closed Timescale chunks exported to Apache Iceberg on S3-compatible object storage, still queryable from Postgres. Nothing before Phase 9 changes because of it, except that enabling the archive transfers chunk-drop ownership from the Timescale retention policy to the archiver (see the Archive tier section).

Revision 2 changes, for orientation: TimescaleDB Community Edition is now a core dependency from Phase 1 (hypertables, compression, retention policies, continuous aggregates) — the hand-rolled partition layer and the `maintain` service are deleted. Notifications move out of Grafana alerting into the detect engine via a transactional outbox. Added: per-rule alert-rate cap, backfill mode, dead-man (absence) rules, `dedup_hash bytea` instead of text keys, HAProxy `/read-only` fix.

## What this is

A self-hostable cloud/SaaS audit-log SIEM on boring infrastructure: **Vector + PostgreSQL/TimescaleDB + Grafana**, packaged with Docker Compose. Each log source is a **module** (AWS CloudTrail, GCP Audit, Cloudflare, Slack, Zoom, KnowBe4) that ships everything needed end-to-end: collector config, field mapping, migrations, a provisioned Grafana dashboard, seed detection rules, a dead-man rule, and test fixtures.

Target user: a small security team (or fractional CISO client) that wants their SaaS audit logs searchable, dashboarded, and covered by baseline detections within an afternoon, without operating OpenSearch.

Working name is `caib` (rename is a find/replace; keep it lowercase in code).

## Non-goals (v1)

- No host agents, FIM, CIS benchmarking, or endpoint anything. Cloud/SaaS audit APIs only.
- No sub-minute detection latency. Batch detection on a 1–5 minute cadence is the design point. Vendor delivery lag (CloudTrail alone can be ~15 min) dominates anyway; do not chase latency by shrinking safety lags.
- No multi-tenancy, no RBAC beyond Grafana's own, no SOAR/response actions.
- No Kubernetes manifests. Docker Compose only (dev single-node + HA profile).
- No UEBA/ML. Detections are SQL.
- No vanilla-Postgres compatibility mode. TimescaleDB Community Edition is required (see licensing note). A stock-Postgres fallback is an open question, not a v1 target.

## Licensing note (read once, then move on)

TimescaleDB Community Edition ships under the TSL: free to self-host and run in production; the restriction is offering it as a managed database service for resale. CAIB deployments are self-hosted by whoever runs them, which is squarely permitted. The Apache-2-only build lacks compression, continuous aggregates, and retention policies and is **not sufficient** — CI must assert `SHOW timescaledb.license` returns `timescale`.

## Locked architecture decisions

Do not redesign these. If you hit a wall, note it in `DECISIONS.md` and ask rather than silently diverging.

| Decision | Rationale |
|---|---|
| **Two-stage ingest**: all producers write JSON envelopes to `ingest.landing`; a single **loader** process moves landing → `caib.events` with `ON CONFLICT DO NOTHING`. | Vector's `postgres` sink does plain inserts — assume no conflict handling. A unique-violation in a batch would poison-loop the sink. Landing has no constraints, so the ingest path cannot fail; the loader is the only writer to `events`, so dedup is race-free and exactly-once. |
| **`ingest.landing` is a plain heap table. Never UNLOGGED, never a hypertable.** | UNLOGGED tables don't replicate — a Patroni failover would eat the queue. It's a short-lived FIFO, not time-series data. |
| **`caib.events` is a TimescaleDB hypertable** (1-day chunks) with compression after 7 days, retention at 90 days, both as Timescale policies. | Chunks auto-create (no premake logic, no DEFAULT-partition failsafe), retention is one function call, columnar compression substantially shrinks cold audit data (the exact ratio is workload-dependent; the Phase 1 benchmark measures it continuously instead of this spec asserting a number) and makes cold-range aggregation columnar-fast. Policies are Timescale background jobs: they run on the primary and follow a Patroni failover, because they're catalog state, not node state. |
| **Dashboards read continuous aggregates**, not raw chunks, for anything wider than ~7 days. | Row-store aggregation over 90 days can't compete with pre-aggregation. Caggs are incremental and real-time (materialized history ∪ recent raw). |
| **Partition by `event_time`, drive detections by `ingested_at` watermark.** | Audit APIs deliver late and out of order. Event-time chunks keep queries/retention sane; ingest-time watermarks mean late events still pass through detection instead of slipping behind an already-evaluated window. |
| **Dedup key is `dedup_hash bytea` = sha256, always.** | Fixed 32-byte keys keep the PK index (the fattest object in the DB) small and uniform vs. variable-length ARNs/UUIDs-as-text. Human-readable IDs remain in `attrs`/`raw`. |
| **Pollers are Python, not Vector `http_client`.** | OAuth refresh, cursor state, pagination, and 429 handling don't fit VRL. Pollers are thin: fetch → normalize → insert landing + advance cursor **in one transaction**. |
| **Push sources (S3/SQS, Pub/Sub) go through Vector.** | Vector's `aws_s3`/`gcp_pubsub` sources + disk buffers are exactly the hard part you don't want to rewrite. |
| **No pg_cron, no maintain container.** Timescale's job scheduler owns compression/retention/cagg refresh; job health is monitored, not reimplemented. | One scheduler, failover-aware, already there. |
| **Detections are code (YAML + SQL) executed by `detect`. Notifications are sent by `detect` via a transactional outbox.** Grafana is read-only over the results. | Rules in the repo are testable and reviewable. The detect engine knows *at write time* whether an alert is new or a folded recurrence; Grafana alerting would have to re-derive that by polling and produces resolve-spam on sliding windows. |
| **One core image, entrypoints `migrate` / `loader` / `detect`**; one poller image parameterized by `CAIB_MODULE`. | Fewer images, one dependency set. |
| **Modules are Compose profiles.** | Enable/disable a source without editing YAML. |
| **Timestamps are UTC `timestamptz`. Severity and outcome are closed enums.** | Cross-module dashboards and rules depend on it. |
| **Cold data (optional) is an *archive tier*: Iceberg tables on S3-compatible storage, exported per-chunk, queried explicitly.** It is never called "snapshots", and no default dashboard touches it. | Iceberg gives immutable versioned snapshots, partition/file statistics for pruning, and schema evolution — the right substrate for a long-lived audit archive. But b-tree/BRIN indexes do not travel with it: structured investigations (time + module + actor) prune well; arbitrary substring hunts over terabytes of cold Parquet will not behave like Lucene searchable snapshots. Name and UX must set that expectation. |

## System architecture

```
 push sources                         poll sources
 ────────────                         ────────────
 CloudTrail → S3 → SQS ─┐             Slack Audit API ─┐
 GCP Audit → PubSub ────┤             Zoom API ────────┤
                        ▼             KnowBe4 API ─────┤
                  ┌──────────┐        CF Audit API ────┘
                  │  Vector  │               │
                  │ (sources │        ┌────────────┐
                  │  + VRL)  │        │  pollers   │ fetch → normalize
                  └────┬─────┘        │  (python)  │ → tx: landing+cursor
                       │ postgres sink└─────┬──────┘
                       ▼                    ▼
        ┌──────────────────────────────────────────────────┐
        │ PostgreSQL 16 + TimescaleDB (TSL)                │
        │   ingest.landing ── loader ──► caib.events       │
        │                                 (hypertable:     │
        │   Timescale jobs:                compress 7d,    │
        │    compression, retention,       retain 90d)     │
        │    cagg refresh                     │            │
        │                              caib.events_5m,     │
        │   detect.alerts ◄── upsert   caib.actors_1h      │
        │   detect.outbox ◄── same tx   (continuous aggs)  │
        │        │                                         │
        │  dev: single node · ha: Patroni x3 + HAProxy     │
        └────┬────────────────┬────────────────┬───────────┘
             │ sender loop    │ watermarked SQL│ SQL (RO)
             ▼                ▼                ▼
        Slack/webhook   ┌──────────┐    ┌──────────┐
                        │  detect  │    │ Grafana  │
                        │ (python) │    │ (provis- │
                        └──────────┘    │  ioned)  │
                          rules/*.yaml  └──────────┘
```

## Repository layout

```
caib/
├── README.md                  # quickstart, module enable matrix
├── DECISIONS.md               # append-only; deviations from this spec go here
├── Makefile
├── docker-compose.yml         # dev: single timescaledb node
├── compose.ha.yml             # overlay: Spilo x3 + etcd + haproxy
├── .env.example               # every env var, commented, no real secrets
├── db/
│   └── migrations/            # NNNN_name.sql, append-only, applied in order
├── vector/
│   ├── vector.yaml            # api, postgres sink → ingest.landing, disk buffer
│   └── modules/{aws_cloudtrail,gcp_audit}.yaml
├── core/                      # python pkg `caib_core`; image caib-core
│   ├── caib_core/{migrate,loader,detect,notify}.py
│   ├── caib_core/db.py        # connection, tx helpers
│   └── pyproject.toml
├── pollers/                   # python pkg `caib_pollers`; image caib-poller
│   ├── caib_pollers/framework.py
│   └── caib_pollers/modules/{slack_audit,zoom,knowbe4,cloudflare_audit}.py
├── detect/
│   └── rules/<module>/*.yaml
├── grafana/provisioning/{datasources,dashboards}/
├── modules/<module>/{README.md,MAPPING.md,API_NOTES.md,fixtures/*.json}
├── scripts/{demo_seed.py,ha_failover_test.sh,new_module.py}
└── tests/                     # pytest; needs docker for a throwaway timescaledb
```

Tooling: Python 3.12, `uv`, `ruff`, `pytest`, `psycopg` (v3, binary). Base images: `timescale/timescaledb:latest-pg16` for dev **(pin the concrete tag you pick in DECISIONS.md; it must be the TSL build)**, `timberio/vector:0.4X-debian`, `grafana/grafana:11.X`. HA uses Spilo (see HA section).

Postgres config (dev command args and Spilo env alike): `shared_preload_libraries=timescaledb`, `default_toast_compression=lz4`, `timezone=UTC`.

## Data model

Migrations in `db/migrations/`, applied in order by the `migrate` entrypoint, recorded in `caib.schema_migrations`; after applying, it syncs role passwords from env (`ALTER ROLE ... PASSWORD` — never in migration files). Roles: `caib_app` (owner; loader/detect/pollers/vector), `caib_ro` (Grafana; SELECT on `caib.*`, `detect.*`).

### 0001 — schemas, landing, cursors, dead letter

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE SCHEMA IF NOT EXISTS caib;
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS detect;
CREATE SCHEMA IF NOT EXISTS ops;

-- Append-only FIFO. Plain heap ON PURPOSE (never UNLOGGED: doesn't replicate;
-- never a hypertable: it's a queue). Exactly three columns; the Vector postgres
-- sink populates columns by name and absent keys become NULL — a malformed
-- remap must fail loudly on the NOT NULLs.
CREATE TABLE ingest.landing (
  received_at timestamptz NOT NULL,
  module      text        NOT NULL,
  payload     jsonb       NOT NULL
) WITH (autovacuum_vacuum_scale_factor = 0.01, autovacuum_vacuum_cost_delay = 0);
CREATE INDEX landing_received_at ON ingest.landing (received_at);

CREATE TABLE ingest.cursors (
  module     text PRIMARY KEY,
  cursor     jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Envelopes the loader refuses. Non-empty = mapping bug; on the Overview dashboard.
CREATE TABLE ingest.dead_letter (
  received_at timestamptz NOT NULL DEFAULT now(),
  module      text  NOT NULL,
  payload     jsonb NOT NULL,
  reason      text  NOT NULL
);

CREATE FUNCTION ops.try_inet(t text) RETURNS inet
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN RETURN t::inet; EXCEPTION WHEN OTHERS THEN RETURN NULL; END $$;

CREATE FUNCTION ops.try_tstz(t text) RETURNS timestamptz
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN RETURN t::timestamptz; EXCEPTION WHEN OTHERS THEN RETURN NULL; END $$;

-- Space-joined values of an attrs object, for search_text construction.
CREATE FUNCTION ops.attrs_text(a jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS
$$ SELECT string_agg(value, ' ') FROM jsonb_each_text(a) $$;
```

### 0002 — events hypertable + policies

```sql
CREATE TABLE caib.events (
  event_time  timestamptz NOT NULL,
  ingested_at timestamptz NOT NULL,
  module      text        NOT NULL,
  dedup_hash  bytea       NOT NULL,   -- sha256(module||':'||event_id-or-canonical)
  actor       text,
  src_ip      inet,
  action      text        NOT NULL,
  target      text,
  outcome     text        NOT NULL DEFAULT 'unknown'
              CHECK (outcome IN ('success','failure','unknown')),
  severity    text        NOT NULL DEFAULT 'info'
              CHECK (severity IN ('info','low','medium','high','critical')),
  attrs       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  raw         jsonb,
  search_text text,                       -- loader-built; see Analyst search
  PRIMARY KEY (event_time, dedup_hash)   -- partition column must be in the PK
) WITH (toast_tuple_target = 256);       -- push jsonb out of line: narrow hot rows

SELECT create_hypertable('caib.events', 'event_time',
                         chunk_time_interval => INTERVAL '1 day');

CREATE INDEX events_module_time ON caib.events (module, event_time DESC);
CREATE INDEX events_actor_time  ON caib.events (actor, event_time DESC);
CREATE INDEX events_ingested_brin ON caib.events USING brin (ingested_at);
-- No GIN on attrs unless a detection provably needs it; ad-hoc search goes
-- through search_text (see Analyst search). Its index arrives in Phase 8.

ALTER TABLE caib.events SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'module',
  timescaledb.compress_orderby   = 'event_time DESC'
);
SELECT add_compression_policy('caib.events', compress_after => INTERVAL '7 days');
SELECT add_retention_policy  ('caib.events', drop_after     => INTERVAL '90 days');
```

Notes you must respect: b-tree indexes cover uncompressed chunks only — queries into compressed history lean on `segmentby` + Timescale's sparse min/max (and bloom, where available) metadata, which is fine for time-bounded module/actor filtering and columnar aggregation, and deliberately not optimized for arbitrary substring hunts. `ON CONFLICT` against a compressed chunk works but pays decompression; with `compress_after = 7 days`, duplicate late arrivals essentially never land there. Retention/compression windows are configurable post-install via `remove_*_policy`/`add_*_policy` — document in README, don't parameterize migrations.

### 0003 — detection state, alerts, outbox

```sql
CREATE TABLE detect.rule_state (
  rule_id   text PRIMARY KEY,
  watermark timestamptz NOT NULL
);

CREATE TABLE detect.alerts (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_id     text NOT NULL,
  dedup_key   text NOT NULL,
  severity    text NOT NULL,
  title       text NOT NULL,
  entity      jsonb NOT NULL DEFAULT '{}'::jsonb,
  context     jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_seen  timestamptz NOT NULL,
  last_seen   timestamptz NOT NULL,
  event_count int  NOT NULL DEFAULT 1,
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open','ack','closed')),
  UNIQUE (rule_id, dedup_key)
);
CREATE INDEX alerts_status_time ON detect.alerts (status, last_seen DESC);

-- Transactional outbox: written in the same tx as the alert upsert,
-- drained by the notify sender. Crash-safe by construction.
CREATE TABLE detect.outbox (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  alert_id   bigint NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('new','recurrence','meta')),
  payload    jsonb NOT NULL,
  sent_at    timestamptz
);
CREATE INDEX outbox_unsent ON detect.outbox (id) WHERE sent_at IS NULL;
```

Alert upsert — `RETURNING (xmax = 0) AS is_new` distinguishes insert from fold:

```sql
INSERT INTO detect.alerts (rule_id, dedup_key, severity, title, entity, context,
                           first_seen, last_seen, event_count)
VALUES (...)
ON CONFLICT (rule_id, dedup_key) DO UPDATE SET
  last_seen   = GREATEST(detect.alerts.last_seen, EXCLUDED.last_seen),
  event_count = detect.alerts.event_count + EXCLUDED.event_count,
  context     = EXCLUDED.context,
  status      = CASE WHEN detect.alerts.status = 'closed' THEN 'open'
                     ELSE detect.alerts.status END
RETURNING id, (xmax = 0) AS is_new;
```

### 0004 — continuous aggregates

```sql
CREATE MATERIALIZED VIEW caib.events_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket('5 minutes', event_time) AS bucket,
       module, action, outcome, count(*) AS n
FROM caib.events GROUP BY 1, 2, 3, 4
WITH NO DATA;

CREATE MATERIALIZED VIEW caib.actors_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', event_time) AS bucket,
       module, actor, count(*) AS n,
       count(*) FILTER (WHERE outcome = 'failure') AS failures
FROM caib.events GROUP BY 1, 2, 3
WITH NO DATA;

SELECT add_continuous_aggregate_policy('caib.events_5m',
  start_offset => INTERVAL '1 day', end_offset => INTERVAL '5 minutes',
  schedule_interval => INTERVAL '5 minutes');
SELECT add_continuous_aggregate_policy('caib.actors_1h',
  start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour');
-- Retention on the caggs themselves: 400 days (cheap; outlives raw).
```

Caggs are real-time by default: querying them unions materialized history with recent raw, so panels don't need to care about refresh lag.

## The envelope contract

Every producer — VRL remaps and Python pollers alike — writes the same JSON shape into `ingest.landing.payload`. The loader is the only code that maps envelope → columns; this contract is the seam that keeps two normalization stacks honest.

```jsonc
{
  "event_time": "2026-08-23T10:41:07Z",   // required, RFC3339, UTC
  "event_id":   "abc-123",                // vendor's stable id, or null
  "actor":      "matija@example.com",     // human/service identity, or null
  "src_ip":     "203.0.113.7",            // string or null; loader uses ops.try_inet
  "action":     "ConsoleLogin",           // required; vendor's verb, unmodified
  "target":     "arn:aws:iam::...:role/x",// object acted on, or null
  "outcome":    "success",                // success | failure | unknown
  "severity":   "info",                   // info|low|medium|high|critical, per-module map
  "attrs":      { "user_type": "Root" },  // flat, snake_case, documented in MAPPING.md
  "raw":        { ... }                   // original vendor event, verbatim
}
```

Rules: `dedup_hash` is derived by the loader, never sent. `attrs` keys must be listed in the module's `MAPPING.md` with vendor source paths — no ad-hoc keys in rules. Severity maps conservatively (default `info`). `raw` is kept in v1; `CAIB_DROP_RAW=true` on the loader nulls it (with compression this is a minor lever, not the main one).

## Ingest: push path (Vector)

`vector/vector.yaml`:

```yaml
api: { enabled: true, address: 0.0.0.0:8686 }
sinks:
  pg:
    type: postgres
    inputs: ["remap_*"]
    endpoint: "${CAIB_PG_URL}"        # dev: timescaledb:5432, ha: haproxy:5000
    table: "ingest.landing"
    batch: { max_events: 500, timeout_secs: 5 }
    buffer: { type: disk, max_size: 1073741824, when_full: block }
```

Module remaps must end with exactly `. = { "received_at": now(), "module": "<module>", "payload": <envelope> }` (or an array of such objects — an array at the remap root fans out to one event per element).

**aws_cloudtrail**: `aws_s3` source in SQS mode (`compression: auto`; CloudTrail objects are `.json.gz` holding `{"Records":[...]}`). Remap: parse, map over `Records`, emit the envelope array. Mapping: `eventID`→event_id, `eventTime`→event_time, `userIdentity.arn`|`.type`→actor, `eventName`→action, `sourceIPAddress`→src_ip, `errorCode` present→failure; attrs: `user_type`, `event_source`, `aws_region`, `error_code`, `mfa`. Verify in Phase 3 (record in `API_NOTES.md`): framing so one S3 object = one event, and array fan-out on the pinned Vector version — `vector test` with a gzipped fixture covers both. Cloud-side setup (trail→S3, S3 notifications→SQS, IAM) as copy-pasteable CLI/Terraform in the module README.

**gcp_audit**: `gcp_pubsub` source, `decoding.codec: json`. `insertId`→event_id, `timestamp`→event_time, `protoPayload.authenticationInfo.principalEmail`→actor, `protoPayload.methodName`→action, `protoPayload.resourceName`→target, `protoPayload.requestMetadata.callerIp`→src_ip; severity ERROR→low else info; attrs: `service_name`, `log_id`, `permission_denied`. Cloud-side: Log Router sink filtered to `cloudaudit.googleapis.com` → Pub/Sub.

## Ingest: loader

One loop, one transaction per iteration (batch 5000, interval 5s, env-tunable):

```sql
WITH batch AS (
  SELECT ctid FROM ingest.landing
  ORDER BY received_at
  LIMIT %(batch)s
  FOR UPDATE SKIP LOCKED
), moved AS (
  DELETE FROM ingest.landing l USING batch b
  WHERE l.ctid = b.ctid
  RETURNING l.module, l.payload
), parsed AS (
  SELECT module, payload, ops.try_tstz(payload->>'event_time') AS et FROM moved
), ins AS (
  INSERT INTO caib.events (event_time, ingested_at, module, dedup_hash, actor, src_ip,
                           action, target, outcome, severity, attrs, raw, search_text)
  SELECT et, now(), module,
         sha256(convert_to(module || ':' ||
           coalesce(payload->>'event_id',
                    coalesce(payload->>'event_time','') || '|' ||
                    coalesce(payload->>'actor','')      || '|' ||
                    coalesce(payload->>'action','')     || '|' ||
                    coalesce(payload->>'target','')), 'UTF8')),
         payload->>'actor',
         ops.try_inet(payload->>'src_ip'),
         coalesce(payload->>'action', 'unknown'),
         payload->>'target',
         coalesce(payload->>'outcome', 'unknown'),
         coalesce(payload->>'severity', 'info'),
         coalesce(payload->'attrs', '{}'::jsonb),
         payload->'raw',
         lower(concat_ws(' ',
           payload->>'actor', payload->>'action', payload->>'target',
           payload->>'src_ip', payload->>'outcome',
           ops.attrs_text(coalesce(payload->'attrs', '{}'::jsonb))))
  FROM parsed WHERE et IS NOT NULL
  ON CONFLICT (event_time, dedup_hash) DO NOTHING
)
INSERT INTO ingest.dead_letter (module, payload, reason)
SELECT module, payload, 'unparseable event_time' FROM parsed WHERE et IS NULL;
```

`SKIP LOCKED` makes a second loader replica safe later; v1 runs one.

## Ingest: poller framework

`caib_pollers/framework.py` defines the contract; modules never touch the DB or retry logic.

```python
class Poller(Protocol):
    module: str
    interval_s: int                      # default 300
    def fetch(self, cursor: dict | None, ctx: Ctx) -> FetchResult: ...

@dataclass
class FetchResult:
    envelopes: list[dict]     # envelope-contract dicts
    next_cursor: dict         # opaque, persisted verbatim
    has_more: bool            # True → loop immediately (backfill)
```

Framework: cursor from `ingest.cursors` (first run starts at `CAIB_<MODULE>_BACKFILL`, default `now()-24h`); HTTP client with timeouts, exponential backoff + jitter, `Retry-After` honored on 429, crash after 6 attempts (compose restarts; cursor unchanged; at-least-once is safe because dedup); per batch **one transaction**: landing insert + cursor upsert; envelope validation before insert (invalid → dead_letter, not crash); `--once` for tests and demo; one structured log line per cycle; OAuth2 client-credentials helper with early refresh (Zoom).

### Poll modules

Per module: the class, `MAPPING.md` (field table, attrs list, severity map, dedup basis), `API_NOTES.md` (verified endpoints/auth/pagination/rate limits), ≥2 sanitized fixtures, fixture tests. **The table below is best-known starting points — verify against current vendor docs during that module's phase and record findings. Do not trust it blindly.**

| Module | Auth | Endpoint (verify) | Cursoring (verify) | Dedup basis |
|---|---|---|---|---|
| `slack_audit` | Enterprise Grid org token, `auditlogs:read` | `GET https://api.slack.com/audit/v1/logs` | `oldest` + `cursor` | `entries[].id` |
| `cloudflare_audit` | API token, Account→Audit Logs Read | `GET /client/v4/accounts/{id}/audit_logs` | `since`/`before` + page (v2 cursor — check) | `result[].id` |
| `zoom` | Server-to-Server OAuth (`account_credentials`) | `/v2/report/operationlogs`, `/v2/report/activities` | date window (max ~1 month) + `next_page_token` | no stable id → canonical hash |
| `knowbe4` | Bearer (region host us/eu) | KnowBe4 User Events API | occurred-date paging (confirm params) | event `id` |

Zoom runs both endpoints as two streams under one module; `attrs.stream` distinguishes them and the cursor dict holds one sub-cursor per stream.

## Detection engine

`detect` loads `detect/rules/**/*.yaml` at start (and SIGHUP), validates each (YAML schema + `EXPLAIN` against the DB; a broken rule blocks startup), then loops. Per rule, per tick:

1. `wm_from = rule_state.watermark` (first run: `now() - 1h`).
2. `wm_to = now() - safety_lag` (`CAIB_DETECT_SAFETY_LAG=60s`; must exceed any loader transaction's age so no `ingested_at` can land inside an already-evaluated window). Skip if `wm_to <= wm_from`.
3. One transaction: execute rule SQL → **rate-cap check** → upsert alerts (capturing `is_new`) → enqueue `detect.outbox` rows → `rule_state.watermark = wm_to`. A crash re-runs the window; upsert + outbox-in-same-tx make that idempotent and lose no notifications.

**Rate cap**: the engine wraps every rule as `WITH r AS (<sql>) SELECT * FROM r LIMIT %(cap)s + 1` (`CAIB_DETECT_MAX_ALERTS_PER_TICK=500`). Over cap → skip individual upserts, write **one** meta-alert (`dedup_key='overflow'`, kind `meta`, context = row count + window) and still advance the watermark. A bad rule floods one Slack message, not five thousand.

**Notifier** (`notify` loop inside the detect process): drain `detect.outbox` where `sent_at IS NULL` order by id, `FOR UPDATE SKIP LOCKED`, batch 50 → POST to `CAIB_NOTIFY_WEBHOOK_URL` (blank = drain-and-mark, disabled) → set `sent_at`. Failed POSTs stay unsent and retry with backoff. `CAIB_NOTIFY_MIN_SEVERITY` filters at enqueue time. Default: notify on `new` and `meta` only; per-rule `notify_recurrence: true` opts folds in. Payload is a flat JSON of the alert; Slack Block Kit formatting is Phase 8.

**Backfill mode**: `caib-core detect --advance-watermarks` sets every rule's watermark to `now()` without evaluating. Mandatory step after any historical import — otherwise the entire backfill lands in one watermark window and every rule fires on day one. `make demo` and the backfill docs both use it.

Rule file:

```yaml
id: aws_cloudtrail.root_console_login    # <module>.<name>, unique
enabled: true
module: aws_cloudtrail
title: "Root account console login"
severity: high
interval: 60
lookback: 15m                            # exposed as %(lookback)s
notify_recurrence: false
sql: |
  SELECT
    actor || '|' || coalesce(host(src_ip), '-')  AS dedup_key,
    jsonb_build_object('actor', actor, 'src_ip', host(src_ip)) AS entity,
    jsonb_build_object('events', count(*))       AS context,
    max(event_time)                              AS last_event_time
  FROM caib.events
  WHERE module = 'aws_cloudtrail'
    AND ingested_at > %(wm_from)s AND ingested_at <= %(wm_to)s
    AND action = 'ConsoleLogin'
    AND attrs->>'user_type' = 'Root'
  GROUP BY actor, src_ip
```

Contract: rule SQL returns exactly `dedup_key, entity, context, last_event_time`. Three sanctioned shapes, documented in `detect/rules/README.md`:

- **Existence** — filter the watermark slice (above).
- **Threshold/correlation** — watermark slice as trigger, aggregate over `event_time > %(wm_to)s::timestamptz - %(lookback)s::interval`, `HAVING count(*) >= N`. Recurrence folds via the upsert; put a `date_trunc` bucket in `dedup_key` only when a fresh alert per window is genuinely wanted.
- **Absence (dead-man)** — ignores watermark params; fires when a module has zero events in a trailing window: `SELECT '<module>' AS dedup_key, ... WHERE NOT EXISTS (SELECT 1 FROM caib.events WHERE module='<module>' AND ingested_at > now() - interval '2 hours')`. **Every module ships one** — this is the alarm for expired tokens and silently crash-looping pollers, the number-one SaaS-poller failure mode. Window per module (CloudTrail 2h; low-traffic sources like KnowBe4 24h). Stays open until a human closes it.

### Seed rules (3–5 per module, plus the dead-man rule)

- **aws_cloudtrail**: root console login; console login without MFA; `StopLogging`/`DeleteTrail`/`PutEventSelectors`; `AttachUserPolicy` with `AdministratorAccess`; SG ingress from `0.0.0.0/0`; ≥5 login failures then success from one IP (threshold).
- **gcp_audit**: `SetIamPolicy` granting owner/editor; logging sink deleted/modified; service-account key created; `permission_denied` burst per actor (threshold).
- **slack_audit**: `user_role_changed` to admin/owner; `app_installed`/scopes expanded; public sharing settings changed; bulk `file_downloaded` per actor (threshold).
- **cloudflare_audit**: DNS change on apex/wildcard; WAF/firewall rule deleted; member added with admin role; API token created.
- **zoom**: security-category setting changes; admin role grants. (New-geo-per-actor needs state — out of scope, do not attempt.)
- **knowbe4**: repeat phishing-test failure (threshold); training past-due burst. These two only.

Where a rule maps to a public Sigma cloud rule, note the Sigma ID in a comment. Automated Sigma→SQL is Phase 8.

## Analyst search

The "find `foo-prod` in everything from the last few days" workflow, architected now, indexed in Phase 8.

- **`search_text`** is built by the loader for every event: lowercased concat of actor, action, target, src_ip, outcome, and all `attrs` values. Because `attrs` is already the module-curated field set (every key documented in MAPPING.md), the per-module curation comes for free — no module code, no drift between the two normalization stacks. `raw` stays evidence and is never search-indexed.
- **Hot-tier-only indexing falls out of the architecture**: on a hypertable, b-tree/GIN/trgm indexes physically exist only on uncompressed chunks, so a search index covers exactly the 0–7d rowstore window with zero extra configuration. Investigations beyond that are time-bounded columnstore scans of `search_text` (acceptable) or the archive tier (explicit).
- **Default index (Phase 8)**: `CREATE INDEX ... USING gin (search_text gin_trgm_ops)` — substring/`ILIKE` recall is the SIEM search workload; ranking is secondary.
- **Optional BM25** (`CAIB_SEARCH_BM25=true`): pg_textsearch on `search_text` for ranked multi-term search. Gate behind a verification spike recorded in DECISIONS.md: custom access methods on hypertable chunks, `shared_preload_libraries` on dev and Spilo images, write overhead at ingest rates. If any of it fails, trgm is the answer and BM25 stays off.
- **Search dashboard** (Phase 8, with the index): text variable `$q` → `WHERE search_text ILIKE '%' || $q || '%' AND $__timeFilter(event_time)`, results table with module/actor/action/target and a raw-JSON drilldown.

## Grafana

All provisioning, zero clickops. Datasource `caib` → `caib_ro` (`timescaledb: true` in jsonData), URL from env (dev `timescaledb:5432`, HA `haproxy:5001`).

- **Panel data policy**: ranges ≤ 7 days may hit `caib.events`; anything wider reads `caib.events_5m` / `caib.actors_1h`. Every panel uses `$__timeFilter(...)`.
- **Overview**: events/hour by module (events_5m); top actors 24h (actors_1h); failure rate by module; open alerts by severity; landing backlog `count(*) from ingest.landing` (ingest-health stat); dead_letter count; Timescale job health (`timescaledb_information.job_errors` last 24h — compression/retention/cagg jobs are infrastructure too).
- **Per-module dashboard** (one pattern, instantiated per module): events/hour, top actors, top actions, failure table, plus module-specific panels listed in each module README.
- **Alerts**: `detect.alerts` table filtered by `$status`/`$severity`, newest `last_seen` first, `entity`/`context` as JSON.

No Grafana alert rules — notifications come from the detect outbox. Dashboards are JSON in the repo; hand-write the first, then template the per-module ones if that's less error-prone.

## Compose

**dev**: `timescaledb` (single, volume-backed) → `migrate` (one-shot) → `vector`, `loader`, `detect`, `grafana`; pollers and vector module files behind profiles named exactly after modules. Healthchecks everywhere; `restart: unless-stopped`.

**ha (`compose.ha.yml`)**: `etcd` ×1 (lab; README: 3 for anything real), `spilo1..3` (Zalando Spilo image — bundles Postgres + Patroni + TimescaleDB; **verify at build time** that the pinned Spilo tag ships the TSL build and that `timescaledb` lands in `shared_preload_libraries`, record in DECISIONS.md; if it doesn't fit, fall back to a custom image `FROM timescale/timescaledb:...-pg16` + `pip install patroni[etcd3]`), `haproxy`:

```
defaults
  mode tcp
  timeout connect 5s
  timeout client 30m
  timeout server 30m
listen primary
  bind *:5000
  option httpchk GET /primary
  http-check expect status 200
  default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
  server pg1 spilo1:5432 check port 8008
  server pg2 spilo2:5432 check port 8008
  server pg3 spilo3:5432 check port 8008
listen readonly
  bind *:5001
  option httpchk GET /read-only        # matches replicas AND the primary:
  http-check expect status 200         # Grafana stays up when replicas are down
  default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
  server pg1 spilo1:5432 check port 8008
  server pg2 spilo2:5432 check port 8008
  server pg3 spilo3:5432 check port 8008
listen stats
  bind *:7000
  mode http
  stats enable
  stats uri /
```

`on-marked-down shutdown-sessions` is what makes failover real — it kills pooled connections to the demoted primary. Writers → 5000; Grafana → 5001. Timescale background jobs need no HA handling: they follow the primary. `scripts/ha_failover_test.sh`: continuous synthetic landing inserts → `docker stop` the current primary (via Patroni REST) → assert loader resumes ≤60s, zero end-to-end loss (dedup makes the count exact), Grafana still answers, and compression/retention jobs are listed on the new primary.

## Archive tier (optional, Phase 9)

Off by default. When enabled, the storage lifecycle becomes three tiers:

```
0–7d          Timescale rowstore        full b-tree indexes, detections, hot dashboards
7d–ARCHIVE    Timescale columnstore     compressed chunks, cagg-backed dashboards
ARCHIVE+      Iceberg on S3/MinIO       explicit investigation/compliance queries
```

Config: `CAIB_ARCHIVE_ENABLED` (default false) · `CAIB_ARCHIVE_AFTER_DAYS` (default 90) · `CAIB_ARCHIVE_URL` (`s3://caib-archive/events`) · S3 endpoint/region/creds for MinIO or any S3-compatible store. Format is Iceberg, not loose Parquet — the catalog, immutable snapshots, and file statistics are the point; do not add a format option.

**Retention handoff (critical):** enabling the archive **removes** `add_retention_policy` on `caib.events`. The archiver becomes the only thing that drops chunks, and only after a verified export. Two chunk-droppers racing means silent data loss; the migration that flips this must be explicit and tested.

**Implementation candidate A (preferred): pg_lake** — the open-source (Apache-2) extension suite from the Crunchy Data lineage: Postgres itself acts as the Iceberg catalog, DuckDB runs underneath as the execution engine via a companion `pgduck_server` process, S3-compatible endpoints are supported, and `CREATE TABLE ... USING iceberg` gives transactional Iceberg tables queryable in ordinary SQL. That makes the archive table a first-class relation:

```sql
CREATE SCHEMA archive;
CREATE TABLE archive.events (LIKE caib.events)      -- verify: jsonb mapping; fall back to text
  USING iceberg;                                     -- partitioned by day(event_time); verify syntax

CREATE VIEW caib.events_all AS
  SELECT * FROM caib.events
  UNION ALL
  SELECT * FROM archive.events;
```

Verify at Phase 9 start and record in DECISIONS.md: pgduck_server topology in Compose (one sidecar; how the extension reaches it), behavior on the Spilo/Patroni nodes (extension `.so` must exist on all three; if it doesn't fit the HA image, the archive tier is documented as requiring the custom-image fallback), jsonb→Parquet mapping, and partition-spec syntax.

**Implementation candidate B (fallback): export-only archiver** — Python, `pyarrow` + `pyiceberg`, writing Parquet + committing Iceberg snapshots to MinIO with the catalog in Postgres. No in-database union view; the archive is compliance/restore storage with a documented re-import path. Ship B only if A fails verification — the union view is most of the user value.

**Archiver** (new core entrypoint `archive`, container behind the `archive` Compose profile, alongside `minio` and `pgduck` for the lab): loop over fully-compressed chunks older than `ARCHIVE_AFTER` not yet in the manifest:

```
tx1: INSERT INTO archive.events SELECT * FROM <chunk>;
     INSERT INTO archive.chunks (chunk_name, range_start, range_end,
                                 row_count, exported_at, verified=false);
verify: count + min/max(event_time) in archive slice == chunk
tx2: UPDATE archive.chunks SET verified = true;  SELECT drop_chunk(...);
```

Idempotent by `chunk_name`; a crash mid-export re-runs and overwrites the unverified attempt; S3 unavailability just means chunks stay local longer. The manifest lives in `archive.chunks` and gets an Overview panel (chunks archived, bytes, lag behind `ARCHIVE_AFTER`, unverified count — unverified > 0 for more than a day is an absence-rule candidate).

**Grafana:** hot dashboards keep reading `caib.events`/caggs and never touch the archive. One additional provisioned dashboard, **Investigations (all tiers)**, queries `caib.events_all` with an explicit warning row that cold ranges scan object storage. Tier choice is always explicit — Hot, Archive, or All — never inferred from the time picker.

**Rules:** detections never read the archive. The watermark model already guarantees archived data was evaluated when it was hot.

## Makefile

`up` / `down` / `logs` · `ha-up` · `migrate` · `test` (ruff + pytest) · `demo` · `bench` · `ha-failover-test` · `new-module NAME=x`.

### Benchmark (`make bench`)

Generates/replays **1M representative events** (module mix from fixtures, timestamps spread over 30 days) through landing → loader, then records to `bench/results/<version>-<date>.json` (committed):
raw fixture bytes · heap size · TOAST size · each index size (incl. `search_text`'s share) · compressed-chunk size after forcing compression · compression ratio · ingest events/sec · dedup re-insert throughput against a **compressed** chunk · representative query latency before/after compression (module+time filter, actor top-N, `search_text ILIKE`).
No pass/fail thresholds — this is measurement, not a gate. The output is also the honest sales artifact: "1M CloudTrail-shaped events: X GB JSON → Y GB hot → Z MB compressed" beats any generic database claim.

**`make demo` is a first-class deliverable** (the consulting-pitch artifact): seed every enabled module's fixtures into landing with timestamps rewritten into the last 24h → loader `--once` → `refresh_continuous_aggregate` on both caggs → `detect --advance-watermarks` is **not** used here (demo *wants* alerts): instead seed, load, run `detect --once` with watermarks defaulted an hour back → print the Grafana URL. Cold clone to populated dashboards + firing sample alerts in under two minutes, no cloud credentials.

## Build phases

In order; a phase is done when its acceptance criteria pass and `make test` is green.

- **Phase 0 — scaffold.** Layout, `.env.example`, dev compose (timescaledb+grafana+migrate stub), CI (ruff+pytest), Makefile. *AC: `make up` → Grafana login; CI asserts `SHOW timescaledb.license` = `timescale`.*
- **Phase 1 — core data path.** Migrations 0001–0004, loader, benchmark harness. *AC: envelope fixtures via psql land correctly in `caib.events`; re-insert changes nothing; dedup against a **compressed** chunk works (insert old rows, `compress_chunk`, re-insert dups); bad `event_time` → dead_letter; retention policy drops an aged chunk in test (`run_job`); caggs answer after refresh; `make bench` completes on 1M events and writes a results file — measurement only, no thresholds.*
- **Phase 2 — poller framework + `cloudflare_audit`.** *AC: fixture tests prove mapping+dedup; `--once` against the real API works (manual verification noted in API_NOTES.md); kill mid-batch → no cursor loss, no duplicates.*
- **Phase 3 — push path (`aws_cloudtrail`).** *AC: gzipped fixture through `vector test` and through localstack-or-replay lands N events for N Records; malformed remap fails loudly on landing NOT NULLs.*
- **Phase 4 — detect.** Engine, watermarks, upsert+outbox, rate cap, backfill mode, notifier, CloudTrail+Cloudflare seed rules incl. dead-man and one threshold rule. *AC: fixture-driven rule tests; crash between upsert and watermark does not double-alert or double-notify (test it); cap produces exactly one meta-alert; absence rule fires on silence and folds on repeat; webhook mock receives `new` only by default.*
- **Phase 5 — Grafana + demo.** Datasource, Overview, two module dashboards, Alerts dashboard, `make demo`. *AC: fresh clone → `make up && make demo` → populated dashboards (incl. cagg-backed 30-day view) and ≥1 sample alert, zero clicks.*
- **Phase 6 — remaining modules.** `gcp_audit`, `slack_audit`, `zoom`, `knowbe4`, each to the module definition-of-done.
- **Phase 7 — HA.** Spilo overlay, HAProxy, failover script. *AC: `make ha-up` clean; `make ha-failover-test` passes incl. jobs-follow-primary check; README covers promoting the overlay to real hosts.*
- **Phase 8 — hardening (optional, by value).** Sigma→SQL for the existence shape · Slack Block Kit notifications · **Analyst Search per its section** (trgm index + Search dashboard; BM25 flag only after its verification spike) · `CAIB_DROP_RAW` · per-module ingest-lag panel (`max(now()-event_time)`) · pgBackRest sidecar docs.
- **Phase 9 — archive tier (optional).** pg_lake verification spike first (record findings, choose candidate A or B), then archiver + manifest + Investigations dashboard. *AC: export a compressed chunk to MinIO; `events_all` returns identical counts for the chunk's range before and after `drop_chunk`; re-running the archiver is a no-op; kill mid-export → chunk not dropped, next run recovers; retention policy is verifiably absent while archive is enabled; hot dashboards provably issue no queries against `archive.*`.*

**Module definition-of-done**: collector config or poller class · `MAPPING.md` + `API_NOTES.md` · ≥2 sanitized fixtures · fixture tests green · dashboard provisioned · ≥3 seed rules **plus the dead-man rule**, with tests · README covering vendor-side setup and env vars.

## Working agreements for Claude Code

- Vendor API specifics here are starting points. **Verify against current vendor docs before implementing a module**; write confirmed details into `API_NOTES.md`. Where reality contradicts this spec, follow reality and log it in `DECISIONS.md`.
- Migrations are append-only. Never edit an applied one.
- No secrets in the repo; env only; `.env.example` documents every var.
- Every module change ships fixtures and tests in the same commit. Fixtures are sanitized real shapes; if genuinely unobtainable, mark `SYNTHETIC` and say so in API_NOTES.md.
- The loader stays the only writer to `caib.events`; the envelope contract stays the only producer interface; landing stays a plain logged heap table. If a feature seems to need otherwise, stop and ask.
- Timescale owns scheduled background work. Do not add cron containers, pg_cron, or hand-rolled partition code. The one sanctioned exception is the Phase 9 archiver, which does something Timescale cannot (export to object storage) and, when enabled, replaces the retention policy as the sole chunk-dropper.
- Prefer boring: psycopg + SQL + small loops. No ORMs, no async frameworks, no brokers.
- When a phase AC is ambiguous, write the test you think expresses it, then ask.

## Open questions (answer before Phase 2)

1. Final name — `caib` assumed.
2. Defaults: compress 7d, retain 90d events / 365d alerts+caggs — confirm.
3. First poll module: Cloudflare (simplest auth) assumed over Slack (needs Enterprise Grid to test live).
4. TSL is fine for self-hosted client deployments, but some enterprises blanket-ban non-OSI dependencies — is a degraded vanilla-Postgres mode ever worth building, or is TimescaleDB simply a stated requirement? (Spec assumes: stated requirement.)
5. License for CAIB itself.
6. Archive tier: is 90d-local + Iceberg-beyond the right default shape for your client base, or should some deployments simply raise local retention and skip Phase 9? (Spec assumes: archive is opt-in and most deployments skip it.)
