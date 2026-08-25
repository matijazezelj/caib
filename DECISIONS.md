# DECISIONS.md

Append-only log of deviations from `caib-build-spec.md` and empirical findings the spec asked us to record.

## 2026-08-24 — PostgreSQL 18 instead of 16 (user-directed)

Spec pins PostgreSQL 16. User directed "use pg17 or 18 if it's better; 19 is already out."
Findings (Docker Hub, 2026-08-24):

- TimescaleDB TSL builds exist for pg16/pg17/pg18, all at 2.29.2. **No pg19 tags exist** — TimescaleDB does not support PG19 yet, so PG19 is not usable regardless of Postgres itself.
- Zalando Spilo `master` builds with `PGVERSION=18` (old versions 14–17 also included), so the Phase 7 HA path is not blocked by PG18.

**Decision: PostgreSQL 18, image `timescale/timescaledb:2.29.2-pg18` (TSL build — the `-oss` suffixed tags are the insufficient Apache-only build).**

## 2026-08-24 — image pins

- `timescale/timescaledb:2.29.2-pg18` (dev + tests + CI).
- `timberio/vector:0.57.0-debian` — spec said `0.4X`; 0.57.0 is current. The `postgres` sink exists since 0.41, so any 0.4X+ works; pin the newest.
- `grafana/grafana:12.4.9` — spec said `11.X`, written before Grafana 12. 12.4.9 is the current stable release.

## 2026-08-24 — Spilo ships Apache-only TimescaleDB by default (Phase 7 heads-up)

`zalando/spilo` master builds with `ARG TIMESCALEDB_APACHE_ONLY=true`, i.e. the published
images contain the Apache-2 TimescaleDB build **without** compression/retention/caggs —
exactly what the spec's licensing note rules out. Phase 7 must either build Spilo from
source with `TIMESCALEDB_APACHE_ONLY=false` or use the spec's custom-image fallback
(`FROM timescale/timescaledb:...-pg18` + `pip install patroni[etcd3]`). Decide at Phase 7;
verify `SHOW timescaledb.license` on the HA nodes either way.

## 2026-08-24 — migration 0004 runs outside a transaction

`CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous)` refuses to run in a
transaction block. The migration runner supports a `-- caib:no-transaction` first-line
marker: such files execute statement-by-statement in autocommit and must be fully
idempotent (IF NOT EXISTS / if_not_exists => true), because a crash mid-file re-runs the
whole file. 0004 uses it; all other migrations keep the one-transaction-per-file default.

Cagg retention: spec's migration comment says 400 days, open question 2 says 365. Went
with the concrete number in the SQL comment (400d). **Closed 2026-08-25: 400 days stands**
— it's what the applied migration says, the difference is 35 days of pre-aggregated
counts (kilobytes), and changing it later is one `remove_retention_policy`/
`add_retention_policy` call, not a migration edit.

## 2026-08-24 — loader dead-letters contract violations beyond bad event_time

Spec's loader CTE dead-letters only unparseable `event_time`. But an envelope with an
out-of-enum `outcome`/`severity` (CHECK violation) or a non-object `attrs`
(`jsonb_each_text` error) would abort the whole batch transaction and poison-loop the
loader — exactly the failure mode the two-stage design exists to prevent. The loader
therefore validates the closed enums and `attrs` shape and routes violations to
`ingest.dead_letter` with reasons `invalid outcome` / `invalid severity` /
`attrs not an object`. Semantics otherwise identical to the spec CTE; the final SELECT
returns (moved, inserted, dead) counts for the per-cycle log line.

## 2026-08-24 — one Vector container per push module, not one shared Vector

