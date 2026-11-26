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

## 2026-09-02 — agent query layer (todo #5)

Read-only HTTP/JSON API + MCP server over one shared query core, as the todo
asked. Five findings and three deviations, in the order they bit.

**`caib_ro` already existed, and it is the wrong role for this.** The brief said
to create `caib_ro` in migration 0008 with SELECT on five tables. But
`caib_ro` has existed since the initial build: it is Grafana's datasource,
created in `caib_core.migrate.bootstrap()`, and `sync_grants()` re-grants it
SELECT on **all tables** in `caib`/`ingest`/`detect`/`ops` on every single
migrate run — deliberately, because the Overview dashboard reads
`ingest.landing` and `ingest.dead_letter` (the 2026-08-24 entry above). So the
brief's grant list is strictly narrower than what `caib_ro` has, and narrowing
`caib_ro` to match would have broken shipped panels that reviewers consider
frozen.

Decision: a **second** role, `caib_agent_ro`, with the enumerated five-table
grant, leaving `caib_ro` untouched. The alternative — reuse `caib_ro` — would
have handed an LLM the unsanitised vendor payloads in `ingest.*`, which is the
exact thing the brief's grant list was drawn to prevent. Env var
`CAIB_AGENT_RO_PASSWORD`; the connection URL keeps the brief's
`CAIB_RO_PG_URL` name.

**Role creation cannot live in a migration file, and this is structural.**
Migration files run under `SET LOCAL ROLE caib_app`, and `caib_app` is neither
a superuser nor a `CREATEROLE` role. `CREATE ROLE`, `GRANT CONNECT ON
DATABASE` and `ALTER ROLE ... SET` all require privileges it does not have —
which is precisely why the existing role bootstrap lives in `migrate.py` in the
first place (its module docstring says so). Split accordingly: the role object
and its session defaults go in `bootstrap()` next to `caib_app` and `caib_ro`;
the schema `USAGE` and the five table `SELECT`s go in
`0008_caib_agent_ro_role.sql`, which `caib_app` can grant because it owns those
objects. That keeps the security-relevant half — the grant list — in a file
someone can review, which was the point of asking for a migration.

The session defaults (`statement_timeout=15s`,
`idle_in_transaction_session_timeout=30s`,
`default_transaction_read_only=on`) are re-applied on **every** migrate rather
than once, so an operator who resets them gets them back. They bind anything
holding the credentials, `psql` included.

**Granting SELECT on a continuous aggregate grants more than it says.**
TimescaleDB propagates the grant to the cagg's own materialisation objects in
`_timescaledb_internal` — six extra rows in
`information_schema.table_privileges` for `events_5m` and `actors_1h`. They
hold counts already derivable from `caib.events`, which is granted anyway, so
nothing extra is exposed; recorded because "the role can read exactly five
tables" is otherwise a claim the catalogue contradicts, and the grant-set test
has to allow for it.

**`default_transaction_read_only` proves nothing on its own.** It is a
`USERSET` GUC: any client holding the credentials can `SET` it off. The
read-only property is the *absence of INSERT/UPDATE/DELETE grants*, and the
tests assert it that way — flip the GUC off first, then require
`InsufficientPrivilege` on five writes and a `CREATE TABLE`. Related finding
while writing those: the GUC is read when a transaction begins, so the `SET`
needs a commit before it takes effect; psycopg has already opened a transaction
by then.

**Deviations from the brief, each deliberate.**

- *`list_rules(conn_or_path)` takes a path, not a connection.* The rule set is
  YAML on disk. It reads through `caib_core.detect.load_rules` — the same call
  the engine makes — so it cannot describe a rule set the engine is not
  running. `detect.rule_state` is the only DB side of rules and holds
  watermarks, which are engine internals and are not granted. The rule SQL is
  not returned either: it is detection logic, and nothing an analyst agent does
  with an alert needs it.
- *`agentapi/` uses a `src/` layout* while `core/` and `pollers/` are flat.
  Following the brief's explicit `agentapi/src/caib_agentapi/mcp_server.py`
  path; hatchling handles it, and it is the more standard layout. Noted because
  it is the one inconsistency in the workspace.
