#!/usr/bin/env bash
# HA failover test (spec Phase 7 AC):
#   continuous synthetic landing inserts -> docker stop the current primary ->
#   assert: new primary elected <=60s, zero end-to-end loss (contiguous ids,
#   dedup makes the count exact), Grafana still answers, compression/retention
#   jobs listed on the new primary, TSL license, old node rejoins as replica.
# Plus DETECTION CONTINUITY (the detect engine keeps firing across the
# failover, without losing or double-counting events):
#   - a probe lander (scripts/ha_detect_probe.py) lands envelopes that
#     aws_cloudtrail.root_console_login fires on, for key A, continuously from
#     before the kill until after promotion (so events sitting inside detect's
#     safety lag at kill time are covered) -> alert A must exist pre-failover;
#   - after promotion a one-shot batch for a NEW key B lands -> alert B must be
#     raised within a bounded time, and A's event_count must keep growing;
#   - exact counts: alert.event_count == events in caib.events == committed by
#     the lander, per key (lower = lost, higher = double-counted window), and
#     exactly one 'new' outbox row per alert (no double notification);
#   - loader and detect heartbeats are stamped after promotion (DB clock).
# Run after: make ha-up (cluster healthy).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
APP_URL="postgresql://caib_app:${CAIB_PG_APP_PASSWORD}@localhost:5000/caib"
COMMITTED_FILE=$(mktemp)
PROBE_A_FILE=$(mktemp)
RUN_ID="$(date +%s)"
RULE=aws_cloudtrail.root_console_login
PROBE_N_B=7
# Detect sees an event once ingested_at < now - safety lag (60s), evaluates
# this rule every 60s and ticks every 15s: ~135s worst case after ingest, plus
# loader interval and a crash/restart of detect. 240s leaves margin, not slack
# for a hang.
DETECT_BOUND_S=240

sql() { uv run python -c "
import psycopg, sys
with psycopg.connect('$APP_URL', connect_timeout=5) as c:
    cur = c.execute(sys.argv[1])
    if cur.description:
        r = cur.fetchone()
        print(r[0] if r else '')
" "$1"; }

elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }

# alert row for a probe key: "event_count|new_outbox_rows|outbox_created_epoch"
alert_state() {
  sql "SELECT a.event_count || '|' ||
         (SELECT count(*) FROM detect.outbox o WHERE o.alert_id = a.id AND o.kind = 'new') || '|' ||
         coalesce((SELECT extract(epoch FROM min(o.created_at)) FROM detect.outbox o
                   WHERE o.alert_id = a.id AND o.kind = 'new')::text, '')
       FROM detect.alerts a
       WHERE a.rule_id = '$RULE' AND a.dedup_key = 'ha-root-$RUN_ID-$1|198.51.100.200'" 2>/dev/null || true
}

probe_events() {
  sql "SELECT count(*) FROM caib.events WHERE module = 'aws_cloudtrail'
       AND actor = 'ha-root-$RUN_ID-$1'"
}

detect_diag() {
  echo "   diag: heartbeats: $(sql "SELECT string_agg(service || ' ' || round(extract(epoch FROM now() - beat_at)) || 's ago', ', ') FROM ops.heartbeat" 2>/dev/null || echo '?')"
  echo "   diag: detect container: $(docker inspect -f '{{.State.Status}} restarts={{.RestartCount}}' caib-detect-1 2>/dev/null || echo missing)"
}

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
[ "$(docker inspect -f '{{.State.Running}}' caib-detect-1 2>/dev/null)" = "true" ] \
  || { echo "FAIL: detect container is not running — nothing to test continuity of"; exit 1; }
DETECT_RESTARTS_BEFORE=$(docker inspect -f '{{.RestartCount}}' caib-detect-1)

