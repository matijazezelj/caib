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

## 2026-09-02 — reliability/ops backlog (the P1s from the 2026-08-25 review)

The six items the P0 round left open, all fixed. Migration 0007 carries the
whole schema side: three indexes, `detect.alerts.last_notified_at`,
`detect.outbox.claimed_at`.

**Nothing bounded the three plain tables.** `caib.events` is the only
hypertable, so `add_retention_policy` covers it and stops there;
`detect.outbox`, `detect.alerts` and `ingest.dead_letter` are plain heaps and
grew forever — the outbox fastest, since every notification stays in it after
delivery. New `caib_core.retention.sweep()`: delivered outbox rows past
`CAIB_RETENTION_OUTBOX_DAYS` (30), **closed** alerts past
`CAIB_RETENTION_ALERTS_CLOSED_DAYS` (90) by `last_seen`, dead-letter rows past
`CAIB_RETENTION_DEAD_LETTER_DAYS` (30); any knob at 0 keeps everything. The
detect loop calls it at most hourly on the monotonic clock, in its own
transaction, behind the same `_connection_lost` re-raise as the drain;
`caib-core retention --once` runs it by hand. Deliberate: open and acked alerts
are never eligible at any age — deleting one drops an investigation *and*
un-dedups the rule, so the next tick would re-open it as new and notify again.
Undelivered outbox rows are likewise never touched. Each table gets its own
transaction; the DELETEs are unbounded in row count, which is fine hourly but
means a first sweep against a long-neglected outbox is one big transaction.

**`CAIB_NOTIFY_FORMAT` was documented but not wired.** It had been in
`.env.example` since the notifier learned Slack Block Kit, and
`docker-compose.yml` never put it in detect's environment, so setting it did
nothing and the format stayed whatever `resolve_format()` inferred from the
URL. Passed now. `compose.ha.yml` needs no change — its `detect` block only
overrides `CAIB_PG_URL` and compose merges environment maps. The general fix is
the test: `tests/test_env_guards.py` asserts every
`CAIB_DETECT_`/`CAIB_NOTIFY_`/`CAIB_RETENTION_` variable in `.env.example`
reaches the container, so the next documented-but-unwired knob fails CI.

**A bad `CAIB_NOTIFY_MIN_SEVERITY` waited for the first alert to crash.**
`Cfg.from_env()` took it unchecked and only `SEVERITIES.index()` looked at it,
so `HIGH` or `urgent` started cleanly, passed its healthcheck, and raised
`ValueError` out of a rule's transaction whenever something finally fired.
Validated in `from_env()` now: `SystemExit` at startup with the allowed values.

**A YAML error in a rule file killed a running engine.** The SIGHUP reload
caught only `ValueError` and `SystemExit`, and `yaml.YAMLError` is neither — so
the failure mode of the exact thing SIGHUP exists for (you just edited a rule)
was process death. Catches `Exception` and keeps the old rule set. The test
spawns the engine, SIGHUPs it with a tab-indented rule file in the directory
and watches `ops.heartbeat` keep advancing; verified it does fail against the
old handler (heartbeat stops within seconds).

**A permanently-flooding rule notified once, ever.** Overflow upserts a fixed
`dedup_key='overflow'`, so only the first was `is_new`; every later one folded
in as a `recurrence` and, with `notify_recurrence` off by default, was never
enqueued. A rule flooding for a week therefore looked identical to silence
after day one. Overflows keep `kind='meta'` and are rate-limited instead — at
most one per rule per `CAIB_DETECT_OVERFLOW_NOTIFY_INTERVAL` (default 1h, 0 =
every tick), off `detect.alerts.last_notified_at`, which `_enqueue` stamps in
the same transaction as the outbox row (a rolled-back window can't consume the
interval, and a severity-filtered alert never stamps it). The alert row still
folds to one: the dedup was right, only the silence was wrong.