- *A third Dockerfile rather than extending `core/Dockerfile`.* The brief said
  to prefer the smaller pin surface, which would have meant installing
  `caib-agentapi` into `caib-core:dev`. That would put FastAPI, uvicorn, the
  MCP SDK and their transitive tree — cryptography, pyjwt, starlette,
  jsonschema — inside the image that runs `migrate`, the loader and the
  detection engine. Those three **write** to the audit database; the agent
  layer only reads it, as a different role. Keeping the web stack out of the
  writers' image is the smaller blast radius. The cost is not two new pins:
  `agentapi/Dockerfile` carries the same `ghcr.io/astral-sh/uv:0.11.17` and
  `python:3.12-slim` refs the other two do, and `scripts/check_image_pins.sh`
  already fails when copies of a pin disagree.

**`module_health` differs from the Overview panel it mirrors, twice.** The
panel takes its module list from `caib.events_5m` alone, so a freshly started
stack reports no modules at all while happily ingesting; this takes the union
of the 30-day cagg and the 2-day rowstore. And the panel's landing-backlog,
dead-letter and Timescale-job-error stats are simply absent, because
`caib_agent_ro` cannot read those tables — an operator question, and the
operator has Grafana. Heartbeats come back as a separate list from modules,
because `ops.heartbeat` is keyed by service (loader, detect) and there is no
per-module heartbeat to report; a quiet module shows up as `lag_seconds`
growing.

**`top_actors` picks its source and says so.** `caib.actors_1h` buckets
hourly, so a window starting at 09:30 would either count the whole 09:00 bucket
or drop it. It reads the cagg only when `since` lands on an hour boundary and
counts `caib.events` otherwise, and reports which in `source` rather than
leaving the caller to guess at the error bars.

**Guardrails live in the core, not the transports**, so HTTP and MCP cannot
drift apart in what they will answer or refuse: the LIMIT caps, the 90-day
lookback ceiling (matching the events retention policy), timestamp coercion,
and `raw` being opt-in. Clamped windows and truncated pages are reported in
every response — an agent that does not know a result was truncated reports the
truncation as the finding. Out-of-enum filters raise instead of matching zero
rows, because an empty result set reads as "nothing happened", which is the
worst possible wrong answer about an audit log.

**The MCP SDK resolved to 2.x, not 1.x**, whose API differs enough to matter:
`MCPServer` rather than `Server` plus decorators, `Tool.input_schema` rather
than `inputSchema`, and a `ToolError`/`UnexpectedToolError` split where only
the former's text reaches the model. Pinned `mcp>=2.1` after first writing
`>=1.2`, which would have let a resolver pick a version that cannot import.
Tool input schemas are generated from `Annotated[...]` + pydantic `Field`
rather than hand-written JSON Schema, so the schema an agent reads and the code
that runs cannot drift.

**No writes in v1**, as specified: no ack, no close, no create, in either
transport, and no grant behind them if there were. A test fails the build if a
tool name ever contains ack/close/create/update/delete/silence. The alert
lifecycle has not survived a dogfooding round yet, and a write API designed
before then would encode guesses.

Suite 104 → 189 tests; the 3 failures are the pre-existing
`host.docker.internal` Vector/localstack ones. Left open: nothing here has run
against real data — the numbers, the lag semantics and the usefulness of the
tool descriptions are all untested by an actual analyst session, which is what
todo #3 is for. `scripts/init_env.sh` does not touch an existing `.env`, so an
already-deployed stack must add `CAIB_AGENT_RO_PASSWORD` and
`CAIB_AGENT_API_TOKEN` by hand and re-run `make migrate`; that is documented in
`docs/AGENT_API.md` but not automated.

## 2026-09-16 — agent query layer: review fixes, and MCP over Streamable HTTP

A second implementation of todo #5 was written in a stale checkout (26 commits
behind; the local CLAUDE.md still listed #5–#7 as open) before anyone noticed
PR #16 had merged. It was compared against the merged one instead of pushed,
and discarded: the merged design wins on the security boundary
(`caib_agent_ro` with five enumerated grants beats reusing Grafana's
`caib_ro`, which reads `ingest.*`), on role-level rather than per-connection
timeouts, on `top_actors` choosing an exact source, and on an SDK-backed MCP
server. The comparison found four defects in the merged code, each reproduced
against a live database before fixing, plus one feature worth porting — and a
fifth defect that only a real compose stack showed.

