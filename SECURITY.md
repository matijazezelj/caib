# Security policy

## Supported versions

There are no releases yet. `main` is the only supported branch — fixes land
there and nowhere else. Once the first tag is cut this section will say which
tags still get fixes; until then, "supported" means "run `main`".

## Reporting a vulnerability

Email **matija@rtfm.hr**. PGP is not set up; if you need an encrypted channel,
say so in a first message and we will arrange one.

Useful to include: what you did, what happened, and which commit you were on.
A proof of concept is welcome but not required.

**Expect an acknowledgement within 7 days.** That is an acknowledgement, not a
fix — CAIB is a spare-time project, so a realistic fix timeline comes with the
acknowledgement rather than before it. Please give us a chance to ship
something before disclosing publicly, and tell us if you have a date you need
to disclose by.

Do not open a public GitHub issue for a vulnerability.

## Scope

**In scope:** this repository — the Python in `core/`, `pollers/` and
`agentapi/` (the token-authenticated agent API and MCP server), the SQL in
`db/migrations/`, the detection rules, the Vector configs, the compose files
and the HA (`ha/`) and backup (`backup/`) configs, the Dockerfiles, and the
scripts in `scripts/`.

**Out of scope:** the vendor APIs CAIB polls (Cloudflare, GCP, Azure, AWS) and
the upstream images it pins (TimescaleDB, Vector, Grafana, Patroni, HAProxy,
etcd). Report those to their vendors. If an upstream advisory affects what CAIB
ships, an issue here is the right place — that is a dependency bump, not a
vulnerability report.

Also out of scope: the deployment choices listed below, which are documented
behaviour rather than bugs.

## What CAIB does not do

Read [docs/DEPLOY.md](docs/DEPLOY.md) before putting this on a host that is not
your laptop. The short version of what is deliberately absent:

- **There is no TLS and no extra auth layer in front of Postgres, Grafana or
  the agent API.** Each has its own login (database passwords, the Grafana
  admin password, the agent API bearer token); beyond that, the only access
  control is the bind address. The base compose file publishes Postgres (5432),
  Grafana (3000) and the optional agent API (8088) on `CAIB_BIND_ADDR`, which
  defaults to `127.0.0.1`. The HA overlay (`compose.ha.yml`) publishes HAProxy
  (5000, 5001, 7000) and the three Patroni REST APIs (8008-8010) on
  `127.0.0.1`, hard-coded rather than following `CAIB_BIND_ADDR`. Reaching
  any of them from elsewhere is your SSH tunnel or your TLS reverse proxy to
  build.
  Note that Docker publishes ports in its own iptables chain, ahead of ufw and
  firewalld — a firewall rule will not save a `0.0.0.0` bind.
- **The HA overlay's control endpoints have no authentication configured.**
  `ha/patroni.yml` sets no `restapi` authentication, so the Patroni REST API
  (which includes switchover and restart endpoints) is open to anything on the
  compose network and, via the published ports, to host loopback. The same
  goes for the HAProxy stats page on 7000 (`ha/haproxy.cfg`) and the single
  etcd node (plain HTTP, compose network only). The overlay is lab-shaped; see
  [docs/DEPLOY.md](docs/DEPLOY.md) before running it anywhere shared.
- **Secrets are container environment variables.** Anyone in the `docker` group
  can read them with `docker inspect`.
- **No multi-tenancy.** Everything that can read the database can read every
  event in it; `caib_ro` is read-only, not scoped.
- **Healthchecks do not page anyone.** They mark a container unhealthy and
  stop there. External monitoring still owns liveness.

None of these are secrets, and none of them are things we would like a report
about. A way to get *past* one of them — reading events without a database
credential, say — is very much a report we want.