The spec's architecture diagram shows a single Vector; the compose requirement
("vector module files behind profiles named exactly after modules") has no clean
single-container mechanism — compose can't conditionally add `--config` args or mounts
per active profile. Each push module therefore gets its own Vector service
(`vector-aws-cloudtrail`, later `vector-gcp-audit`) sharing `vector/vector.yaml` plus its
module file, each with its own disk-buffer volume. Same locked decision ("push sources go
through Vector"), better isolation, profile-pure.

## 2026-08-24 — Phase 3 empirical findings (Vector 0.57.0)

- S3-object framing and remap array fan-out verified: 1 gzipped CloudTrail object with 3
  Records → exactly 3 landing rows (LocalStack S3+SQS end-to-end test).
- VRL: `if` expressions are not valid directly as object-literal values; assign to a
  variable first.
- `localstack/localstack:latest` (2026.x) now requires a license token and exits without
  one; tests pin `localstack/localstack:4.14`, the last community series.

## 2026-08-24 — Phase 6 rescoped (user-directed): major clouds first, SaaS modules deferred

Spec Phase 6 called for gcp_audit + slack_audit + zoom + knowbe4. User redirected:
build **gcp_audit** and a new **azure_activity** module (not in the spec — Azure
Activity Log via ARM REST poller with the framework's new OAuth2 client-credentials
helper) so the three major clouds + Cloudflare are covered, all on SYNTHETIC fixtures
with live verification pending. slack_audit / zoom / knowbe4 are deferred to detailed
build recipes in docs/NEW_MODULE_HOWTO.md (they also need vendor accounts we don't
have: Enterprise Grid, Zoom S2S app, KnowBe4 key).

Related detail: the spec's "3–5 seed rules per module" guideline conflicts with its own
zoom/knowbe4 lists (2 each); the recipes follow the explicit lists.

## 2026-08-24 — Phase 8 partial: trgm search shipped ahead of Phase 7 (user-directed)

Migration 0005 adds `pg_trgm` + `events_search_trgm` (GIN, gin_trgm_ops on search_text)
and the provisioned Search dashboard ($q textbox → ILIKE, raw-JSON drilldown). Verified:
the index serves ILIKE plans on uncompressed chunks, and 0005 applies cleanly on a DB
that already has compressed chunks (tests/test_search.py). Remaining Phase 8 items (BM25
spike, Sigma→SQL, Slack Block Kit, per-module ingest-lag panel, pgBackRest docs) and all
of Phase 7 (HA) are still open. Phase-ordering deviation is deliberate.

## 2026-08-24 — Phase 7 HA: timescale/timescaledb-ha instead of Spilo

The spec planned Zalando Spilo with a build-time TSL verification. Verified instead that
**published Spilo images ship the Apache-only TimescaleDB** (`TIMESCALEDB_APACHE_ONLY=true`
default build arg), which the licensing note rules out — using them would need a full
source rebuild. `timescale/timescaledb-ha:pg18.4-ts2.29.2` is strictly better: TSL build
maintained by Timescale, Patroni 4.1.5 with etcd3 support bundled, arm64+amd64. Node
services keep the spec's `spilo1..3` names so the HAProxy config matches the spec.
Verified in `make ha-failover-test`: `SHOW timescaledb.license` = `timescale` on the
cluster, election in ~3s, zero loss under 20 inserts/s, jobs follow the primary.

Two additions to the spec's HAProxy config: a `resolvers docker` section with
`init-addr libc,none` (containers change IP across restarts; the static config would pin
first-seen IPs and never re-admit a restarted node) and a `global maxconn`.

## 2026-08-24 — BM25 verification spike: pg_textsearch stays OFF (spec §Analyst search)

Empirical findings (2026-08-24):

- `pg_textsearch` **ships in `timescale/timescaledb-ha:pg18.4-ts2.29.2`** (the HA image)
  together with vectorscale, and works without any `shared_preload_libraries` entry:
  `CREATE EXTENSION pg_textsearch` succeeds plain.
- The `bm25` access method **works on hypertables**: `CREATE INDEX ... USING bm25 (col)`
  propagates to chunk indexes (`_hyper_*_chunk_*_bm25`). Query operator is `<@>`.
- `pg_textsearch` is **absent from `timescale/timescaledb:2.29.2-pg18`** (the alpine dev
  image) — no package, no .control file.
- Write overhead at ingest rates: unmeasured (moot given the above).

Verdict per the spec's gate ("if any of it fails, trgm is the answer and BM25 stays
off"): the dev-image criterion fails, so **BM25 stays off** and no `CAIB_SEARCH_BM25`
flag is wired. Revisit if the alpine image ever ships pg_textsearch, or if a deployment
standardizes on the timescaledb-ha image for single-node too — the hypertable mechanics
are proven to work.

## 2026-08-24 — considered: standardize dev on timescaledb-ha (deferred)

Phase 7 landed on `timescale/timescaledb-ha` for the cluster, and the BM25 spike proved
that image ships pg_textsearch (works on hypertables) and pgBackRest. Standardizing the
dev single node on it would buy dev/prod image parity, reopen the BM25 option, and
simplify the backup docs (no image bake).

Deferred for now: the alpine image is ~5x smaller, gets pulled by every CI run and every
local test session, and nothing currently *requires* what the HA image adds — trgm covers
the search need and the BM25 write-overhead question is still unmeasured. Revisit when
(a) BM25 becomes a product requirement, or (b) a deployment already runs timescaledb-ha
single-node and parity starts paying real debugging dividends. The switch itself is
mechanical: image + PGDATA path (`/home/postgres/pgdata/data`) in compose, conftest,
bench, and CI.

## 2026-08-25 — Phase 8 status: complete; project status: engineering done

Earlier entries list Phase 8 items as remaining — superseded. Shipped since:
trgm search index + Search dashboard (0005); BM25 spike concluded (off, findings above);
Slack Block Kit notifications (auto-detected for hooks.slack.com); Sigma→SQL converter
(`caib_core.sigma`, existence shape); per-module ingest-lag panel; pgBackRest docs plus
a shipped backup overlay, leader-aware HA backup script, and an automated
`make restore-test` drill. CAIB is licensed Apache-2.0 (spec open question 5 closed).
Bench re-measured with the search index: 14.7k events/s ingest, 2.0 GB hot (0.47 GB trgm
index), 0.22 GB cold — 9.3x vs hot footprint, ~6x pure data compression (indexes don't
exist on compressed chunks).

Remaining work needs vendor accounts, not a keyboard: live API verification for the four
fixture-verified modules, slack/zoom/knowbe4 builds from the HOWTO recipes, and the
optional Phase 9 archive tier. Next milestone is real-data dogfooding.

## 2026-08-24 — caib_ro also gets SELECT on `ingest.*`

Spec §Data model says caib_ro gets SELECT on `caib.*`, `detect.*` only, but the specced
Overview dashboard reads `ingest.landing` (backlog stat) and `ingest.dead_letter` (count).
Grants therefore include SELECT on the `ingest` schema for caib_ro. Read-only, no secrets
in those tables (payloads are the same data as `caib.events.raw`).

## 2026-08-25 — P0 hardening round (post-handover code review)

A review of the shipped code against its own docs turned up five things that
would bite on first real use. All fixed; recording what was found, because
three of them were invisible from the outside.

**detect failed open on connection loss.** The loop catches `psycopg.Error` per
rule so one broken rule can't kill the engine — which also swallowed
connection loss. After a Postgres restart or an HA failover, every tick raised,
the process stayed up, `restart: unless-stopped` never fired, and detection
stopped silently. Reproduced by terminating detect's backend: it ran on
regardless. The loop now re-raises when `conn.closed or conn.broken` and lets
compose restart it, matching the crash rules the loader and pollers already
follow. `make ha-failover-test` never caught this because it exercises the
inserter/loader path only.

**Nothing watched the engine itself.** Dead-man rules cover a source going
quiet, but a stopped engine evaluates no rules, including the dead-man ones —
the alarm cannot detect its own absence. Added `ops.heartbeat` (migration 0006,
stamped every cycle by loader and detect), `caib-core healthcheck --service X
--max-age N`, compose healthchecks on both loops, and an Overview panel. Also
the honest limit, now documented: a Docker healthcheck marks a container
unhealthy, it does not restart it and it does not page anyone. External
monitoring still owns liveness. `caib_ro` gains SELECT on `ops` for the panel.

**The Overview ingest-lag panel was unbounded.** `max(event_time) GROUP BY
module FROM caib.events` with no time predicate, refreshing every minute — a
walk across all 90 days of chunks, compressed ones included, and a direct
violation of the spec's own panel data policy. Rewritten to take the module
list plus a fallback timestamp from `caib.events_5m` and the precise timestamp
from a 2-day rowstore window, so a silent module still shows a growing lag
instead of disappearing. `tests/test_dashboards.py` now EXPLAINs every
provisioned panel and fails on any panel reading `caib.events` without a time
bound — the dashboards previously had no test coverage at all.

**The documented first run shipped published passwords on 0.0.0.0.** `make up`
did `cp .env.example .env`, and compose published 5432 and 3000 with no
interface prefix (which bypasses ufw/firewalld — Docker owns its own iptables
chain). The `:?set in .env` guards only catch *unset*, never *unchanged*. Now:
`scripts/init_env.sh` generates random passwords on first run, `CAIB_BIND_ADDR`
defaults to `127.0.0.1` for both published ports, and `scripts/preflight.sh`
refuses to start when example passwords meet a non-loopback bind
(`CAIB_ALLOW_DEFAULT_SECRETS=1` overrides). Defaults on loopback warn only —
refusing there would break the dev flow the demo depends on.

**TLS was hardcoded off.** Every connection URL was bare and the Grafana
datasource pinned `sslmode: disable` in a tracked provisioning file, so no
deployment could enable TLS without editing the repo. Single `CAIB_PG_SSLMODE`
(default `prefer`) now drives every app URL and the datasource via
`$__env{}` — verified live that Grafana interpolates it (`jsonData.sslmode:
prefer`, datasource health OK). `prefer` verifies nothing; `docs/DEPLOY.md` is
new and says so, along with the reverse-proxy, secrets, monitoring, retention,
and known-rough-edges list (containers still run as root; images build from
`pip install .`, not `uv.lock`).

Not addressed in this round, still open: unbounded `detect.outbox` /
`detect.alerts` / `ingest.dead_letter` growth; `CAIB_NOTIFY_FORMAT` documented
but never passed to the detect service; a bad `CAIB_NOTIFY_MIN_SEVERITY`
crashing at first alert instead of at startup; SIGHUP reload killing the
process on a YAML error; a permanently-flooding rule notifying only once; no
dependabot; CI not building the Dockerfiles.