**`make up` broke for every pre-existing `.env`.** Compose passed
`CAIB_AGENT_API_TOKEN: ${CAIB_AGENT_API_TOKEN:?set in .env}`. Compose
interpolates every service whether or not its profile is enabled, so an `.env`
without the variable fails `docker compose config` — and so `make up` — even for
someone who never enables `agent-api`. CI missed it because its build job
generates a fresh `.env` first. Now `:-`; `Settings.from_env` was already the
real guard (refuses empty or unset). `tests/test_compose_env.py` strips every
opt-in variable from `.env.example` and runs `config` on the base file and the
HA overlay; it fails against the old compose file. Rule for future opt-in
services: required-ness goes in the service's startup check, never in `:?`.

**`actor_activity` over HTTP 404'd for every IAM ARN.** `/v1/actors/{actor}`
cannot match a `/`, and percent-encoding does not help: the server decodes
`%2F` before routing. CloudTrail actors are ARNs (`…:user/carol`), so the
endpoint was unusable for the primary module. Now `{actor:path}`, still
declared after `/actors/top`. MCP was unaffected (arguments, not paths).

**Open alerts aged out of the default view.** `get_alerts`/`alert_summary`
filtered every status on `last_seen >= now() - 7d`, and `_window` clamped to the
90-day *events* retention. An open, unacknowledged root-login alert whose rule
last matched 10 days ago was absent from `get_alerts?status=open`, and
`alert_summary` reported `open: 0`; at 120 days no `since` could reach it. But
`caib_core.retention` deliberately never deletes open or acked alerts — they
are open cases — so the view contradicted the store. Now: without `since`,
`status <> 'closed' OR last_seen >= since` (closed alerts keep the 7-day
default); with an explicit `since`, it applies to every status. Alerts are not
clamped, and have no upper bound (`last_seen` is a vendor event time and can
sit slightly ahead of `now()`). The window metadata says which rule applied
(`applies_to`).

**On a stock compose stack the agent API could never log in.** `migrate` sets
`caib_agent_ro`'s password from `CAIB_AGENT_RO_PASSWORD`, but compose never
passed that variable to the `migrate` container: it logged "not set; password
for role caib_agent_ro left unchanged", the role stayed password-less, and
`agent-api` retried `password authentication failed` forever while its
`/healthz` (process-only) reported healthy. Every test calls `migrate.run()`
with the variable already in the process env, and CI's compose check only
validates interpolation, so nothing caught it. Found by `up --build` on a
throwaway project. Now passed with `:-` (opt-in, same reasoning as the token);
`test_migrate_receives_every_role_password_it_syncs` reads the variable names
out of `sync_passwords` and asserts compose hands each one to `migrate`.

**`truncated` reported a full page, not remaining rows.** `len(rows) == limit`
is true when exactly `limit` rows match. Every paged query now fetches
`limit + 1`.

**MCP over Streamable HTTP, at `/mcp` on the existing service.** stdio needs
the `caib_agent_ro` password on every agent's machine; HTTP needs only the
bearer token, which is the credential already meant for agents. Built from the
SDK (`MCPServer.streamable_http_app(stateless_http=True, json_response=True)`)
but routed by our FastAPI app so it sits behind the same token check —
`GuardedMcp`, a small ASGI wrapper, because FastAPI dependencies do not run on
a raw ASGI route and the SDK's auth hooks are OAuth-shaped. The SDK session
manager runs inside the app lifespan. Stateless + JSON because the server never
initiates messages, and it keeps restarts and replicas trivially safe.

The SDK's DNS-rebinding protection is turned **off** and replaced: for a
`127.0.0.1` host it auto-enables a Host allowlist of localhost names, which
421s every request arriving through a reverse proxy — the documented way to
reach this service off-host. `origin_allowed` instead refuses any request whose
`Origin` is not in `CAIB_AGENT_ALLOWED_ORIGINS` (default empty), on `/v1` as
well as `/mcp`. Non-browser clients send no Origin; a rebound browser page
does, and would not hold the token anyway. Tested: a foreign Host is served, a
foreign Origin is 403 even with the token, and the official SDK client
(`mcp` 2.1.1, over `httpx2`, against a real uvicorn listener) initializes,
lists seven tools, and gets both a result and a readable `isError`.

