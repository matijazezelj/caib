#!/usr/bin/env bash
# HA failover test (spec Phase 7 AC):
#   continuous synthetic landing inserts -> docker stop the current primary ->
#   assert: new primary elected <=60s, zero end-to-end loss (contiguous ids,
#   dedup makes the count exact), Grafana still answers, compression/retention
#   jobs listed on the new primary, TSL license, old node rejoins as replica.
# Run after: make ha-up (cluster healthy).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
APP_URL="postgresql://caib_app:${CAIB_PG_APP_PASSWORD}@localhost:5000/caib"
COMMITTED_FILE=$(mktemp)

sql() { uv run python -c "
import psycopg, sys
with psycopg.connect('$APP_URL', connect_timeout=5) as c:
    cur = c.execute(sys.argv[1])
    if cur.description:
        r = cur.fetchone()
        print(r[0] if r else '')
" "$1"; }

patroni_role() { # $1 = rest port, $2 = role path
  curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1/$2" 2>/dev/null || echo 000
}

find_primary() {
  for i in 1 2 3; do
    port=$((8007 + i))
    if [ "$(patroni_role "$port" primary)" = "200" ]; then echo "$i"; return 0; fi
  done
  return 1
}

echo "== preflight: cluster + license"
PRIMARY_N=$(find_primary) || { echo "FAIL: no primary"; exit 1; }
echo "   primary: spilo$PRIMARY_N"
LICENSE=$(sql "SHOW timescaledb.license")
[ "$LICENSE" = "timescale" ] || { echo "FAIL: license=$LICENSE"; exit 1; }
sql "DELETE FROM caib.events WHERE module = 'ha_test'" >/dev/null
sql "DELETE FROM ingest.landing WHERE module = 'ha_test'" >/dev/null

echo "== starting synthetic inserter"
uv run python scripts/ha_inserter.py --url "$APP_URL" --out "$COMMITTED_FILE" --rate 20 &
INSERTER_PID=$!
trap 'kill $INSERTER_PID 2>/dev/null || true' EXIT
sleep 5

echo "== stopping primary spilo$PRIMARY_N"
docker stop "caib-spilo${PRIMARY_N}-1" >/dev/null
T0=$(date +%s)

echo "== waiting for new primary (<=60s)"
NEW_PRIMARY=""
while [ -z "$NEW_PRIMARY" ]; do
  for i in 1 2 3; do
    [ "$i" = "$PRIMARY_N" ] && continue
    if [ "$(patroni_role $((8007 + i)) primary)" = "200" ]; then NEW_PRIMARY=$i; break; fi
  done
  [ $(( $(date +%s) - T0 )) -gt 60 ] && { echo "FAIL: no new primary in 60s"; exit 1; }
  sleep 2
done
ELECTION_S=$(( $(date +%s) - T0 ))
echo "   new primary: spilo$NEW_PRIMARY after ${ELECTION_S}s"

echo "== writes must resume through haproxy (<=60s from stop)"
RESUME_DEADLINE=$(( T0 + 60 ))
BASELINE=$(cat "$COMMITTED_FILE" 2>/dev/null || echo 0)
while true; do
  NOW_LANDING=$(sql "SELECT count(*) FROM ingest.landing WHERE module = 'ha_test'" 2>/dev/null || echo "")
  NOW_EVENTS=$(sql "SELECT count(*) FROM caib.events WHERE module = 'ha_test'" 2>/dev/null || echo "")
  if [ -n "$NOW_LANDING" ]; then break; fi
  [ "$(date +%s)" -gt "$RESUME_DEADLINE" ] && { echo "FAIL: writes did not resume in 60s"; exit 1; }
  sleep 2
done
sleep 10  # let some post-failover inserts land

echo "== stopping inserter, draining loader"
kill -TERM $INSERTER_PID
wait $INSERTER_PID || true
COMMITTED=$(cat "$COMMITTED_FILE")
echo "   committed inserts: $COMMITTED"
[ "$COMMITTED" -gt 100 ] || { echo "FAIL: too few inserts ($COMMITTED) to be meaningful"; exit 1; }

DRAIN_DEADLINE=$(( $(date +%s) + 120 ))
while [ "$(sql "SELECT count(*) FROM ingest.landing WHERE module = 'ha_test'")" != "0" ]; do
  [ "$(date +%s)" -gt "$DRAIN_DEADLINE" ] && { echo "FAIL: loader did not drain in 120s"; exit 1; }
  sleep 3
done

echo "== zero-loss check (contiguity: ids 0..N-1 all present, count == max+1)"
COUNT=$(sql "SELECT count(*) FROM caib.events WHERE module = 'ha_test'")
MAX_SEQ=$(sql "SELECT max((attrs->>'seq')::int) FROM caib.events WHERE module = 'ha_test'")
[ "$COUNT" = "$(( MAX_SEQ + 1 ))" ] || { echo "FAIL: gaps: count=$COUNT max_seq=$MAX_SEQ"; exit 1; }
[ "$COUNT" -ge "$COMMITTED" ] || { echo "FAIL: lost commits: count=$COUNT committed=$COMMITTED"; exit 1; }
echo "   count=$COUNT committed=$COMMITTED max_seq=$MAX_SEQ — no gaps, no loss"

echo "== grafana still answers (read path via haproxy:5001)"
GRAFANA_DB=$(curl -s "http://localhost:${CAIB_GRAFANA_PORT:-3000}/api/health" | uv run python -c "import json,sys; print(json.load(sys.stdin)['database'])")
[ "$GRAFANA_DB" = "ok" ] || { echo "FAIL: grafana health=$GRAFANA_DB"; exit 1; }
DS_OK=$(curl -s -u "admin:${CAIB_GRAFANA_ADMIN_PASSWORD:-admin}" \
  "http://localhost:${CAIB_GRAFANA_PORT:-3000}/api/datasources/uid/caib/health" \
  | uv run python -c "import json,sys; print(json.load(sys.stdin).get('status'))")
[ "$DS_OK" = "OK" ] || { echo "FAIL: datasource health=$DS_OK"; exit 1; }

echo "== timescale jobs follow the new primary"
JOBS=$(sql "SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name IN ('policy_compression','policy_retention')")
[ "$JOBS" -ge 2 ] || { echo "FAIL: expected compression+retention jobs, got $JOBS"; exit 1; }

echo "== old primary rejoins as replica"
docker start "caib-spilo${PRIMARY_N}-1" >/dev/null
REJOIN_DEADLINE=$(( $(date +%s) + 120 ))
while [ "$(patroni_role $((8007 + PRIMARY_N)) replica)" != "200" ]; do
  [ "$(date +%s)" -gt "$REJOIN_DEADLINE" ] && { echo "FAIL: spilo$PRIMARY_N did not rejoin in 120s"; exit 1; }
  sleep 3
done

echo "PASS: failover ${ELECTION_S}s, $COUNT events, zero loss, grafana up, jobs on new primary, node rejoined"
