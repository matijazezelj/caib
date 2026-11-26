# Changelog

Notable changes, newest first. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions will follow
[semver](https://semver.org/spec/v2.0.0.html) once there is a version.

The reasoning behind a change lives in [DECISIONS.md](DECISIONS.md) — this file
is the index, not the record.

## [Unreleased]

Everything so far. Phases 0–8 of `caib-build-spec.md` are the initial build and
are not itemised here; what follows is what changed after it, by pull request.

### HA failover: detection continuity

- `make ha-failover-test` now asserts detection keeps working across a
  failover: an alert before the kill, a new alert within 240s after
  promotion, exact alert event_count vs events (nothing lost or
  double-counted), one notification per alert, and loader/detect heartbeats
  advancing on the new primary. Measured: first post-failover alert ~130s
  after the primary stop.
- `ha_inserter.py`: event_time fixed per sequence number across retries, so
  a retried ambiguous commit can't become a duplicate event.
- A failed failover run restarts the stopped node on exit.

### Model-selection guidance

- `docs/AGENT_API.md`: which Claude model and effort to point at the agent
  layer, per job (unmeasured starting points).
- MCP: `get_alerts` / `top_actors` descriptions route to the follow-up tool;
  `list_rules` reports a missing rules directory legibly.
- `.claude/agents/`: `check-runner` (Haiku) and `module-reviewer` (Sonnet).

### TimescaleDB 2.30.0

- `timescale/timescaledb` 2.30.0-pg18 everywhere the single-node image is named
  (supersedes #17 and the backup-image part of #29).
- `migrate` now runs `ALTER EXTENSION timescaledb UPDATE` in a fresh session on
  every start. Before, an existing database stayed on its old extension version
  after an image bump. **Upgrading:** `make up`, then
  `docker compose restart loader detect` (and agent-api/pollers) — see
  `docs/DEPLOY.md` §8.

### Cleanup

- Removed dead code: `caib_core/db.py` (never imported since Phase 0), an
  unreachable "not implemented" branch in the `caib-core` CLI, an unused
  counter in `scripts/demo_seed.py`; committed `.DS_Store` files and `.gitkeep`
  placeholders in directories that have content.
- README: corrected stale facts (test count, entrypoints, what binds to
  loopback, which services read through HAProxy `:5001`, search latency,
  Slack not being a shipped module) and moved the dev/test note into
  Quickstart.

### Python 3.14

- All three images on `python:3.14-slim` (supersedes #31). `.python-version`
  pins dev and CI to 3.14 so the tests run on the interpreter the images ship;
  code stays compatible with 3.12.

### HA overlay

- `coreos/etcd` v3.7.1 (#9), `haproxy` 3.4-alpine (#8),
  `timescale/timescaledb-ha` pg18.6-ts2.30.0. `make ha-failover-test` passes on
  the upgraded cluster, before and after the extension update.
- Fixed: `make ha-up` over an existing cluster could fail in migrate with
  "read-only transaction" — HAProxy routed :5000 to replicas for ~9s after
  starting. Servers now start `init-state down`.

### Grafana 13.2.2

- `grafana/grafana` 13.2.2 (supersedes #10, #28). Verified on a running demo
  stack: provisioning, datasource and all four README dashboards render; the
  screenshots are re-rendered on 13.2.2.
- `compose.render.yml` passes a renderer token (`CAIB_RENDERER_TOKEN`), which
  Grafana 13 requires to render.

### Dependency batch (2026-09-16)

- Python: `mcp` 2.2.0, `psycopg`/`psycopg-binary` 3.3.5 (and the `>=3.3.5`
  floors), `ruff` 0.16.8, `boto3` 1.43.96. Supersedes #22, #23, #25.
- `ghcr.io/astral-sh/uv` 0.12.15 in all three Dockerfiles (#18–#20);
  `astral-sh/setup-uv` v10.1.0 in CI (#24).
- `timberio/vector` 0.58.0 in compose **and** in the Vector tests, which is what
  actually exercises it (#7).

### Dependency PR hygiene

- Test fixture pauses TimescaleDB policy jobs on each test database, removing
  intermittent `LockNotAvailable`/`DeadlockDetected` CI failures.
- `scripts/check_image_pins.sh` also checks image constants in `tests/` and
  `scripts/`, and requires the Vector pins to agree — a compose-only image bump
  can no longer pass CI while the tests run the old image.
- Dependabot groups Dockerfile base-image bumps across directories into one PR.

### Agent query layer: review fixes and MCP over HTTP

- MCP over Streamable HTTP at `/mcp` on the `agent-api` service, behind the
  same bearer token: an agent needs only the token, not the `caib_agent_ro`
  password. Stateless, JSON responses. stdio (`caib-agent-mcp`) is unchanged.
- New `CAIB_AGENT_ALLOWED_ORIGINS`: any request carrying an `Origin` header
  not listed there gets 403, on `/v1` and `/mcp` (DNS-rebinding guard).
- Fixed: `make up` failed for every `.env` created before the agent layer —
  compose required `CAIB_AGENT_API_TOKEN` with `:?`, which compose evaluates
  even when the `agent-api` profile is off. The service's own startup check
  still refuses an empty token.
- Fixed: the `agent-api` service could never authenticate on a compose stack —
  `migrate` was not given `CAIB_AGENT_RO_PASSWORD`, so `caib_agent_ro` was
  created without a password. Existing stacks: re-run `make migrate`.
- Fixed: `GET /v1/actors/{actor}` returned 404 for any actor containing `/`,
  i.e. every AWS IAM ARN.
- Fixed: `get_alerts` and `alert_summary` hid open and acked alerts whose
  rule last matched more than 7 days ago (and could not reach them at all past
  90 days), so "what is open?" could answer "nothing". Unresolved alerts now
  ignore the default window; an explicit `since` applies to every status and
  is no longer clamped for alerts.
- Fixed: `truncated` was true whenever a page was exactly full, even with no
  more rows. It now means more rows exist.

### Agent query layer

- New optional read-only query surface over the audit log, compose profile
  `agent-api`: an HTTP/JSON API (`caib-agent-api`) and an MCP stdio server
  (`caib-agent-mcp`), both over one shared query core (`caib_core.query`).
  Seven questions: `search_events`, `get_alerts`, `alert_summary`,
  `top_actors`, `actor_activity`, `module_health`, `list_rules`.
- New database role `caib_agent_ro` (migration 0008): `SELECT` on five named
  tables and nothing else — no `ingest.*`, no `detect.outbox` — plus
  `statement_timeout=15s` and `default_transaction_read_only=on`. It is a
  different, much narrower role than Grafana's `caib_ro`.
- Read-only v1: there is no endpoint or tool that acknowledges, closes or
  creates anything, and no write privilege behind them if there were.
- New workspace member `agentapi` and a third image, `caib-agent-api`, added
  to dependabot and the CI build job.
- Two new generated secrets in `.env`: `CAIB_AGENT_RO_PASSWORD` and
  `CAIB_AGENT_API_TOKEN`. The API refuses to start without the latter. An
  existing `.env` needs both added by hand — see `docs/AGENT_API.md`.

### Supply chain and CI

- Bumped `actions/checkout` to v7.0.1 and `astral-sh/setup-uv` to v10.0.1, off
  the deprecated Node 20 runtimes, and pinned both to full commit SHAs.
- Added `.github/dependabot.yml`: weekly `github-actions`, `docker` (the three
  Dockerfile directories), `docker-compose` (the six stack image pins) and `uv`
  (the workspace lock, minor and patch grouped).
- Added `scripts/check_image_pins.sh`, which inventories every image reference
  and fails on an untagged/`:latest` image or on the copies of the
  `timescale/timescaledb` pin disagreeing — neither is something dependabot
  catches.
- CI now has a `build` job: `docker build` for all three images (no push) and
  `docker compose config -q` for the base file and each overlay, with all
  profiles enabled. `test` now waits on `lint`.
- Both Python images build from `uv.lock` (`uv sync --frozen --no-dev
  --no-editable --package …`) instead of `pip install .`, so a container ships
  the dependencies the lockfile pins rather than whatever pip resolved that
  day. Both now run as `caib`, uid 10001.
- Added `SECURITY.md`.

### Reliability and ops (#4)

- `detect.outbox`, `detect.alerts` and `ingest.dead_letter` are swept on a
  retention schedule; nothing bounded them before.
- `CAIB_NOTIFY_FORMAT` is actually passed to the detect service in compose. It
  was documented but never wired.
- A bad `CAIB_NOTIFY_MIN_SEVERITY` is rejected at startup rather than at the
  first alert.
- A YAML error in a rule file no longer kills the detect process on SIGHUP.
- A permanently-flooding rule re-notifies the overflow meta-alert on an
  interval instead of once and never again.
- `notify.drain` POSTs the webhook outside the outbox transaction.

### Tests (#3)

- Coverage for the rule and envelope guardrails and for transport retry.

### Docs (#2, #1)

- README says how CAIB compares to the alternatives, and that HA is optional.
- `scripts/preflight.sh` says so when it cannot check for shipped passwords
  instead of passing silently.

[Unreleased]: https://github.com/matijazezelj/caib/commits/main