**`notify.drain` POSTed inside its own open transaction.** It selected the
batch `FOR UPDATE` and then POSTed each row without leaving that transaction,
so a hung webhook held row locks and an idle-in-transaction connection for
`TIMEOUT_S` per row — 500s for a full batch — blocking autovacuum on
`detect.outbox` and holding a snapshot open across a failover. Restructured to
claim → POST → settle: a short transaction stamps `claimed_at`, the POSTs run
with nothing open, a second short transaction sets `sent_at` for what went and
clears `claimed_at` for what didn't (so a 500 is retryable immediately, not
after the timeout). Ordering, stop-at-first-failure and the `(sent, failed)`
return are unchanged. The trade is explicit: rows are now in flight without a
lock, so a sender that dies mid-POST leaves them claimed until `CLAIM_TIMEOUT`
(5 min) makes them reclaimable. Delivery was and remains at-least-once — a POST
that succeeds and then fails to record redelivers. `drain()` must be called
with no caller transaction open, or psycopg makes its transactions savepoints
and the POSTs are back inside one; the docstring says so and a test asserts
`pg_stat_activity.state = 'idle'` during the POST.

Not addressed, still open: the supply-chain/CI backlog (no dependabot, CI
builds neither Dockerfile nor validates the compose files, images run as root
and install from `pip install .` rather than `uv.lock`, `actions/checkout@v4`
and `setup-uv@v5` on deprecated Node 20, no `SECURITY.md`).

## 2026-09-02 — supply chain and CI (todo #7)

The whole item, closed. Nothing here changes runtime behaviour; the two Python
images are rebuilt from the inside out and verified against a live stack.

**Actions pinned to SHAs, not tags.** `actions/checkout@v4` and
`setup-uv@v5` were both on the deprecated Node 20 runtime. Now
`actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` (v7.0.1) and
`astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d` (v10.0.1), both
node24, each with the version as a trailing comment. Both tags are lightweight,
so `gh api .../git/ref/tags/<tag>` returns the commit directly. SHA pinning is
not optional here: setup-uv stopped publishing floating major/minor tags at v8
precisely because a movable tag is a supply-chain hole, so `@v10` does not
exist. Checked the majors we skipped for behaviour changes that touch us —
v10's cache-poisoning default only fires on `pull_request_target`,
`workflow_run` and `release`, none of which this workflow uses.

**Dependabot covers more than expected — verified, not assumed.** The obvious
reading is that the `docker` ecosystem watches Dockerfiles and the six stack
images in the compose files go unwatched. That is half right. `docker`'s
fetcher matches `/dockerfile|containerfile/i` only, but there is a separate
`docker-compose` ecosystem whose fetcher matches
`/(docker-)?compose(-[\w]+)?(?>\.[\w-]+)?\.ya?ml/i` — which covers all four of
`docker-compose.yml`, `compose.backup.yml`, `compose.ha.yml` and
`compose.render.yml`. Both ecosystem names, and the `uv` one, were read out of
`dependabot-core` (`common/lib/dependabot/config/file.rb`,
`docker/lib/dependabot/*/file_fetcher.rb`) rather than taken from memory. So
four entries: `github-actions`, `docker` on `/core /pollers /backup`,
`docker-compose` on `/`, `uv` on `/` with minor+patch grouped. The locally
built `caib-*` images are ignored — there is no registry behind them.

**Two gaps dependabot cannot close, hence `scripts/check_image_pins.sh`.**
First: the `timescale/timescaledb` service container in
`.github/workflows/ci.yml`. The `github-actions` ecosystem parses `uses:` refs
and nothing else, so the image the test job actually runs against is in no
ecosystem's scope — and since CI asserts the TSL license against that build, a
stale pin there means asserting it about the wrong version. Second: agreement
between copies. That same image is pinned in `docker-compose.yml`,
`backup/Dockerfile` and the workflow; dependabot opens one PR per file and is
perfectly happy to leave them on three different tags. The script inventories
every `image:`, `FROM` and `COPY --from=` in the repo and exits non-zero on an
untagged or `:latest` image, or on a disagreement within
`timescale/timescaledb`, `ghcr.io/astral-sh/uv` or `python`. The render
sidecar is allowlisted as deliberately floating — it is a throwaway used to
regenerate screenshots, never part of a running stack. Both failure paths were
exercised against a scratch compose file before the script was committed.

