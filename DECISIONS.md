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
with the concrete number in the SQL comment (400d) — revisit when open question 2 is
answered; it's a `remove_retention_policy`/`add_retention_policy` call either way, not a
migration edit.

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

## 2026-08-24 — caib_ro also gets SELECT on `ingest.*`

Spec §Data model says caib_ro gets SELECT on `caib.*`, `detect.*` only, but the specced
Overview dashboard reads `ingest.landing` (backlog stat) and `ingest.dead_letter` (count).
Grants therefore include SELECT on the `ingest` schema for caib_ro. Read-only, no secrets
in those tables (payloads are the same data as `caib.events.raw`).