INSERTER_PID=""; PROBE_A_PID=""; STOPPED_NODE=""
cleanup() {
  [ -n "$INSERTER_PID" ] && kill "$INSERTER_PID" 2>/dev/null || true
  [ -n "$PROBE_A_PID" ] && kill "$PROBE_A_PID" 2>/dev/null || true
  # A failed run must not leave a two-node cluster behind.
  [ -n "$STOPPED_NODE" ] && docker start "caib-spilo${STOPPED_NODE}-1" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== detection probe: key A lands continuously (1/s) from before the kill"
uv run python scripts/ha_detect_probe.py --url "$APP_URL" --run "$RUN_ID" --key A \
  --rate 1 --out "$PROBE_A_FILE" &
PROBE_A_PID=$!
T_PROBE=$EPOCHREALTIME

echo "== pre-failover: $RULE must raise alert A (<=${DETECT_BOUND_S}s)"
while :; do
  STATE_A=$(alert_state A)
  [ -n "$STATE_A" ] && break
  if [ "$(elapsed "$T_PROBE" "$EPOCHREALTIME" | cut -d. -f1)" -gt "$DETECT_BOUND_S" ]; then
    echo "FAIL: pre-failover alert A not raised in ${DETECT_BOUND_S}s — detect is not firing even before the failover"
    detect_diag; exit 1
  fi
  sleep 1
done
PRE_ALERT_S=$(elapsed "$T_PROBE" "$EPOCHREALTIME")
PRE_COUNT_A=${STATE_A%%|*}
echo "   alert A raised ${PRE_ALERT_S}s after probe start (event_count=$PRE_COUNT_A)"

echo "== starting synthetic inserter"
uv run python scripts/ha_inserter.py --url "$APP_URL" --out "$COMMITTED_FILE" --rate 20 &
INSERTER_PID=$!
sleep 5

echo "== stopping primary spilo$PRIMARY_N"
STOPPED_NODE=$PRIMARY_N
T0_RT=$EPOCHREALTIME
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
  sleep 0.5
done
ELECTION_S=$(elapsed "$T0_RT" "$EPOCHREALTIME")
echo "   new primary: spilo$NEW_PRIMARY after ${ELECTION_S}s (stop -> Patroni /primary 200)"

echo "== writes must resume through haproxy (<=60s from stop)"
RESUME_DEADLINE=$(( T0 + 60 ))
BASELINE=$(cat "$COMMITTED_FILE" 2>/dev/null || echo 0)
while true; do
  NOW_LANDING=$(sql "SELECT count(*) FROM ingest.landing WHERE module = 'ha_test'" 2>/dev/null || echo "")
  NOW_EVENTS=$(sql "SELECT count(*) FROM caib.events WHERE module = 'ha_test'" 2>/dev/null || echo "")
  if [ -n "$NOW_LANDING" ]; then break; fi
  [ "$(date +%s)" -gt "$RESUME_DEADLINE" ] && { echo "FAIL: writes did not resume in 60s"; exit 1; }
  sleep 0.5
done
RESUME_S=$(elapsed "$T0_RT" "$EPOCHREALTIME")
# DB clock on the new primary: anything stamped after this was written post-failover.
T_PROMO_DB=$(sql "SELECT now()")
echo "   writes resumed through haproxy ${RESUME_S}s after stop (new-primary clock: $T_PROMO_DB)"

echo "== post-failover: landing NEW key B ($PROBE_N_B events)"
uv run python scripts/ha_detect_probe.py --url "$APP_URL" --run "$RUN_ID" --key B \
  --count "$PROBE_N_B" \
  || { echo "FAIL: could not land post-failover probe batch B"; exit 1; }
T_B_RT=$EPOCHREALTIME
sleep 10  # let some post-failover inserts land

echo "== stopping inserter, draining loader"
kill -TERM $INSERTER_PID
wait $INSERTER_PID || true
kill -TERM $PROBE_A_PID
wait $PROBE_A_PID || true
COMMITTED_A=$(cat "$PROBE_A_FILE")
COMMITTED=$(cat "$COMMITTED_FILE")
echo "   committed inserts: $COMMITTED"
[ "$COMMITTED" -gt 100 ] || { echo "FAIL: too few inserts ($COMMITTED) to be meaningful"; exit 1; }

DRAIN_DEADLINE=$(( $(date +%s) + 120 ))
while [ "$(sql "SELECT count(*) FROM ingest.landing WHERE module IN ('ha_test', 'aws_cloudtrail')")" != "0" ]; do
  [ "$(date +%s)" -gt "$DRAIN_DEADLINE" ] && { echo "FAIL: loader did not drain in 120s"; exit 1; }
  sleep 3
done

echo "== zero-loss check (contiguity: ids 0..N-1 all present, count == max+1)"
COUNT=$(sql "SELECT count(*) FROM caib.events WHERE module = 'ha_test'")
MAX_SEQ=$(sql "SELECT max((attrs->>'seq')::int) FROM caib.events WHERE module = 'ha_test'")
[ "$COUNT" = "$(( MAX_SEQ + 1 ))" ] || { echo "FAIL: gaps: count=$COUNT max_seq=$MAX_SEQ"; exit 1; }
[ "$COUNT" -ge "$COMMITTED" ] || { echo "FAIL: lost commits: count=$COUNT committed=$COMMITTED"; exit 1; }
echo "   count=$COUNT committed=$COMMITTED max_seq=$MAX_SEQ — no gaps, no loss"

echo "== detection probe events: exactly what the lander committed"
EV_A=$(probe_events A); EV_B=$(probe_events B)
[ "$EV_A" = "$COMMITTED_A" ] || { echo "FAIL: probe A events=$EV_A but lander committed $COMMITTED_A (lost or duplicated across failover)"; exit 1; }
[ "$EV_B" = "$PROBE_N_B" ] || { echo "FAIL: probe B events=$EV_B, landed $PROBE_N_B"; exit 1; }
[ "$EV_A" -gt "$PRE_COUNT_A" ] || { echo "FAIL: no key-A events after the pre-failover alert ($EV_A)"; exit 1; }
echo "   A=$EV_A (committed $COMMITTED_A)  B=$EV_B (landed $PROBE_N_B)"

echo "== detection continuity: alert B raised, A and B event_count exact (<=${DETECT_BOUND_S}s after B landed)"
while :; do
  STATE_A=$(alert_state A); STATE_B=$(alert_state B)
  CNT_A=${STATE_A%%|*}; CNT_B=${STATE_B%%|*}
  # Over-count is final (counts only grow): a re-run window was double-counted.
  [ -n "$CNT_A" ] && [ "$CNT_A" -gt "$EV_A" ] && { echo "FAIL: alert A event_count=$CNT_A > $EV_A events — detection double-counted a window across the failover"; exit 1; }
  [ -n "$CNT_B" ] && [ "$CNT_B" -gt "$EV_B" ] && { echo "FAIL: alert B event_count=$CNT_B > $EV_B events — double-counted"; exit 1; }
  [ "$CNT_A" = "$EV_A" ] && [ "$CNT_B" = "$EV_B" ] && break
  if [ "$(elapsed "$T_B_RT" "$EPOCHREALTIME" | cut -d. -f1)" -gt "$DETECT_BOUND_S" ]; then
    if [ -z "$STATE_B" ]; then
      echo "FAIL: DETECTION GAP — no alert for post-failover key B ${DETECT_BOUND_S}s after it landed (A event_count=${CNT_A:-none}/$EV_A)"
    else
      echo "FAIL: DETECTION GAP — events missing from alerts after failover: A event_count=${CNT_A:-none}/$EV_A, B event_count=$CNT_B/$EV_B"
    fi
    detect_diag; exit 1
  fi
  sleep 1
done
IFS='|' read -r _ NEW_A _ <<<"$STATE_A"
IFS='|' read -r _ NEW_B OUTBOX_B_EPOCH <<<"$STATE_B"
[ "$NEW_A" = "1" ] || { echo "FAIL: alert A has $NEW_A 'new' outbox rows, expected exactly 1 (re-notified or never notified)"; exit 1; }
[ "$NEW_B" = "1" ] || { echo "FAIL: alert B has $NEW_B 'new' outbox rows, expected exactly 1"; exit 1; }
ALERT_FROM_KILL_S=$(elapsed "$T0_RT" "$OUTBOX_B_EPOCH")
ALERT_FROM_LAND_S=$(elapsed "$T_B_RT" "$OUTBOX_B_EPOCH")
echo "   alert B raised ${ALERT_FROM_KILL_S}s after primary stop, ${ALERT_FROM_LAND_S}s after batch B landed (detect.outbox created_at)"
echo "   alert A event_count $PRE_COUNT_A -> $CNT_A; one 'new' notification each"

echo "== heartbeats advance past the failover (beat_at > $T_PROMO_DB, <=60s)"
HB_DEADLINE=$(( $(date +%s) + 60 ))
for svc in loader detect; do
  until [ "$(sql "SELECT count(*) FROM ops.heartbeat WHERE service = '$svc' AND beat_at > '$T_PROMO_DB'::timestamptz")" = "1" ]; do
    [ "$(date +%s)" -gt "$HB_DEADLINE" ] && { echo "FAIL: $svc heartbeat not stamped after promotion"; detect_diag; exit 1; }
    sleep 2
  done
done
DETECT_RESTARTS=$(( $(docker inspect -f '{{.RestartCount}}' caib-detect-1) - DETECT_RESTARTS_BEFORE ))
echo "   loader + detect beat on the new primary; detect restarted $DETECT_RESTARTS time(s) during failover"

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
STOPPED_NODE=""

echo "PASS: failover ${ELECTION_S}s, $COUNT events, zero loss, grafana up, jobs on new primary, node rejoined"
echo "PASS: detection continuity: alert B ${ALERT_FROM_KILL_S}s after stop (${ALERT_FROM_LAND_S}s after landing), A $PRE_COUNT_A -> $CNT_A, counts exact, heartbeats advanced"
