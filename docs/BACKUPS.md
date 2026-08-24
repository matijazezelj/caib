# Backups with pgBackRest

CAIB ships no backup automation — retention policies drop data on schedule and
replicas are not backups (they replicate your mistakes at wire speed).
Compression helps here: pgBackRest backs up the compressed chunks as-is, and
the committed benchmark measured **7.05x** columnar compression on 1M events
(`bench/results/`), so a 90-day archive backs up far smaller than the raw log
volume.

**The one architectural fact this document is built around:**
`archive_command` executes inside the *database* container. The pgbackrest
binary must exist there — a sidecar alone cannot archive WAL, and a failing
`archive_command` means WAL piles up until disk pressure kills the node.

## Dev / single node

The stock `timescale/timescaledb` alpine image does not ship pgbackrest, but
the package is in the alpine repo. Two-line image:

```dockerfile
# backup/Dockerfile
FROM timescale/timescaledb:2.29.2-pg18
RUN apk add --no-cache pgbackrest
```

Compose changes (build the image, mount the config, enable archiving):

```yaml
services:
  timescaledb:
    build: ./backup                    # instead of image:
    command:
      - postgres
      - -c
      - shared_preload_libraries=timescaledb
      - -c
      - default_toast_compression=lz4
      - -c
      - timezone=UTC
      - -c
      - archive_mode=on
      - -c
      - archive_command=pgbackrest --stanza=caib archive-push %p
    volumes:
      - pgdata:/var/lib/postgresql
      - ./backup/pgbackrest.conf:/etc/pgbackrest/pgbackrest.conf:ro
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

Initialize once, then schedule with host cron (one scheduler per concern —
no cron inside containers):

```sh
docker compose exec -T timescaledb pgbackrest --stanza=caib stanza-create
docker compose exec -T timescaledb pgbackrest --stanza=caib backup --type=full
```

```cron
0 2 * * 0   docker compose exec -T timescaledb pgbackrest --stanza=caib backup --type=full
0 2 * * 1-6 docker compose exec -T timescaledb pgbackrest --stanza=caib backup --type=incr
```

Watch `SELECT last_archived_wal, last_failed_wal FROM pg_stat_archiver` — a
growing `last_failed_wal` is the WAL-pileup alarm (a candidate for an ops
detection rule).

## HA cluster

`timescale/timescaledb-ha` **already ships pgBackRest** (2.59.1 in the pinned
tag) — no image change needed. Mount the same `pgbackrest.conf` on every node
and let Patroni own the archiving settings so they follow the primary — in
`ha/patroni.yml` under `bootstrap.dcs.postgresql.parameters`:

```yaml
archive_mode: "on"
archive_command: pgbackrest --stanza=caib archive-push %p
```

(Only the current primary archives; the parameters travel with failover.)

Optionally configure Patroni to build replicas from backup instead of
basebackup (`postgresql.create_replica_methods: [pgbackrest, basebackup]` plus
a `pgbackrest` method section — see Patroni docs).

To back up **from a replica** so the primary stays unloaded, the config needs
every node listed and standby backup enabled:

```ini
[caib]
pg1-path=/home/postgres/pgdata/data
pg1-host=spilo1
pg2-path=/home/postgres/pgdata/data
pg2-host=spilo2
pg3-path=/home/postgres/pgdata/data
pg3-host=spilo3
backup-standby=y
```

Host-to-host access for that mode needs the pgBackRest TLS server (or SSH)
between nodes — start without it (backups from the primary) and add it when
primary load actually becomes a problem.

## Restore drill (do this once before you need it)

```sh
pgbackrest --stanza=caib --set=<backup-label> --delta restore
# or point-in-time:
pgbackrest --stanza=caib --type=time "--target=2026-08-24 12:00:00+00" restore
```

Restore into a scratch container, sanity-check (`SHOW timescaledb.license`,
`SELECT count(*) FROM caib.events`), and time the whole thing — an untested
backup is a hope, not a backup.

## What to back up beyond the DB

- `.env` (secrets — store separately/encrypted)
- `detect/rules/` and `grafana/provisioning/` are in git already; the DB
  carries only state (alerts, cursors, watermarks, events)
