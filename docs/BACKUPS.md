# Backups with pgBackRest

CAIB ships no backup automation — retention policies drop data on schedule and
replicas are not backups (they replicate your mistakes at wire speed). This
documents the pgBackRest sidecar approach for both deployment shapes.
Compression matters here: pgBackRest backs up the compressed chunks as-is, so
a 90-day CAIB archive is typically a fraction of the raw log volume.

## Dev / single node (sidecar)

Add to `docker-compose.yml` (or a `compose.backup.yml` overlay):

```yaml
services:
  pgbackrest:
    image: woblerr/pgbackrest:latest        # or build your own from pgbackrest.org
    volumes:
      - pgdata:/var/lib/postgresql          # same volume as timescaledb
      - ./backup/pgbackrest.conf:/etc/pgbackrest/pgbackrest.conf:ro
      - backrest_repo:/var/lib/pgbackrest   # or S3, below
```

`backup/pgbackrest.conf` (S3-compatible repo; MinIO works):

```ini
[global]
repo1-type=s3
repo1-s3-bucket=caib-backups
repo1-s3-endpoint=minio:9000
repo1-s3-key=<access key>
repo1-s3-key-secret=<secret>
repo1-s3-uri-style=path
repo1-retention-full=2
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=<generate one>
process-max=2

[caib]
pg1-path=/var/lib/postgresql/18/docker
```

Postgres needs WAL archiving (add to the timescaledb command args):

```
-c archive_mode=on
-c archive_command='pgbackrest --stanza=caib archive-push %p'
```

Then:

```sh
docker compose exec pgbackrest pgbackrest --stanza=caib stanza-create
docker compose exec pgbackrest pgbackrest --stanza=caib backup --type=full
```

Schedule with host cron (the container has no scheduler on purpose —
one scheduler per concern):

```cron
0 2 * * 0  docker compose exec -T pgbackrest pgbackrest --stanza=caib backup --type=full
0 2 * * 1-6 docker compose exec -T pgbackrest pgbackrest --stanza=caib backup --type=incr
```

## HA cluster

Run pgBackRest on **every** Patroni node (same config, shared S3 repo) and let
Patroni own the archiving settings so they follow the primary. In
`ha/patroni.yml` under `bootstrap.dcs.postgresql.parameters`:

```yaml
archive_mode: "on"
archive_command: pgbackrest --stanza=caib archive-push %p
```

and configure Patroni to restore replicas from backup instead of basebackup
when possible (`postgresql.create_replica_methods: [pgbackrest, basebackup]`
plus a `pgbackrest` method section — see Patroni docs). Only the primary
archives; `backup-standby=y` lets backups run from a replica so the primary
stays unloaded.

## Restore drill (do this once before you need it)

```sh
pgbackrest --stanza=caib --set=<backup-label> --delta restore
# or point-in-time:
pgbackrest --stanza=caib --type=time "--target=2026-08-24 12:00:00+00" restore
```

Restore into a scratch container, run `make test`-style sanity
(`SHOW timescaledb.license`, `SELECT count(*) FROM caib.events`), and time the
whole thing — an untested backup is a hope, not a backup.

## What to back up beyond the DB

- `.env` (secrets — store separately/encrypted)
- `detect/rules/` and `grafana/provisioning/` are in git already; the DB
  carries only state (alerts, cursors, watermarks, events)
