# Agent query layer

A read-only query surface over the CAIB audit log, for LLM agents and for
anything else that wants JSON instead of a dashboard. Two transports —
an HTTP/JSON API and an MCP stdio server — over one shared query core
(`caib_core.query`), so both answer identically and both refuse identically.

Compose profile `agent-api`. Off unless you ask for it.

```sh
docker compose --profile agent-api up -d
curl -sH "Authorization: Bearer $CAIB_AGENT_API_TOKEN" \
  'http://127.0.0.1:8088/v1/alerts/summary' | jq
```

## Read-only by design — what it deliberately cannot see

The read-only property is not a promise made by the code in front of the
database. It is a database role, `caib_agent_ro`, and the code cannot exceed
it. Migration `0008_caib_agent_ro_role.sql` grants it `SELECT` on exactly five
tables and nothing else:

| Table | Why |
| --- | --- |
| `caib.events` | the normalised audit events themselves |
| `caib.events_5m` | 5-minute rollup, for `module_health` |
| `caib.actors_1h` | hourly per-actor rollup, for `top_actors` |
| `detect.alerts` | alerts the detection rules have raised |
| `ops.heartbeat` | engine liveness beacons |

Plus `USAGE` on schemas `caib`, `detect` and `ops`, and `CONNECT` on the
database. That is the whole grant.

Not granted, each for a reason:

- **`ingest.landing`, `ingest.dead_letter`** — raw vendor envelopes, exactly as
  the vendor sent them and sanitised by nobody. The dead-letter table is by
  definition the payloads whose shape we got wrong. There is no `USAGE` on
  schema `ingest` at all, so these are not even nameable by the role.
- **`ingest.cursors`** — poller state, which for some vendors includes API
  pagination tokens.
- **`detect.outbox`** — notification payloads and delivery state.
- **`detect.rule_state`** — per-rule watermarks; engine internals.
- **anything added later** — there is no `ALTER DEFAULT PRIVILEGES ... GRANT`
  for this role, so a table created by a future migration does not become
  agent-readable by existing. Widening the surface means adding a line to a
  migration, and `tests/test_query_core.py` asserts the exact grant set and
  fails when it drifts.

Note one thing the grant list understates: granting `SELECT` on a continuous
aggregate makes TimescaleDB grant it on that aggregate's own internal
materialisation views in `_timescaledb_internal` too. Those hold the same
counts the aggregate does, derived from `caib.events`, which is already
readable — no additional data is exposed, but it is why
`information_schema.table_privileges` lists more than five rows for the role.

There is also no write path of any kind. v1 has no tool or endpoint to
acknowledge, close, silence or create an alert. That is deliberate: the alert
lifecycle has not survived contact with a real dogfooding round yet, and a
write API designed before then would encode guesses. Adding one later is a
migration plus a grant, not a flag.

Three more limits, enforced in the role so they hold for anything holding its
credentials — `psql` included, not just the services here:

```
statement_timeout                   = 15s
idle_in_transaction_session_timeout = 30s
default_transaction_read_only       = on
```

And three enforced in `caib_core.query`, so both transports get them:

- a hard `LIMIT` cap per function, applied regardless of what the caller asks
  for (`search_events` 500, `get_alerts` 500, `actor_activity` 500,
  `top_actors` 100);
- a 90-day maximum lookback, matching the `caib.events` retention policy;
- `raw` — the full original vendor event — excluded from `search_events` unless
  explicitly requested, and never returned by `actor_activity` at all.

Every response reports the window it actually used and whether the result was
truncated, so a clamped window or a capped page is visible to the caller rather
than silently changing the answer.

## Setup

`make up` generates both new secrets into `.env`:

```
CAIB_AGENT_RO_PASSWORD   # the caib_agent_ro role's password
CAIB_AGENT_API_TOKEN     # the HTTP API's bearer token
```

**Upgrading an existing deployment:** `scripts/init_env.sh` never overwrites an
existing `.env`, so add both by hand — any long random value — then re-run
`make migrate` so `migrate` creates the role and syncs its password. The
`agent-api` service will not start without them.

`CAIB_AGENT_RO_PASSWORD` is *not* `CAIB_PG_RO_PASSWORD`. Those are two different
roles: `caib_ro` is Grafana's datasource and can read every table in the
database, including the raw `ingest.*` tables the Overview dashboard's panels
need. Pointing the agent layer at `caib_ro` would give away everything this
document says it cannot see.

## Exposure

The published port binds to `CAIB_BIND_ADDR` (`127.0.0.1` by default), the same
as Postgres and Grafana. The compose service sets `CAIB_AGENT_API_BIND=0.0.0.0`
inside the container, which is the container's own network namespace and not
exposure — the port mapping is what decides who can reach it. Running
`caib-agent-api` outside a container binds `127.0.0.1` instead.

The API speaks plain HTTP and does not terminate TLS. Anything off-host goes
behind the same reverse proxy as Grafana — see `docs/DEPLOY.md`.

