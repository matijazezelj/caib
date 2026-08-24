#!/usr/bin/env bash
# Automated restore drill (docs/BACKUPS.md, made executable):
#   backup-enabled dev node -> marker row -> full backup -> restore into a
#   scratch volume -> start a scratch postgres -> assert license, marker, and
#   event counts match -> clean up the scratch.
# Leaves the dev stack running WITH archiving enabled (the backup overlay).
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_B="docker compose -f docker-compose.yml -f compose.backup.yml"
DB=caib-timescaledb-1
SCRATCH_VOL=caib_restore_scratch
SCRATCH_PG=caib-restore-pg
MARKER="restore-drill-$(date +%s)"

cleanup() {
  docker rm -f "$SCRATCH_PG" >/dev/null 2>&1 || true
  docker volume rm "$SCRATCH_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== bringing up backup-enabled dev node"
$COMPOSE_B up -d --build timescaledb
until docker exec "$DB" pg_isready -U postgres -d caib -q 2>/dev/null; do sleep 2; done
$COMPOSE_B run --rm migrate >/dev/null

echo "== seeding marker + counting events"
docker exec "$DB" psql -U postgres -d caib -qc \
  "CREATE TABLE IF NOT EXISTS public.restore_marker (v text); INSERT INTO public.restore_marker VALUES ('$MARKER')"
EVENTS_BEFORE=$(docker exec "$DB" psql -U postgres -d caib -Atc "SELECT count(*) FROM caib.events")

echo "== stanza-create + full backup"
docker exec -u postgres "$DB" pgbackrest --stanza=caib stanza-create
docker exec -u postgres "$DB" pgbackrest --stanza=caib check
docker exec -u postgres "$DB" pgbackrest --stanza=caib backup --type=full

echo "== restoring into scratch volume"
cleanup  # remove leftovers from a previous run before the fresh restore
docker volume create "$SCRATCH_VOL" >/dev/null
docker run --rm \
  -v "$SCRATCH_VOL":/restore \
  -v caib_backrest_repo:/var/lib/pgbackrest \
  -v "$PWD/backup/pgbackrest.conf":/etc/pgbackrest/pgbackrest.conf:ro \
  --entrypoint sh caib-timescaledb-backup:dev -c \
  "mkdir -p /restore/pgdata && chown -R postgres:postgres /restore && \
   su postgres -c 'pgbackrest --stanza=caib --pg1-path=/restore/pgdata restore'"

echo "== starting scratch postgres on the restored data"
docker run -d --name "$SCRATCH_PG" \
  -v "$SCRATCH_VOL":/restore \
  -v caib_backrest_repo:/var/lib/pgbackrest \
  -v "$PWD/backup/pgbackrest.conf":/etc/pgbackrest/pgbackrest.conf:ro \
  -e PGDATA=/restore/pgdata \
  caib-timescaledb-backup:dev \
  postgres -c shared_preload_libraries=timescaledb -c archive_mode=off >/dev/null

DEADLINE=$(( $(date +%s) + 120 ))
until docker exec "$SCRATCH_PG" pg_isready -U postgres -d caib -q 2>/dev/null; do
  [ "$(date +%s)" -gt "$DEADLINE" ] && { echo "FAIL: restored postgres not up in 120s"; docker logs "$SCRATCH_PG" | tail -20; exit 1; }
  sleep 2
done

echo "== assertions"
LICENSE=$(docker exec "$SCRATCH_PG" psql -U postgres -d caib -Atc "SHOW timescaledb.license")
[ "$LICENSE" = "timescale" ] || { echo "FAIL: license=$LICENSE"; exit 1; }
GOT_MARKER=$(docker exec "$SCRATCH_PG" psql -U postgres -d caib -Atc \
  "SELECT v FROM public.restore_marker WHERE v = '$MARKER'")
[ "$GOT_MARKER" = "$MARKER" ] || { echo "FAIL: marker missing after restore"; exit 1; }
EVENTS_AFTER=$(docker exec "$SCRATCH_PG" psql -U postgres -d caib -Atc "SELECT count(*) FROM caib.events")
[ "$EVENTS_AFTER" = "$EVENTS_BEFORE" ] || { echo "FAIL: events $EVENTS_AFTER != $EVENTS_BEFORE"; exit 1; }

echo "PASS: restored $EVENTS_AFTER events, marker present, TSL license intact"
echo "note: dev stack now runs the backup overlay (archiving on); 'make up' reverts"