**CI builds the images and validates the compose files.** New `build` job:
`docker build` all three (no push), then `docker compose config -q` on the base
file and on each `compose.*.yml` overlay combined with it, against a `.env`
from `scripts/init_env.sh`. `--profile '*'` is load-bearing — the per-module
poller and vector services sit behind profiles, and without it `config`
renders the file without them and validates nothing about them. `test` now
needs `lint`; `build` has no `needs` and runs beside both.

**Images build from `uv.lock` and run as uid 10001.** `pip install .` against
the member `pyproject.toml` meant the lockfile that CI and the tests resolve
against had no bearing on what shipped in a container: pip re-resolved every
`>=` on every build, so an image built today and one built last month were
different software with the same tag. Both Dockerfiles are two stages now — a
builder that runs `uv sync --frozen --no-dev --no-editable --package <member>`
over the whole workspace, and a runtime stage that takes only `/app/.venv`.
`--frozen` makes a stale lock a build failure rather than a silent
re-resolution, which is the entire point. `--no-editable` is what makes the
venv relocatable: uv installs workspace members editable by default, and an
editable install would have left the runtime image's `.pth` pointing at a
source tree that is not there.

uv itself comes in as `FROM ghcr.io/astral-sh/uv:0.11.17 AS uv` and is then
copied out, rather than the shorter `COPY --from=ghcr.io/astral-sh/uv:0.11.17`
the uv docs suggest. Deliberate: dependabot's docker parser matches on
`FROM` lines only (`docker/lib/dependabot/docker/file_parser.rb`), so the
documented form would have added a seventh pinned image that nothing watches.
This adds one image to track and one build stage; the alternative was an
unpinned `pip install uv`, which would defeat the exercise.

Non-root was cheap because nothing writes: `caib_core` and `caib_pollers` have
no `open(..., 'w')`, no tempfiles, no state directory. So `/app` stays
root-owned and read-only to `caib` and nothing is chowned. uid/gid 10001 is
above the range distros allocate, so it cannot collide with a host account
through a bind mount. `ENV PATH=/app/.venv/bin:$PATH` is what keeps the
exec-form `ENTRYPOINT` and the compose `["CMD", "caib-core", "healthcheck",
...]` resolving — Docker resolves an exec-form binary against the image's PATH.
**No healthcheck or entrypoint tweak was needed**; the read-only bind mounts
(`./db/migrations`, `./detect/rules`) are world-readable and non-root reads
them fine.

Verified against a real stack, not just a build: `make up` brings
timescaledb, loader, detect and grafana to healthy and migrate to `Exited (0)`
— 7 migrations applied under `caib_app`, 22 rules loaded from the bind mount,
the four dead-man rules firing. `docker compose run poller-cloudflare-audit
--once` reaches the Cloudflare API and fails only on the absent token, so the
wheel-installed package and psycopg both work as uid 10001. `make down` clean.

`backup/Dockerfile` is untouched: it is the TimescaleDB image with pgbackrest
added, it has no Python packaging to move onto the lock, and its entrypoint has
to start as root before dropping to `postgres` itself.

**`SECURITY.md` and `CHANGELOG.md`.** The security policy is deliberately
unflattering — supported versions is "main, there are no tags", the response
promise is an acknowledgement within 7 days and explicitly not a fix timeline,
and the largest section lists what CAIB does not do (no auth or TLS in front of
Grafana or Postgres beyond the loopback bind, secrets as container env vars, no
multi-tenancy, healthchecks that page nobody) so that those arrive as
documented behaviour rather than as four separate reports. The changelog keeps
everything under `Unreleased`, grouped by PR; the initial build is not
itemised, because that is what this file is for. README gained a Releases
section with the `git tag -s v0.1.0` recipe. **No tag was created** — the first
one waits on the dogfooding round.

Left open: the repo is still private, and the `-s` recipe assumes a signing key
that is not configured yet. Neither is a keyboard problem.
