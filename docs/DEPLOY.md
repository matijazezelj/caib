# Deploying CAIB on a real host

`make up` gets you a working stack. This is what changes when the host is not
your laptop. Backups are separate: [BACKUPS.md](BACKUPS.md).

Nothing here is exotic. It is the short list of things that are wrong by
default in every "runs on Docker Compose" project, written down.

## 1. Exposure

**Docker port publishing bypasses your firewall.** A published port is a rule
in Docker's own iptables chain (`DOCKER-USER`), evaluated before ufw and
firewalld. A ufw `deny` on 3000 does not stop `ports: "3000:3000"`.

CAIB therefore binds to loopback:

```sh
CAIB_BIND_ADDR=127.0.0.1   # default; both Postgres and Grafana
```

Keep it. Reach Grafana over an SSH tunnel (`ssh -L 3000:127.0.0.1:3000 host`)
or, for a team, put a TLS reverse proxy in front of it. Do **not** set
`CAIB_BIND_ADDR=0.0.0.0` to "make it reachable" — that publishes the Postgres
superuser port to the internet as well.

If you do need a routable bind (a private VLAN, say), set a real address, never
`0.0.0.0`, and confirm with `docker compose ps` that only the port you meant is
listed.

## 2. Secrets

`make up` generates `.env` with random passwords on first run
(`scripts/init_env.sh`). `scripts/preflight.sh` then refuses to start a stack
that still carries the example passwords from this repository on a non-loopback
address (`CAIB_ALLOW_DEFAULT_SECRETS=1` overrides, for labs).

What that does *not* solve, and you should know:

- **Secrets are container environment variables.** Anyone in the `docker` group
  reads them with `docker inspect`; they appear in `/proc/<pid>/environ`. The
  database passwords are inside the connection URLs, so a libpq connection
  failure can log them.
- `chmod 600 .env`, and back it up somewhere other than the host it protects.
- Vendor API tokens (`CAIB_CLOUDFLARE_API_TOKEN`, `CAIB_AZURE_CLIENT_SECRET`,
  `AWS_SECRET_ACCESS_KEY`) live in the same file. Scope them read-only at the
  vendor — every module README says exactly which permission it needs.
- Rotating a database password is `ALTER ROLE` + `.env` + recreate: the
  `migrate` entrypoint syncs role passwords from env on every run.

## 3. TLS to the database

`CAIB_PG_SSLMODE` (default `prefer`) applies to every connection, app and
Grafana alike. `prefer` encrypts when the server offers TLS and falls back
silently when it does not — **it verifies nothing**, so it is protection
against a passive listener only, and the stock `timescale/timescaledb` image
does not enable TLS at all.

Single-node compose, everything on one host over the docker bridge: `prefer` is
fine.

Anything where the database is on a different host from the loader/detect
containers — including the HA overlay promoted to real hosts, where the app →
HAProxy → Postgres hop crosses the network — wants:

1. Server certificates on the database nodes (`ssl = on`, `ssl_cert_file`,
   `ssl_key_file`; on the HA overlay set them in `ha/patroni.yml` under
   `bootstrap.dcs.postgresql.parameters` so they follow the primary).
2. `CAIB_PG_SSLMODE=verify-full` and the CA mounted into the app containers
   (`PGSSLROOTCERT`).
3. `ha/patroni.yml`'s `pg_hba` narrowed from `host all all 0.0.0.0/0` to
   `hostssl` plus the actual node/app subnets. The shipped value is a lab
   default: it is `scram-sha-256`, so it wants a password, but it accepts a
   connection attempt from anywhere that can route to 5432.

## 4. TLS to Grafana

Grafana ships plain HTTP and CAIB does not configure a certificate. Terminate
TLS in front of it (Caddy, nginx, Traefik — any of them), keep Grafana on
loopback, and tell Grafana where it lives so redirects and links are right:

```yaml
# docker-compose.override.yml
services:
  grafana:
    environment:
      GF_SERVER_ROOT_URL: https://caib.example.com/
      GF_SERVER_ENFORCE_DOMAIN: "true"
      GF_USERS_ALLOW_SIGN_UP: "false"
```

Grafana's own auth is the only access control in CAIB — there is no RBAC and no
multi-tenancy (spec non-goal). Anyone who can log in sees every event, and the
raw-event panels render the full vendor payload. Use Grafana's OAuth/LDAP
integration for real teams, and keep the admin account for administration.

## 5. What to watch

CAIB alerts on its own ingest going quiet (a dead-man rule per module) but
cannot alert on the alerting being dead. Point an external monitor at these:

| Signal | Check |
|---|---|
| detect engine alive | `caib-core healthcheck --service detect --max-age 120` (container healthcheck; Overview → *Engine heartbeats*) |
| loader alive | same, `--service loader` |
| container health | `docker compose ps` — a healthcheck marks a container unhealthy, it does **not** restart it or page anyone |
| ingest stalled | Overview → *Ingest lag by module*, and the dead-man alerts |
| mapping bugs | Overview → *Dead letter (total)* — non-zero means envelopes the loader refused |
| Timescale jobs | Overview → *Timescale job errors (24h)* (compression, retention, cagg refresh) |
| WAL archiving | `SELECT last_archived_wal, last_failed_wal FROM pg_stat_archiver` — a growing `last_failed_wal` fills the disk |

Notifications go to `CAIB_NOTIFY_WEBHOOK_URL` (Slack Block Kit is automatic for
`hooks.slack.com`). Note the alert payload embeds the rule's `entity` and
`context`, which come from event fields — assume anything in a detection can
reach that channel.

## 6. Retention

`caib.events` is compressed after 7 days and **dropped after 90**; the
dashboard aggregates (`caib.events_5m`, `caib.actors_1h`) are kept 400 days.
That default is active from the first migration. If you have a compliance
retention floor (1–7 years of raw audit evidence is common), set it before you
need the data, not after — a dropped chunk is gone unless a backup has it.

Set it in `.env` and re-run migrate (`make up` does; so does `make migrate`):

```sh
CAIB_EVENTS_RETENTION_DAYS=2555   # raw events: 7 years. 0 = never drop
#CAIB_CAGG_RETENTION_DAYS=        # aggregates; unset = follow raw upwards
```

| Variable | Unset | Accepted values |
|---|---|---|
| `CAIB_EVENTS_RETENTION_DAYS` | leave the database's policy as it is (90 days on a fresh install) | `0` (never drop) or `7`–`36500` |
| `CAIB_CAGG_RETENTION_DAYS` | leave as is (400 days), except: raised to match `CAIB_EVENTS_RETENTION_DAYS` when that is set longer | `0` or `1`–`36500`, and not shorter than raw events |

How it behaves:

- **Unset never resets anything.** If you changed retention by hand with
  `remove_retention_policy`/`add_retention_policy` (what this section used to
  tell you to do), migrate leaves it alone until you set the variable.
- The same value on every run changes nothing. A new value alters the existing
  Timescale job in place (`alter_job`: same job id and history); `0` removes the
  policy; going from `0` back to a number adds one.
- Nonsense is refused: migrate exits non-zero **before applying any
  migration or changing any policy**:
  non-integers, negatives, more than 100 years, raw retention under 7 days
  (the uncompressed hot tier that compression, detection and the raw dashboard
  panels read), and aggregates kept shorter than raw events — dashboards read
  the aggregates for any range wider than ~7 days, so they would show nothing
  for periods whose raw events are still on disk.
- **Lowering it deletes data**: chunks older than the new value go on the next
  run of the retention job, and migrate logs a warning saying so.
- Retention is a Timescale job — it follows a Patroni failover like the others.

Do not edit the migrations — they are append-only and already applied.

### Disk size

From the committed `make bench` run (1M CloudTrail/GCP/Slack-shaped events,
raw vendor JSON kept, i.e. `CAIB_DROP_RAW=false`; single run, expect ±20–30%,
and your event shape will differ):