## 2026-09-16 — making dependabot PRs' CI mean something

Triage of the 14 open dependabot PRs found that their checks could not be
trusted in either direction.

**Red for reasons unrelated to the bump.** Two PRs touching no Python (#24
setup-uv, #8 haproxy) failed `test` on TimescaleDB background-job races:
`LockNotAvailable` ("concurrent refresh") in `test_caggs_answer_after_refresh`
and `DeadlockDetected` in `test_retention_policy_drops_aged_chunk`. Migrations
add six policy jobs and the scheduler starts running them on each fresh test
database within seconds, colliding with tests that refresh or run policies
themselves. `migrated_db` now unschedules every policy job (`alter_job(...,
scheduled => false)` — definitions stay, `CALL run_job` still works) and waits
out any run already in flight. The race did not reproduce locally in 8
isolated runs (it needs CI's timing across the full suite), so the fix is
matched to the recorded failures rather than to a local repro.

**Red by construction.** Dependabot opens one PR per Dockerfile directory, and
`check_image_pins.sh` requires the copies to agree, so the per-directory uv
0.12.11 (#18–#20) and python 3.14 (#6, #12) PRs could never pass
individually. The `docker` ecosystem entry now groups all four directories into
one PR.

**Green without testing the bump.** The Vector tests pin
`VECTOR_IMAGE = "timberio/vector:0.57.0-debian"` in Python, and
`tests/conftest.py`/`scripts/bench.py` pin the TimescaleDB image the same way.
No dependabot ecosystem reads those, and the pin checker only scanned compose,
workflows and Dockerfiles — so #7 (Vector 0.58, compose only) was green while
every Vector test still ran 0.57. The checker now also collects module-level
`*_IMAGE = "..."` constants from `tests/` and `scripts/`, and
`timberio/vector` joins the must-agree list; a compose-only Vector bump now
fails until the test pins move with it.

## 2026-09-16 — dependency batch: Python deps, uv 0.12, setup-uv, Vector 0.58

Landed as one PR instead of merging the dependabot PRs: #22, #23 and #25 all
rewrite `uv.lock`, and #18–#20 must move together. Checked before merging, not
inferred from green checks:

- **Python deps.** `mcp` 2.1.1 → 2.2.0 is the one that matters: the `/mcp`
  endpoint uses the SDK's `streamable_http_app`, `session_manager` and
  `StreamableHTTPASGIApp`. The full suite (201, minus the three Vector e2e tests
  this dev host cannot network) passes on the new lock, including the
  official-client test against a live uvicorn.
- **uv 0.12.15.** The relocked `uv.lock` (written by uv 0.12.15) still passes
  `uv lock --check` under uv 0.11.17, so a mixed toolchain during rollout reads
  it. Upstream 0.12.0 breaking changes don't touch `sync --frozen`, the lock
  version, or the `UV_*` env vars the Dockerfiles set.
- **setup-uv v10.1.0.** Pinned SHA `bec219d…` confirmed to be the upstream
  `v10.1.0` tag commit before copying it in.
- **Vector 0.58.0.** The 0.58 upgrade guide's breaking changes (removed
  `azure_monitor_logs` sink, removed buffer metrics, `http_server` encoding,
  `influxdb_logs` namespace, `logdna` alias, `webhdfs` scheme, templated URI
  hosts) touch nothing CAIB uses. The test pins move with the compose pin, so
  CI's Vector end-to-end tests are the verification. `API_NOTES.md` and the
  HOWTO still say "verified on 0.57.0": that is what was verified then, and is
  left as history.

## 2026-09-16 — Grafana 13.2.2

A major-version bump, so verified on a running stack rather than from the green
check (CI's dashboard test EXPLAINs panel SQL; it never starts Grafana).

Demo-seeded stack on 13.2.2 with the render overlay: `/api/health` reports
13.2.2, all 7 provisioned dashboards load, the `caib` datasource health check
passes (`$__env{}` interpolation and `timescaledb: true` intact), and the log
has no provisioning errors besides the empty `provisioning/alerting` and
`plugins` directories. The four README screenshots were re-rendered at their
original sizes and default time ranges and compared by eye against the 12.4.9
ones: same data, colours and layout; differences are cosmetic (tighter panel
headers, so the Ingest lag table no longer clips its last row; data-link cells
in Top actors/actions now styled as links). The screenshots in `docs/img/` are
the 13.2.2 renders.

**The render overlay needed a change.** Grafana 13 removed the renderer plugin
and requires a shared token with the remote renderer service; without
`GF_RENDERING_RENDERER_TOKEN` it will not render. `compose.render.yml` now sets
it and the renderer's `AUTH_TOKEN` from `CAIB_RENDERER_TOKEN` (default
`caib-local-render` — the renderer publishes no port, so the default guards only
the compose network). Also noted from the 13.0 upgrade guide, none affecting
CAIB: dashboards move to unified storage on startup, numeric-id datasource APIs
are off by default, `grafana-cli`/`grafana-server` are gone, gzip is on.

## 2026-09-16 — HA overlay: etcd 3.7.1, HAProxy 3.4, timescaledb-ha pg18.6-ts2.30.0; HAProxy init-state

Tested as an operator would meet it: `make ha-up` on the old versions (etcd
3.6.4, HAProxy 3.2, `timescaledb-ha:pg18.4-ts2.29.2`) until one primary and two
streaming replicas, then the three bumps applied and `make ha-up` again over
the same volumes.

- **etcd 3.6.4 → 3.7.1 on the existing `etcd_data` volume worked**: the log
  shows `updated storage version 3.6.0 → 3.7.0` and Patroni carried on. Upstream
  requires clusters to be on 3.6.11+ before moving to 3.7; that guards
  mixed-version multi-member clusters, which this single-node lab etcd is not.
  A real three-member etcd should follow the upstream path.
- **timescaledb-ha pg18.6-ts2.30.0**: PostgreSQL 18.4 → 18.6 (minor, same data
  directory), pgBackRest still 2.59.1 so docs/BACKUPS.md stands, license
  `timescale`. The extension stays at 2.29.2 until migrate updates it — run
  with the TimescaleDB-branch migrate image through HAProxy it went to 2.30.0
  on the primary, and both replicas reported 2.30.0.
- **`make ha-failover-test` passed twice**: before the extension update
  (failover 2s, 392 events, zero loss) and after it plus a loader/detect
  restart (2s, 364 events, zero loss); Grafana answered, jobs followed the new
  primary, the old primary rejoined.
- **HAProxy 3.2 → 3.4**: `haproxy -c` clean. The default `balance` became
  `random` in 3.3; harmless here (one healthy server on :5000, read-only spread
  on :5001 is not load-sensitive).

**Found on the way: `make ha-up` over an existing cluster failed
intermittently**, in migrate, with `cannot execute GRANT in a read-only
transaction`. HAProxy starts every server UP until `fall` (3 × 3s) checks fail,
so for ~9s after a restart :5000 routes to replicas as well, and migrate's
connect-retry only covers connection failures. Reproduced on **3.2** as well as
3.4 (run 1 failed, run 2 passed on 3.2), so it predates this bump; fresh
`ha-up`s never hit it because initdb outlasts the window. Fix:
`init-state down` on both listeners (HAProxy ≥ 3.1), so a server takes traffic
only after `rise` checks pass. Four consecutive restarts on 3.4 then all
passed, each with migrate logging "database not ready" three times before
reaching the real primary.

## 2026-09-16 — Python 3.14 in the images, and CI tests what ships

Dependabot's grouped PR (#31, earlier #6/#12/#29) moved the three Dockerfiles
from `python:3.12-slim` to `3.14-slim`. The bump alone would have made CI less
honest: the test job runs `uv sync` on whatever Python the runner has (3.12 on
ubuntu-latest), so images would ship an interpreter CI never ran. Added
`.python-version` (3.14), which uv reads for `uv sync`/`uv run` locally and in
CI, without touching the workflow file. `requires-python >=3.12` and ruff's
`py312` target are unchanged: the code stays 3.12-compatible, only the
interpreter we test and ship moves.

Checked: every compiled dependency in `uv.lock` has cp314 manylinux wheels for
x86_64 and aarch64 (psycopg-binary, pydantic-core, pyyaml, cffi, rpds-py;
cryptography via abi3), so slim images never fall back to a source build — in
the built image `psycopg.pq.__impl__` is `binary`. All three images build; a
throwaway compose stack on 3.14 came up healthy (loader, detect, agent-api),
seeded demo data, and answered `/v1` and `/mcp`. The local test suite ran on
3.14.7 all session (202 passing, excluding the three Vector e2e tests this dev
host cannot network).

## 2026-09-16 — TimescaleDB 2.30.0, and migrate now updates the extension

Upstream 2.30.0 lists no backward-incompatible changes or deprecations touching
compression, retention, continuous aggregates, the jobs views or GIN indexes
(two new GUCs, both harmless). The bump itself is a pin change across the seven
places that name the image (compose, backup/Dockerfile, CI service container,
tests/conftest.py, scripts/bench.py, README, CLAUDE.md).

**The upgrade was the real work.** Verified in a 2.30.0 container: the image
still ships the 2.29.x libraries, so an existing volume keeps running 2.29.2 —
silently — and bootstrap's `CREATE EXTENSION IF NOT EXISTS` never changes that.
`ALTER EXTENSION timescaledb UPDATE` is accepted only as the first statement of
a session: after `SELECT count(*) FROM pg_class`, and even after reading
`extversion` from `pg_extension`, it fails with "cannot be updated after the
old version has already been loaded". So `migrate.run` now opens a dedicated
connection whose first statement is the unconditional update (a NOTICE no-op
when current; `UndefinedObject` on a fresh database, where bootstrap creates it
at the image version), then bootstraps on a second connection. CI's fresh
databases could never have caught the gap; `test_migrate_updates_an_older_
timescaledb_extension` builds one from `template0` (template1 carries the
image's version), installs the newest older version the image ships, and
asserts migrate brings it to the default. It fails without the change
(2.29.1 != 2.29.2 on the old image).

Rehearsed on a real volume: a demo-seeded 2.29.2 stack with 28 compressed
chunks, switched to the 2.30.0 image with `up -d`. migrate logged
"timescaledb extension at 2.30.0"; all 1996 events and 28 compressed chunks
were intact; cagg refresh, trgm search, the compression policy job and
loader/detect heartbeats all worked after `restart loader detect`.

Long-lived sessions opened before migrate ran (loader, detect, agent-api) keep
the old library until they reconnect; docs/DEPLOY.md §8 says to restart them.
Not automated: a migrate that restarts its siblings is a different design.

The HA image (`timescaledb-ha` → `pg18.6-ts2.30.0`) already moved in the HA PR
(#33), where this branch's migrate was run against the HA cluster: the extension
updated to 2.30.0 through HAProxy and replicated to both replicas.
Also removed three stale "known rough edges" from DEPLOY.md (root containers,
`pip install .`, no dependabot) that the supply-chain round had already fixed.

## 2026-11-10 — Model-selection guidance applied: agent docs, MCP descriptions, project subagents

Applied Anthropic's "which Claude model should you build with" guidance
(November 2026). CAIB calls no model itself, so it lands in three places:

- `docs/AGENT_API.md` gains "Which Claude model to point at it". It lists a
  starting model and effort per job: Sonnet 5.5 for triage and repeated
  investigation, Haiku 5.5 for bulk tagging, Opus 5.5 for open-ended
  reconstruction. It also gives the escalation order (self-check, then
  effort, then model), the safety-classifier fallback caveat (audit logs read
  as cybersecurity content), and the API-client gotchas: `tool_choice: auto`
  and content blocks read by type. **Unmeasured.** These are the vendor's
  defaults, not CAIB results. Measuring them belongs to todo #3.
- MCP tool descriptions now route between tools. `get_alerts` says how to get
  to the events behind an alert, and `top_actors` says it returns one row per
  (actor, module) and points at `actor_activity`. `list_rules` now raises a
  `ToolError` for a missing rules directory, where before it escaped as a
  generic "Error executing tool".
- `.claude/agents/`: `check-runner` (Haiku) runs ruff/pytest and reports
  failures; `module-reviewer` (Sonnet) runs one per module in parallel.

A Sonnet review of the MCP surface against the same guidance raised more
issues. They are deferred because each changes behaviour, which is the
user's call:
- `module` is not validated, so `module="aws"` returns zero rows. That is
  the same empty-means-nothing-happened failure the severity check exists to
  prevent.
- Default page sizes (100 events, 200 for actor_activity) with full `attrs`
  can use a lot of an agent's context. Options: smaller defaults, or
  opt-in `attrs`.
- A truncated page has no continuation cursor (`next_until`).
- `top_actors` has no `until`, so past incidents can't be bracketed.
- `q` is not escaped, so `_` matches any character.
- `list_rules` returns `source` (a server filesystem path) that its
  description does not mention.
- Enums are prose in the schema rather than `Literal`. They are still
  rejected server-side with a clear error.

## 2026-11-26 — Image batch: TimescaleDB 2.32.1, Vector 0.62, Grafana 13.3.1, etcd 3.7.4, uv 0.13.10

Consolidates dependabot #63 #66 #67 #68 #69 into one branch: each bumped one
file, so `check_image_pins.sh` failed them (and every `test` job failed on the
retention-test clock bug fixed in #71).

**TimescaleDB 2.30.0 → 2.32.1, HA pg18.6-ts2.30.0 → pg18.7-ts2.32.1.** A
matching TSL HA tag exists (pg18.7 only; no pg18.6-ts2.32.1), so HA moved too
and both images now run PostgreSQL 18.7. Upstream's only backward-incompatible
changes (2.31.0) rename debug GUCs we don't use. Two notes that matter: 2.32.0
runs the bgw scheduler as the database owner instead of superuser, and 2.32.1
fixes "every job run by the scheduler fails" on PG 18.7 for older TimescaleDB
versions. Reproduced: on the 2.32.1 image with the volume's extension still at
2.30.0, a scheduler-run compression job failed ("cannot drop portal "" while it
is being set up"); after migrate's update it succeeded. So on an existing volume
every policy job fails from the image switch until migrate has run — the
extension update is the fix, not the loader/detect restart (that one is for
client sessions holding the old library). The image ships a direct
2.30.0→2.32.1 update script. Rehearsed with `docker run` on a throwaway volume:
2.30.0 image, migrations, 720 events, 22 compressed chunks; switched to 2.32.1,
migrate updated the extension to 2.32.1; counts intact, cagg refresh, trgm
search, recompress and the compression policy job worked. Not yet rehearsed on
a demo-seeded compose volume or the HA cluster; container checks ran on arm64.

**Vector 0.58 → 0.62, and Vector configs were never interpolated.** Since 0.57,
`${VAR}` in a config file stays a literal string unless
`VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION` is set (verified on 0.58 and
0.62 with a one-field config). Neither compose Vector service set it, so both
have run with endpoint `"${CAIB_PG_URL}"` since the 0.57 pin — unnoticed because
both modules are credential-gated, 0.58's `validate` accepted the literal, and
the e2e tests write a concrete endpoint. 0.62 validates the URI ("invalid uri
character") and broke `test_gcp_config_validates`, which is how it surfaced.
Both services now set the variable; 0.62 parses config before substituting and
substitutes into string values only, so the structural-injection risk the name
warns about is gone. `test_compose_env` asserts every `timberio/vector` service
carries it (fails without). The 0.59–0.62 upgrade guides and release notes have
no other change touching our components; VRL 0.36's stricter array typing still
compiles our `join(...) ?? null`.

**Grafana 13.3.1** — one breaking change (`isDefault` dropped from an API list
type); file-provisioned datasource and all six dashboards verified in a
throwaway container. **etcd 3.7.4** — patch releases, base image now Debian 13,
still runs as root, our command line works. **uv 0.13.10** — all three Python
images build against the unchanged `uv.lock` with `--frozen`.

## 2026-11-26 — HA failover test asserts detection continuity

An external reviewer made "validate detection continuity during database
failover" a P0 before public release: `make ha-failover-test` proved Patroni
promotion and zero landing loss, but nothing about detect.

The test now runs a probe lander (`scripts/ha_detect_probe.py`) whose
envelopes fire `aws_cloudtrail.root_console_login` (chosen for its 60s
interval; the 300s rules would push the bound to ~7 min). Key A streams at
1/s from before the kill until after promotion, so events still inside the
60s safety lag at kill time are covered. A new key B lands after promotion.
Asserted: alert A before the kill; alert B within 240s of landing (60s lag +
60s interval + 15s tick + a detect restart); per key, alert event_count ==
events in caib.events == lander commits (higher = a re-run window
double-counted, lower = lost); exactly one 'new' outbox row per alert;
loader and detect heartbeats stamped after promotion on the new primary's
clock.

Measured (arm64 laptop, graceful `docker stop` of the leader, 2 runs):
promotion 2.1s, writes resume 11.7–13.3s, first post-failover alert
130.0/130.5s after the stop (118.0/116.9s after landing — the safety lag
plus rule interval, as designed). Detect crashed on the broken connection
and compose restarted it once per run: the conn.closed/conn.broken re-raise
doing its job. Negative control: `compose stop detect` at the kill →
`FAIL: DETECTION GAP — no alert for post-failover key B 240s after it
landed`. A hard kill (TTL 30s) is not measured.

Found on the way: `ha_inserter.py` recomputed event_time on every retry of
the same seq; the loader's conflict key is (event_time, dedup_hash), so an
ambiguous COMMIT retried across a second boundary would have become a
duplicate. Fixed (time fixed per seq). The EXIT trap now restarts the
stopped node so a failed run doesn't leave a two-node cluster.

## 2026-11-26 — raw-event and cagg retention are env knobs, applied by migrate

External review P1: audit/compliance deployments need 1–7 years of raw events, or
never-drop. The spec says retention is "configurable post-install via
remove_*_policy/add_*_policy — document in README, don't parameterize migrations".
That still holds: no migration changed. `migrate` gained a post-migration sync
step (`caib_core.events_retention`), like the role/grant syncs, driven by
`CAIB_EVENTS_RETENTION_DAYS` and `CAIB_CAGG_RETENTION_DAYS` (0 = never drop).

**Unset means "leave the database alone", not "reset to 90".** The old DEPLOY.md
told operators to change retention by hand; a default of 90 re-applied on every
`make up` would have cut a hand-raised policy back and dropped the data. Fresh
installs still get 90/400 from 0002/0004.

**Validated before anything changes**: integers 0..36500; raw >= 7 days (the
uncompressed hot tier that compression, detection and the raw dashboard panels
read); caggs not shorter than raw, because dashboards read the caggs for ranges
wider than ~7 days and would show nothing over stored raw data. A bad value
fails migrate before any migration runs.

**Caggs follow raw upwards** when the events knob is set and the cagg knob is not.
Raising retention deletes nothing; an explicit shorter cagg value is refused instead.

Applied with `alter_job` (keeps job id, history and the `scheduled` flag),
remove for 0, add where none exists; one transaction; same values are a no-op.
Found while testing: the jobs view names a cagg's job by the view, not the
materialization hypertable; and a policy re-added by hand as postgres is still
owned by the hypertable owner (caib_app).

**The agent API's lookback ceiling follows the live policy.** query.MAX_LOOKBACK
(90 days, "matching the retention policy") became wrong the moment retention was
configurable: a 7-year install's agents still could not see past 90 days. The
query core now reads caib.events' drop_after from timescaledb_information.jobs on
every event query (one catalog row; a cache would clamp to a stale value after a
migrate) and reports it as window.max_lookback_days. No policy means no ceiling;
what bounds a huge scan is what already did, the role's 15s statement_timeout
and the LIMIT caps. No new cap was invented. caib_agent_ro reads that view
through TimescaleDB's own PUBLIC grant on timescaledb_information (job configs,
no event data, already readable by every role), so 0008's grant list is unchanged.

## 2026-11-26 — Sigma provenance in rule comments

The spec asked rules to "note the Sigma ID in a comment". The five IDs written in
Phase 4 matched nothing in SigmaHQ/sigma (checked at 876eb7b), and comparing logic
showed none of our rules was derived from a Sigma rule: sg_open_ingress,
iam_privileged_grant and sa_key_created have no upstream equivalent;
root_console_login (ConsoleLogin only) and trail_tampering (PutEventSelectors
instead of UpdateTrail) are related to 8ad1600d / 4db60cc0 but not conversions.
Rule comments now say what each rule is; related upstream rules are named with
real ID, title, author and DRL 1.1 link. Going forward: cite only IDs that exist
upstream, with DRL attribution if the rule is based on one. Open: the Sigma->SQL
converter emits only the Sigma ID; DRL 1.1 also wants author, link and licence
kept in shared converted rules (and the author in match messages).
