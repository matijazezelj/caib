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

`caib.events` is compressed after 7 days and **dropped after 90**. That default
is active from the first migration. If you have a compliance retention floor,
change it before you need the data, not after:

```sql
SELECT remove_retention_policy('caib.events');
SELECT add_retention_policy('caib.events', drop_after => INTERVAL '400 days');
```

Do not edit the migrations — they are append-only and already applied.

## 7. Known rough edges

Honest list, so nothing here is a surprise in an audit:

- Image pins are bumped by dependabot, but copies it cannot see (the CI service
  container, image constants in `tests/` and `scripts/`) are only held
  together by `scripts/check_image_pins.sh` failing CI when they disagree.
- The HA overlay runs a **single** etcd node — fine for a lab, a single point
  of failure anywhere real. See the HA section of the README.
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
