# CLAUDE.md — project memory / handover

CAIB (Cloud Audit In a Box): self-hostable cloud/SaaS audit-log SIEM on
Vector + PostgreSQL 18/TimescaleDB (TSL) + Grafana, Docker Compose. Built
2026-08-24/25 by Claude Code working phase-by-phase from `caib-build-spec.md`
(rev 4 — read it before changing anything architectural; it contains locked
decisions and working agreements that still bind).

## State: engineering complete, dogfooding not started

Spec phases 0–8 are done, tested, and pushed (`github.com/matijazezelj/caib`,
main). Phase 9 (Iceberg archive tier) is deliberately unbuilt. Two external
review rounds passed; verdict: "shippable, put real logs into it." 70 tests,
CI green, Apache-2.0.

Read in this order: `README.md` (what exists + measured numbers) →
`DECISIONS.md` (every deviation from spec, append-only — **add entries, never
edit old ones**) → `docs/NEW_MODULE_HOWTO.md` (how modules work + recipes for
the unbuilt ones) → `docs/DEPLOY.md` (what changes on a real host) → module
`API_NOTES.md` files (verify-live checklists).

## The todo list (task trackers don't transfer between accounts)

Ranked; 1–4 are credential-gated. #5–#7 are done (PRs #4, #5, #16, and the
2026-09-16 agent-layer fix round) — details in DECISIONS.md and CHANGELOG.md:

1. **Live-verify cloudflare_audit** — needs a CF API token in `.env`; run
   `caib-poller --module cloudflare_audit --once`, work the checklist in
   `modules/cloudflare_audit/API_NOTES.md`, replace SYNTHETIC fixtures with
   sanitized real responses. Closes the last open Phase 2 AC.
2. **Live-verify gcp_audit / azure_activity / aws_cloudtrail** — cloud-side
   setup is copy-paste CLI in each module README. GCP first: the
   `serviceData` vs `metadata` IAM payload question affects a shipped rule.
3. **Dogfood ~2 weeks on a real VM** — use it as an analyst; harvest missing
   attrs, noisy rules, wrong severities into DECISIONS/API_NOTES; do one real
   pgBackRest restore drill. Point a real agent at the agent layer
   (`/mcp`) while doing it — nothing there has met real data yet — and decide
   whether ack/close writes belong in v2.
4. **Build slack_audit / zoom / knowbe4** from the HOWTO recipes — needs
   Enterprise Grid token, Zoom S2S app, KnowBe4 key.
5. ~~Agent query layer~~ — done (`docs/AGENT_API.md`).
6. ~~Reliability/ops backlog~~ — done.
7. ~~Supply chain / CI~~ — done. Left: repo still private, no signing key for
   tagged releases.
8. Optional: Phase 9 archive tier (pg_lake spike first, spec AC at the
   bottom of the spec); a consulting blog post from the bench + DECISIONS
   findings.

## How to work here (habits the user approved)

- Work phase/feature at a time; **commit at milestones and push**; watch CI
  (`gh run watch`) until green before declaring done. Commits end with
  `Co-Authored-By: Claude <the model's noreply>`.
- Verify empirically, record honestly: every vendor-API assumption goes in
  `API_NOTES.md` with a verify-live checklist; every deviation or finding goes
  in `DECISIONS.md`. Fixtures built from docs (not live capture) are marked
  SYNTHETIC.
- The user reviews via external reviewers and pastes their feedback; treat it
  as actionable, but check claims against the repo first (one reviewer wrongly
  said the bench never ran).
- Screenshots in `docs/img/` are part of the README; regenerate after UI
  changes via the `compose.render.yml` overlay (grafana-image-renderer
  sidecar), then remove the renderer. Reviewers consider the dashboards
  frozen — don't add panels without cause.

## Environment facts

- Python 3.12 target via `uv` (workspace: root + `core/` + `pollers/`).
  `uv run pytest -q` / `uv run ruff check .` — ruff is strict; run it before
  every commit. Tests need Docker (throwaway `timescale/timescaledb:2.29.2-pg18`
  container per session; CI uses a service container).