- **Hot (first 7 days, uncompressed): ~2.0 GB per 1M events.** That is the
  1.28 GB heap plus 0.71 GB of indexes — 0.47 GB of it the trgm search index,
  ~0.24 GB the b-tree/BRIN ones.
- **Compressed (day 8 onward): ~0.22 GB per 1M events.** That is ~6x pure data
  compression on the heap (1.28 GB → 0.22 GB); the often-quoted 9.3x compares
  it with the whole hot footprint, indexes included — compressed chunks carry
  no b-tree/GIN indexes.
- Aggregates are pre-counted buckets: kilobytes to megabytes, not a sizing
  factor.

So the steady state is roughly `7 × daily × 2.0 GB + (retention − 7) × daily × 0.22 GB`,
with `daily` in millions of events:

| Raw retention | 100k events/day | 1M events/day | 10M events/day |
|---|---|---|---|
| 90 days (default) | ~3 GB | ~32 GB | ~320 GB |
| 1 year | ~9 GB | ~93 GB | ~930 GB |
| 3 years | ~25 GB | ~250 GB | ~2.5 TB |
| 7 years | ~58 GB | ~575 GB | ~5.8 TB |
| `0` (never) | grows ~8 GB/year | ~80 GB/year | ~800 GB/year |

Add your backup repository on top (`docs/BACKUPS.md`: full backups plus WAL),
and plan disk for the target retention on day one: growing a volume later is
an outage on most hosts. `CAIB_DROP_RAW=true` drops the vendor JSON from
`raw` and shrinks both tiers, at the cost of the original record — usually not
what an audit wants. For multi-year retention on object storage instead of
local disk, the spec's Phase 9 archive tier (unbuilt) is the intended path; it
takes over chunk dropping from this policy when enabled.

## 7. Known rough edges

Honest list, so nothing here is a surprise in an audit:

- Image pins are bumped by dependabot, but copies it cannot see (the CI service
  container, image constants in `tests/` and `scripts/`) are only held
  together by `scripts/check_image_pins.sh` failing CI when they disagree.
- The HA overlay runs a **single** etcd node — fine for a lab, a single point
  of failure anywhere real. See the HA section of the README.
- HA replication is **asynchronous**. On a hard failure (crash, partition), the
  replicas may not have received the last commits, and those are lost. A cut-off
  leader also keeps accepting writes until Patroni demotes it, which takes up to
  loop_wait + retry_timeout (measured 10–17s). `pg_rewind` discards those writes when
  it rejoins. `make ha-chaos-test` measures both. Patroni's `synchronous_mode` (and
  `synchronous_mode_strict`, which blocks writes rather than fall back to async)
  closes that gap but is untested here. After a hard failure, failover waits out the
  Patroni TTL (measured 22–28s with `ttl: 30`), compared with ~2s for a graceful stop.
- `make restore-test` is the only proof your backups work. Run it.

## 8. Upgrading images

Pull, then recreate: `docker compose pull && make up` (or `up -d --build`).
Two images need more than that.

**TimescaleDB.** A new image does not upgrade an existing database: the volume
keeps running the extension version it was created with (the image ships the
old library for exactly that reason). `migrate` runs
`ALTER EXTENSION timescaledb UPDATE` as the first statement of its own session
on every start, which is the only way the update is accepted. Sessions that
other services opened before `migrate` ran keep the old library loaded, so
restart them afterwards:

```sh
make up                                     # migrate updates the extension
docker compose logs migrate | grep 'timescaledb extension at'
docker compose restart loader detect        # plus agent-api / pollers if running
```

Take a backup first (`docs/BACKUPS.md`); extension updates are not reversible.

**etcd (HA overlay).** etcd only upgrades one minor version at a time, and 3.7
requires the cluster to be on 3.6.11 or later first. See the HA notes in the
README before bumping `coreos/etcd` on an existing `etcd_data` volume.
