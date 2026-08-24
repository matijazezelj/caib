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

Everything below **ships in the repo**: `backup/Dockerfile` (stock alpine image
+ `apk add pgbackrest`), `backup/pgbackrest.conf` (local posix repo by
default), and `compose.backup.yml` (archiving enabled, repo volume mounted).

```sh
docker compose -f docker-compose.yml -f compose.backup.yml up -d --build
make restore-test    # the full drill, automated — see below
```

For a real deployment, switch the shipped conf's repo1 to S3-compatible
storage (MinIO works):

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

**Scheduling on HA:** never point cron at a fixed node — spilo1 is not
reliably the leader. `scripts/ha_backup.sh` asks each node's Patroni REST API
for the primary and execs pgbackrest there:

```cron
0 2 * * 0   /path/to/caib/scripts/ha_backup.sh full
0 2 * * 1-6 /path/to/caib/scripts/ha_backup.sh incr
```

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

## Restore drill

Automated: `make restore-test` takes a full backup of the dev node, restores
it into a scratch volume, boots a scratch postgres on the restored data, and
asserts the TSL license, a marker row, and matching event counts
(`scripts/restore_test.sh`). Run it after changing anything backup-related.

Point-in-time restores stay manual:

```sh
pgbackrest --stanza=caib --type=time "--target=2026-08-24 12:00:00+00" restore
```

An untested backup is a hope, not a backup — the make target exists so the
test actually happens.

## What to back up beyond the DB

- `.env` (secrets — store separately/encrypted)
- `detect/rules/` and `grafana/provisioning/` are in git already; the DB
  carries only state (alerts, cursors, watermarks, events)