- `make up` auto-creates `.env` with **generated** passwords
  (`scripts/init_env.sh`); `scripts/preflight.sh` then refuses to start if the
  example passwords meet a non-loopback `CAIB_BIND_ADDR` (both ports are
  loopback-bound by default).
  Stack: `make up` / `make demo` (no creds, seeds all 4 modules, fires alerts)
  / `make down`. Volumes persist across down/up.
- Release confidence: `make test`, `make bench`, `make ha-failover-test`
  (needs `make ha-up` first), `make restore-test`.
- Images pinned: timescaledb 2.29.2-pg18 (TSL — never the `-oss` builds, CI
  asserts the license), vector 0.57.0-debian, grafana 12.4.9, HA nodes
  `timescale/timescaledb-ha:pg18.4-ts2.29.2` (ships Patroni + pgBackRest +
  pg_textsearch). pg19 has no TimescaleDB build yet.

## Gotchas that cost real time (details in DECISIONS.md / HOWTO)

- **The detect loop swallows `psycopg.Error` on purpose** (a broken rule must
  not kill the engine) — which is why it now re-raises on
  `conn.closed or conn.broken`. Keep that check in any new handler there, or
  detect goes back to failing open on a failover.
- **Both loops stamp `ops.heartbeat` every cycle**; compose healthchecks run
  `caib-core healthcheck --service X --max-age 120`. A healthcheck does not
  restart or page anything — external monitoring still owns liveness.
- **Spilo is a trap**: published images ship Apache-only TimescaleDB. HA uses
  timescaledb-ha instead; service names stay `spilo1..3` to match the spec's
  HAProxy config.
- **VRL**: `if` is invalid as an object-literal value; a remap outputs `.`
  (assign it!); `??` on infallible expressions is a compile error; full list
  in the HOWTO.
- **Never `${VAR:?}` for a variable only an opt-in (profiled) service uses**:
  compose interpolates every service even with its profile off, so it breaks
  `make up` for every existing `.env`. Put the check in the service's startup.
  `tests/test_compose_env.py` guards it.
- **Before starting a todo, `git fetch` and read the log**: a stale checkout
  once rebuilt an already-merged feature from an out-of-date todo list.
- **Rule SQL goes through psycopg named params**: literal `%` must be `%%`.
  Detect tests use `Cfg(safety_lag_s=0)` because freshly-loaded events sit
  inside the safety lag otherwise.
- **Sigma `not` vs SQL three-valued logic**: negated conditions are wrapped
  `NOT coalesce(..., false)` in `caib_core.sigma` — missing field must pass.
- **`host.docker.internal` can't reach loopback-bound host ports on Linux
  CI** — container-to-container tests share a docker network instead.
- **Grafana provisioning reloads dashboards from disk (~10s)** — no restart
  needed; datasource/env changes DO need a grafana recreate.
- **Bench numbers**: quote them decomposed — 9.3x is hot-footprint-vs-cold;
  pure data compression is ~6x (indexes don't exist on compressed chunks);
  14.7k ev/s is WITH the trgm index (31.2k without; that run is in the git
  history of `bench/results/`). Single-run ±20–30% variance.
- Patroni REST: `GET /` and `/primary` return 200 only on the leader —
  replicas 503 by design; don't `curl -f` a replica and call it down.
- `migrate` runner: migrations run per-file transactions under
  `SET LOCAL ROLE caib_app`; files with first-line marker
  `-- caib:no-transaction` run statement-by-statement autocommit and must be
  idempotent (0004 caggs). Migrations are append-only — never edit applied ones.

## Open questions still owned by the user

- Vendor credentials (everything in todo 1–4).
- Whether to standardize dev on timescaledb-ha for dev/prod parity + BM25
  (considered-and-deferred entry in DECISIONS.md has the triggers).
