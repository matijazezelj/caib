# caib — Cloud Audit In a Box

A self-hostable cloud/SaaS audit-log SIEM on boring infrastructure:
**Vector + PostgreSQL/TimescaleDB + Grafana**, packaged with Docker Compose.
Each log source is a module that ships everything end-to-end: collector config,
field mapping, migrations, a provisioned dashboard, seed detection rules, a
dead-man rule, and test fixtures.

Full design: [`caib-build-spec.md`](caib-build-spec.md). Deviations and
empirical findings: [`DECISIONS.md`](DECISIONS.md).

## Quickstart (dev)

Requires Docker with the Compose plugin.

```sh
make up        # creates .env from .env.example on first run, builds, starts
# Grafana: http://localhost:3000 (admin / value of CAIB_GRAFANA_ADMIN_PASSWORD)
make logs      # follow everything
make down      # stop
```

Development also wants [`uv`](https://docs.astral.sh/uv/):

```sh
make test      # ruff + pytest (pytest starts a throwaway timescaledb container)
```

## Stack

| Component | Image | Notes |
|---|---|---|
| Database | `timescale/timescaledb:2.29.2-pg18` | TSL build required (Apache-only build lacks compression/retention/caggs) |
| Collector | `timberio/vector:0.57.0-debian` | push sources (S3/SQS, Pub/Sub) |
| Dashboards | `grafana/grafana:12.4.9` | fully provisioned, read-only DB role |
| Core services | `caib-core` (built here) | `migrate` / `loader` / `detect` entrypoints |
| Pollers | `caib-poller` (built here) | one image, `CAIB_MODULE` selects the module |

## Modules

Enable a module by starting compose with its profile, e.g.
`docker compose --profile cloudflare_audit up -d`.

| Module | Type | Status |
|---|---|---|
| `aws_cloudtrail` | push (S3 → SQS → Vector) | planned (Phase 3) |
| `gcp_audit` | push (Pub/Sub → Vector) | planned (Phase 6) |
| `cloudflare_audit` | poller | planned (Phase 2) |
| `slack_audit` | poller | planned (Phase 6) |
| `zoom` | poller | planned (Phase 6) |
| `knowbe4` | poller | planned (Phase 6) |

## Retention defaults

Events: compressed after 7 days, dropped after 90 days. Continuous aggregates
and alerts are kept much longer (cheap). Change post-install with
`remove_retention_policy` / `add_retention_policy` — do not edit migrations.

## Licensing

TimescaleDB Community Edition ships under the TSL: free to self-host and run in
production; the restriction is reselling it as a managed database service.
CAIB deployments are self-hosted, which is squarely permitted. CI asserts the
running build is the TSL one.