## HTTP endpoints

Every `/v1/*` route requires `Authorization: Bearer $CAIB_AGENT_API_TOKEN`. The
service refuses to start if that variable is empty or unset; there is no
unauthenticated mode.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v1/events` | `q`, `module`, `actor`, `action`, `outcome`, `severity`, `since`, `until`, `limit`, `include_raw` |
| GET | `/v1/alerts` | `status`, `severity`, `rule_id`, `since`, `limit` |
| GET | `/v1/alerts/summary` | `since` |
| GET | `/v1/actors/top` | `since`, `module`, `limit` |
| GET | `/v1/actors/{actor}` | `since`, `until`, `limit` |
| GET | `/v1/health/modules` | — |
| GET | `/v1/rules` | — |
| GET | `/healthz` | liveness, no database, no auth |
| GET | `/readyz` | database round-trip, no auth |

`/healthz` and `/readyz` are unauthenticated on purpose: `healthz` answers from
process state alone, `readyz` reports reachability and nothing about the data,
and making the container healthcheck carry the bearer token would put it in
`docker inspect` output for no gain.

Errors come back as `{"error": "<slug>", "detail": "<prose>"}` and never carry a
traceback — those go to the service log. `bad_request` (400) means the request
was wrong; `unauthorized` (401); `query_timeout` and `database_unavailable`
(503) are both worth retrying; `internal_error` (500) is a bug here.

Interactive schema: `http://127.0.0.1:8088/docs`.

## MCP tools

Same seven functions, same core, same caps. `caib-agent-mcp` speaks MCP over
stdio and connects to the database directly — not through the HTTP API — so
there is no bearer token involved: stdio has exactly one client, the process
that spawned it. Tools only; no sampling, no prompts, no resources.

| Tool | For |
| --- | --- |
| `search_events` | general-purpose event search; free text plus exact filters |
| `get_alerts` | alerts the rules raised, newest `last_seen` first |
| `alert_summary` | counts by status and severity — "how bad is it right now" |
| `top_actors` | busiest actors and their failure counts |
| `actor_activity` | one actor's timeline plus a per-action rollup |
| `module_health` | per-module ingest lag and volume; engine heartbeats |
| `list_rules` | the loaded detection rules |

### Pointing an MCP client at it

The server needs `CAIB_RO_PG_URL` and a readable rules directory. Against a
running compose stack, from the repo:

```jsonc
{
  "mcpServers": {
    "caib": {
      "command": "docker",
      "args": [
        "compose", "-f", "/path/to/caib/docker-compose.yml",
        "run", "--rm", "--no-deps", "-i",
        "--entrypoint", "caib-agent-mcp", "agent-api"
      ]
    }
  }
}
```

Or without Docker, from a checkout with the workspace synced:

```jsonc
{
  "mcpServers": {
    "caib": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/caib", "caib-agent-mcp"],
      "env": {
        "CAIB_RO_PG_URL": "postgresql://caib_agent_ro:PASSWORD@127.0.0.1:5432/caib",
        "CAIB_RULES_DIR": "/path/to/caib/detect/rules"
      }
    }
  }
}
```

Logs go to stderr, because stdout is the transport.

## Semantics worth knowing before you trust an answer

- **Times are UTC** everywhere, in and out. Naive timestamps are read as UTC.
- **`q` is a substring match**, case-insensitive, against a per-event index of
  actor, action, target, source IP, outcome and attribute values — the same
  `search_text` column the Search dashboard uses. `%` and `_` are LIKE
  wildcards, as they are in the dashboard's textbox.
- **`actor` and `action` are exact matches**, not substrings. Use `q` for
  partial ones.
- **An out-of-enum filter is rejected**, not answered with zero rows.
  `severity=warn` returns 400 rather than an empty list, because an empty
  result reads as "nothing happened", which is the worst possible wrong answer
  about an audit log.
- **`get_alerts` filters on `last_seen`**, so an alert that first fired months
  ago but is still firing appears in a short window. An alert row folds every
  recurrence of the same thing, so read `event_count`, `first_seen` and
  `last_seen` together rather than counting rows.
- **`top_actors` reads the hourly rollup only when the window starts on an hour
  boundary**, and counts raw events otherwise; a bucket straddling the boundary
  would over- or under-report. The response says which source ran.
- **`module_health` heartbeats are per engine loop** (`loader`, `detect`), not
  per module — `ops.heartbeat` is keyed by service. A module going quiet shows
  up as its `lag_seconds` growing. The landing-backlog, dead-letter and
  Timescale-job-error stats from the same Overview dashboard row are absent
  because `caib_agent_ro` cannot read those tables; that is an operator
  question, and the operator has Grafana.
- **`list_rules` reads the YAML**, through the same `caib_core.detect.load_rules`
  the engine calls, so it cannot describe a rule set the engine is not running.
  The rule SQL is not returned.
