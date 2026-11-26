#!/usr/bin/env bash
# HA failover test (spec Phase 7 AC), in three fault modes:
#
#   stop       (default; make ha-failover-test) graceful `docker stop` of the
#              leader: Patroni releases the leader key, a replica takes over in
#              seconds, and nothing committed can be lost.
#   kill       (make ha-chaos-test) `docker kill` (SIGKILL) of the leader: the
#              key is never released, the cluster waits out the TTL.
#   partition  (make ha-chaos-test) `docker network disconnect` of the leader
#              from the compose network: it is alive but cannot reach etcd,
#              HAProxy or the replicas, and must demote itself.
#
# Every mode: continuous synthetic landing inserts -> fault -> assert a new
# primary within a bound, writes resume through HAProxy, Grafana answers,
# compression/retention jobs on the new primary, TSL license, old node rejoins
# as a streaming replica on the new timeline.
#
# Loss: replication is ASYNCHRONOUS (ha/patroni.yml sets no synchronous_mode).
# A graceful stop ships all WAL before the key is released, so `stop` asserts
# zero loss. A hard kill or partition can lose commits the replicas had not
# received yet; `kill` and `partition` MEASURE that loss (inserter seqs that
# were acknowledged but are missing) and report it, and fail only if it is not
# shaped like a lost WAL tail (one contiguous run) or if anything duplicated.
#
# DETECTION CONTINUITY (the detect engine keeps firing across the failover,
# without double-counting):
#   - a probe lander (scripts/ha_detect_probe.py) lands envelopes that
#     aws_cloudtrail.root_console_login fires on, for key A, continuously from
#     before the fault until after promotion -> alert A must exist pre-failover;
#   - after promotion a one-shot batch for a NEW key B lands -> alert B must be
#     raised within a bound, and A's event_count must keep growing;
#   - exact counts: alert.event_count == events in caib.events per key (higher
#     = double-counted window, lower = lost); events == lander commits in
#     `stop` (in kill/partition, fewer events = measured async loss);
#     exactly one 'new' outbox row per alert (no double notification);
#   - loader and detect heartbeats are stamped after promotion (DB clock).
#
# PARTITION additionally: a writer loops real INSERTs straight into the
# isolated node (docker exec psql, local socket) and records every accepted
# one. No split brain = the last write the old leader accepted is earlier than
# the new leader acquiring the key. HAProxy's write listener must list only
# the new leader UP; after reconnect, rows the isolated node accepted must be
# gone from every node (its divergent timeline was rewound, not merged).
#
# Run after: make ha-up (cluster healthy).
set -euo pipefail
cd "$(dirname "$0")/.."

MODE=${1:-stop}
case "$MODE" in stop|kill|partition) ;; *) echo "usage: $0 [stop|kill|partition]"; exit 2 ;; esac
# Negative control only: the signal `kill` mode sends. TERM makes it a
# graceful shutdown, which must FAIL the lower promotion bound.
KILL_SIGNAL=${HA_KILL_SIGNAL:-KILL}

set -a; . ./.env; set +a
APP_URL="postgresql://caib_app:${CAIB_PG_APP_PASSWORD}@localhost:5000/caib"
COMMITTED_FILE=$(mktemp)
PROBE_A_FILE=$(mktemp)
SPLIT_LOG=$(mktemp)
RUN_ID="$(date +%s)"
RULE=aws_cloudtrail.root_console_login
PROBE_N_B=7
# Detect sees an event once ingested_at < now - safety lag (60s), evaluates
# this rule every 60s and ticks every 15s: ~135s worst case after ingest, plus
# loader interval and a crash/restart of detect. 240s leaves margin, not slack
# for a hang.
DETECT_BOUND_S=240
. scripts/ha_lib.sh

echo "== [$MODE] preflight: healthy cluster (1 leader + 2 streaming replicas, no lag) + license"
wait_cluster_healthy 180 || fail "cluster not healthy after 180s (run make ha-up and wait for replicas)"
PRIMARY_N=$(find_primary) || fail "no primary"
REPLICAS=""; for i in 1 2 3; do [ "$i" != "$PRIMARY_N" ] && REPLICAS="$REPLICAS $i"; done
TL_BEFORE=$(node_timeline "$PRIMARY_N")
echo "   primary: spilo$PRIMARY_N (timeline $TL_BEFORE)"
LICENSE=$(sql "SHOW timescaledb.license")
[ "$LICENSE" = "timescale" ] || fail "license=$LICENSE"
sql "DELETE FROM caib.events WHERE module = 'ha_test'" >/dev/null
sql "DELETE FROM ingest.landing WHERE module = 'ha_test'" >/dev/null
[ "$(docker inspect -f '{{.State.Running}}' caib-detect-1 2>/dev/null)" = "true" ] \
  || fail "detect container is not running — nothing to test continuity of"
DETECT_RESTARTS_BEFORE=$(docker inspect -f '{{.RestartCount}}' caib-detect-1)

# Promotion bounds, from the live DCS config rather than constants.
TTL=$(patroni_dcs ttl); LOOP_WAIT=$(patroni_dcs loop_wait); RETRY_TIMEOUT=$(patroni_dcs retry_timeout)
if [ "$MODE" = stop ]; then
  # The leader deletes its key on shutdown; a replica takes it on its next
  # watch wake-up. 60s is the spec's AC.
  PROMO_MIN_S=0; PROMO_MAX_S=60
else
  # Nobody releases the key. The leader last refreshed it at most loop_wait
  # before the fault, so it expires between (ttl - loop_wait) and ttl after;
  # a replica then needs up to one more HA cycle (loop_wait) and DCS/promote
  # slack (retry_timeout) to take it:
  #   ttl - loop_wait - 1  <=  promotion  <=  ttl + loop_wait + retry_timeout
  # (-1: the leader's loop does not run at exact loop_wait spacing). Faster
  # than the minimum means the key was released: not a hard failure.
  PROMO_MIN_S=$(( TTL - LOOP_WAIT - 1 )); PROMO_MAX_S=$(( TTL + LOOP_WAIT + RETRY_TIMEOUT ))
fi
# Writes resume once HAProxy sees the new leader: rise 2 x inter 3s after
# promotion, plus a retry interval.
RESUME_MAX_S=$(( PROMO_MAX_S + 15 ))
echo "   patroni ttl=$TTL loop_wait=$LOOP_WAIT retry_timeout=$RETRY_TIMEOUT -> promotion bound [${PROMO_MIN_S}s, ${PROMO_MAX_S}s], writes resume <=${RESUME_MAX_S}s"

if [ "$MODE" = partition ]; then
  node_sql "$PRIMARY_N" "CREATE TABLE IF NOT EXISTS public.ha_chaos_split
    (id bigserial, at timestamptz DEFAULT clock_timestamp(), run text)" >/dev/null \
    || fail "could not create the split-brain probe table on spilo$PRIMARY_N"
  # Aliases to restore on reconnect (service name + container name).
  ALIASES=$(docker inspect -f "{{range (index .NetworkSettings.Networks \"$COMPOSE_NET\").Aliases}}--alias {{.}} {{end}}" "caib-spilo${PRIMARY_N}-1")
fi

INSERTER_PID=""; PROBE_A_PID=""; SPLIT_PID=""; FAULT_NODE=""
restore_node() {
  [ -n "$FAULT_NODE" ] || return 0
  case "$MODE" in
    partition)
      # shellcheck disable=SC2086
      node_on_network "$FAULT_NODE" \
        || docker network connect $ALIASES "$COMPOSE_NET" "caib-spilo${FAULT_NODE}-1" ;;
    *)
      [ "$(docker inspect -f '{{.State.Running}}' "caib-spilo${FAULT_NODE}-1")" = true ] \
        || docker start "caib-spilo${FAULT_NODE}-1" >/dev/null ;;
  esac
}
# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  local rc=$?
  for pid in $INSERTER_PID $PROBE_A_PID $SPLIT_PID; do kill "$pid" 2>/dev/null || true; done
  # A failed run must not leave a two-node cluster or a cut-off node behind.
  if [ -n "$FAULT_NODE" ]; then
    echo "== cleanup: restoring spilo$FAULT_NODE ($MODE)"
    restore_node || echo "   cleanup: could not restore spilo$FAULT_NODE — fix by hand"
    if [ "$rc" != 0 ]; then
      wait_cluster_healthy 180 && echo "   cleanup: cluster healthy again" \
        || echo "   cleanup: WARNING cluster not healthy 180s after restore"
    fi
  fi
  rm -f "$COMMITTED_FILE" "$PROBE_A_FILE" "$SPLIT_LOG"
}
trap cleanup EXIT

echo "== detection probe: key A lands continuously (1/s) from before the fault"
uv run python scripts/ha_detect_probe.py --url "$APP_URL" --run "$RUN_ID" --key A \
  --rate 1 --out "$PROBE_A_FILE" &
PROBE_A_PID=$!
T_PROBE=$EPOCHREALTIME

echo "== pre-failover: $RULE must raise alert A (<=${DETECT_BOUND_S}s)"
while :; do
  STATE_A=$(alert_state A)
  [ -n "$STATE_A" ] && break
  if [ "$(secs "$(elapsed "$T_PROBE" "$EPOCHREALTIME")")" -gt "$DETECT_BOUND_S" ]; then
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

case "$MODE" in
  stop)      echo "== stopping primary spilo$PRIMARY_N (graceful)" ;;
  kill)      echo "== killing primary spilo$PRIMARY_N (docker kill --signal $KILL_SIGNAL)" ;;
  partition) echo "== partitioning primary spilo$PRIMARY_N (docker network disconnect $COMPOSE_NET)" ;;
esac
FAULT_NODE=$PRIMARY_N
T0_RT=$EPOCHREALTIME
case "$MODE" in
  stop)      docker stop "caib-spilo${PRIMARY_N}-1" >/dev/null ;;
  kill)      docker kill --signal "$KILL_SIGNAL" "caib-spilo${PRIMARY_N}-1" >/dev/null ;;
  partition) docker network disconnect "$COMPOSE_NET" "caib-spilo${PRIMARY_N}-1" ;;
esac
T0=$(date +%s)

if [ "$MODE" = partition ]; then
  # Split-brain writer: real INSERTs into the isolated node, one every 0.5s.
  # Logs "<epoch after the attempt returned> ok|fail <primary REST code>".
  (
    while :; do
      if node_sql "$PRIMARY_N" "INSERT INTO public.ha_chaos_split (run) VALUES ('$RUN_ID')" >/dev/null 2>&1; then
        r=ok; else r=fail; fi
      code=$(timeout 3 docker exec "caib-spilo${PRIMARY_N}-1" curl -s -m 2 -o /dev/null -w '%{http_code}' \
        http://localhost:8008/primary 2>/dev/null || echo 000)
      echo "$EPOCHREALTIME $r $code" >>"$SPLIT_LOG"
      sleep 0.5
    done
  ) &
  SPLIT_PID=$!
fi

echo "== waiting for new primary (bound [${PROMO_MIN_S}s, ${PROMO_MAX_S}s])"
NEW_PRIMARY=""
while [ -z "$NEW_PRIMARY" ]; do
  for i in $REPLICAS; do
    if [ "$(patroni_role "$(rest_port "$i")" primary)" = "200" ]; then NEW_PRIMARY=$i; break; fi
  done
  [ -n "$NEW_PRIMARY" ] && break
  if [ $(( $(date +%s) - T0 )) -gt "$PROMO_MAX_S" ]; then
    echo "FAIL: no new primary in ${PROMO_MAX_S}s"
    echo "   diag: cluster members:"; cluster_members | sed 's/^/     /' || true
    [ "$MODE" = partition ] && echo "   diag: isolated-node writes accepted: $(grep -c ' ok ' "$SPLIT_LOG" || true), last sample: $(tail -1 "$SPLIT_LOG")"
    exit 1
  fi
  sleep 0.5
done
T_PROMO_SEEN=$EPOCHREALTIME
# Patroni's own log time for taking the key (earliest possible writability);
# fall back to when we saw /primary answer.
T_PROMO=$(promotion_epoch "$NEW_PRIMARY" "$T0_RT" || true)
[ -n "$T_PROMO" ] || T_PROMO=$T_PROMO_SEEN
ELECTION_S=$(elapsed "$T0_RT" "$T_PROMO")
echo "   new primary: spilo$NEW_PRIMARY took the leader key ${ELECTION_S}s after the fault (/primary 200 seen at $(elapsed "$T0_RT" "$T_PROMO_SEEN")s)"
if [ "$(awk -v e="$ELECTION_S" -v m="$PROMO_MIN_S" 'BEGIN { print (e < m) }')" = 1 ]; then
  fail "promotion after ${ELECTION_S}s is below the hard-failure minimum ${PROMO_MIN_S}s (ttl - loop_wait - 1): the leader key was released, so this was not a hard failure"
fi

if [ "$MODE" = partition ]; then
  echo "== split brain: isolated spilo$PRIMARY_N must refuse writes before spilo$NEW_PRIMARY took the key"
  # Let the writer take a few samples past promotion, then stop it.
  sleep 3
  kill "$SPLIT_PID" 2>/dev/null || true; wait "$SPLIT_PID" 2>/dev/null || true; SPLIT_PID=""
  ACCEPTED=$(grep -c ' ok ' "$SPLIT_LOG" || true)
  LAST_OK=$(awk '$2 == "ok" { t = $1 } END { print t }' "$SPLIT_LOG")
  FIRST_REFUSED=$(awk '$2 == "fail" { print $1; exit }' "$SPLIT_LOG")
  OLD_PRIMARY_200_AFTER=$(awk -v p="$T_PROMO" '$1 >= p && $3 == "200"' "$SPLIT_LOG" | wc -l)
  if [ -n "$LAST_OK" ] && [ "$(awk -v a="$LAST_OK" -v p="$T_PROMO" 'BEGIN { print (a >= p) }')" = 1 ]; then
    fail "SPLIT BRAIN — isolated spilo$PRIMARY_N accepted a write $(elapsed "$T_PROMO" "$LAST_OK")s AFTER spilo$NEW_PRIMARY took the leader key ($ACCEPTED writes accepted while isolated; last sample: $(tail -1 "$SPLIT_LOG"))"
  fi
  [ "$OLD_PRIMARY_200_AFTER" = 0 ] \
    || fail "SPLIT BRAIN — isolated spilo$PRIMARY_N still answered /primary 200 after spilo$NEW_PRIMARY took the key ($OLD_PRIMARY_200_AFTER samples)"
  # And right now, one more direct attempt.
  if node_sql "$PRIMARY_N" "INSERT INTO public.ha_chaos_split (run) VALUES ('$RUN_ID-final')" >/dev/null 2>&1; then
    fail "SPLIT BRAIN — isolated spilo$PRIMARY_N accepts writes with spilo$NEW_PRIMARY promoted"
  fi
  [ -n "$FIRST_REFUSED" ] && DEMOTE_S=$(elapsed "$T0_RT" "$FIRST_REFUSED") || DEMOTE_S="?"
  [ -n "$LAST_OK" ] && LAST_OK_S=$(elapsed "$T0_RT" "$LAST_OK") || LAST_OK_S="none"
  echo "   isolated leader: last write accepted at ${LAST_OK_S}s, refusing from ${DEMOTE_S}s; new leader took the key at ${ELECTION_S}s"
  echo "   $ACCEPTED writes accepted by the isolated leader before it demoted (async: these never reach the cluster)"
fi

echo "== writes must resume through haproxy (<=${RESUME_MAX_S}s from fault)"
RESUME_DEADLINE=$(( T0 + RESUME_MAX_S ))
while true; do
  NOW_LANDING=$(sql "SELECT count(*) FROM ingest.landing WHERE module = 'ha_test'" 2>/dev/null || echo "")
  # DB clock on the new primary: anything stamped after this was written
  # post-failover. Read inside the retry loop: right after promotion HAProxy
  # can still flap, and one good query does not guarantee the next (an
  # unguarded read here killed a run under set -e).
  T_PROMO_DB=$(sql "SELECT now()" 2>/dev/null || echo "")
  if [ -n "$NOW_LANDING" ] && [ -n "$T_PROMO_DB" ]; then break; fi
  [ "$(date +%s)" -gt "$RESUME_DEADLINE" ] && fail "writes did not resume in ${RESUME_MAX_S}s"
  sleep 0.5
done
RESUME_S=$(elapsed "$T0_RT" "$EPOCHREALTIME")
echo "   writes resumed through haproxy ${RESUME_S}s after fault (new-primary clock: $T_PROMO_DB)"

echo "== haproxy routes writes to the new leader only"
UP=""
UP_DEADLINE=$(( $(date +%s) + 30 ))
while [ "$UP" != "pg$NEW_PRIMARY" ]; do
  UP=$(haproxy_primary_up)
  [ "$(date +%s)" -gt "$UP_DEADLINE" ] && fail "haproxy :5000 has UP servers '$UP', expected only pg$NEW_PRIMARY"
  sleep 1
done
SERVED_BY=$(sql "SELECT host(inet_server_addr())" 2>/dev/null || echo "")
[ "$SERVED_BY" = "$(node_ip "$NEW_PRIMARY")" ] \
  || fail "a write session through haproxy:5000 landed on $SERVED_BY, new leader spilo$NEW_PRIMARY is $(node_ip "$NEW_PRIMARY")"
echo "   :5000 UP = $UP only; a session through it is served by spilo$NEW_PRIMARY ($SERVED_BY)"

echo "== post-failover: landing NEW key B ($PROBE_N_B events)"
uv run python scripts/ha_detect_probe.py --url "$APP_URL" --run "$RUN_ID" --key B \
  --count "$PROBE_N_B" \
  || fail "could not land post-failover probe batch B"
T_B_RT=$EPOCHREALTIME
sleep 10  # let some post-failover inserts land

echo "== stopping inserter, draining loader"
kill -TERM "$INSERTER_PID"
wait "$INSERTER_PID" || true
kill -TERM "$PROBE_A_PID"
wait "$PROBE_A_PID" || true
INSERTER_PID=""; PROBE_A_PID=""
COMMITTED_A=$(cat "$PROBE_A_FILE")
COMMITTED=$(cat "$COMMITTED_FILE")
echo "   committed inserts: $COMMITTED"
[ "$COMMITTED" -gt 100 ] || fail "too few inserts ($COMMITTED) to be meaningful"

DRAIN_DEADLINE=$(( $(date +%s) + 120 ))
while [ "$(sql "SELECT count(*) FROM ingest.landing WHERE module IN ('ha_test', 'aws_cloudtrail')")" != "0" ]; do
  [ "$(date +%s)" -gt "$DRAIN_DEADLINE" ] && fail "loader did not drain in 120s"
  sleep 3
done

echo "== loss check: every acknowledged seq 0..$((COMMITTED - 1)) present, nothing duplicated"
COUNT=$(sql "SELECT count(*) FROM caib.events WHERE module = 'ha_test'")
DISTINCT=$(sql "SELECT count(DISTINCT (attrs->>'seq')::int) FROM caib.events WHERE module = 'ha_test'")
[ "$COUNT" = "$DISTINCT" ] || fail "duplicates: $COUNT events but $DISTINCT distinct seqs"
# Missing acknowledged seqs as "count|first|last".
MISSING=$(sql "SELECT count(*) || '|' || coalesce(min(s), -1) || '|' || coalesce(max(s), -1)
  FROM generate_series(0, $COMMITTED - 1) s
  WHERE NOT EXISTS (SELECT 1 FROM caib.events e WHERE e.module = 'ha_test'
                    AND (e.attrs->>'seq')::int = s)")
IFS='|' read -r LOST LOST_FIRST LOST_LAST <<<"$MISSING"
EV_A=$(probe_events A); EV_B=$(probe_events B)
LOST_A=$(( COMMITTED_A - EV_A ))
[ "$EV_A" -le "$COMMITTED_A" ] || fail "probe A events=$EV_A > $COMMITTED_A committed — duplicated across failover"
[ "$EV_B" = "$PROBE_N_B" ] || fail "probe B events=$EV_B, landed $PROBE_N_B (B landed after promotion: nothing may be lost)"
[ "$EV_A" -gt "$PRE_COUNT_A" ] || fail "no key-A events after the pre-failover alert ($EV_A)"
if [ "$MODE" = stop ]; then
  [ "$LOST" = 0 ] || fail "lost commits on a graceful stop: $LOST acknowledged seqs missing ($LOST_FIRST..$LOST_LAST)"
  [ "$LOST_A" = 0 ] || fail "probe A events=$EV_A but lander committed $COMMITTED_A (lost across a graceful stop)"
  echo "   count=$COUNT committed=$COMMITTED — no gaps, no loss; probe A=$EV_A/$COMMITTED_A B=$EV_B/$PROBE_N_B"
else
  # Async replication: a hard failure can lose the WAL tail the replicas had
  # not received. That tail is one contiguous run of seqs; scattered gaps
  # would be a different bug.
  if [ "$LOST" != 0 ] && [ $(( LOST_LAST - LOST_FIRST + 1 )) != "$LOST" ]; then
    fail "$LOST acknowledged seqs missing but not contiguous ($LOST_FIRST..$LOST_LAST): not a lost WAL tail"
  fi
  if [ "$LOST" = 0 ]; then LOST_DESC="0"; else LOST_DESC="$LOST (seqs $LOST_FIRST..$LOST_LAST)"; fi
  echo "   MEASURED (async, not asserted): acknowledged inserter writes lost: $LOST_DESC of $COMMITTED; probe A lost: $LOST_A of $COMMITTED_A"
  echo "   count=$COUNT distinct=$DISTINCT; probe A=$EV_A B=$EV_B/$PROBE_N_B"
fi

echo "== detection continuity: alert B raised, A and B event_count exact (<=${DETECT_BOUND_S}s after B landed)"
while :; do
  STATE_A=$(alert_state A); STATE_B=$(alert_state B)
  CNT_A=${STATE_A%%|*}; CNT_B=${STATE_B%%|*}
  # Over-count is final (counts only grow): a re-run window was double-counted.
  [ -n "$CNT_A" ] && [ "$CNT_A" -gt "$EV_A" ] && fail "alert A event_count=$CNT_A > $EV_A events — detection double-counted a window across the failover"
  [ -n "$CNT_B" ] && [ "$CNT_B" -gt "$EV_B" ] && fail "alert B event_count=$CNT_B > $EV_B events — double-counted"
  [ "$CNT_A" = "$EV_A" ] && [ "$CNT_B" = "$EV_B" ] && break
  if [ "$(secs "$(elapsed "$T_B_RT" "$EPOCHREALTIME")")" -gt "$DETECT_BOUND_S" ]; then
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
[ "$NEW_A" = "1" ] || fail "alert A has $NEW_A 'new' outbox rows, expected exactly 1 (re-notified or never notified)"
[ "$NEW_B" = "1" ] || fail "alert B has $NEW_B 'new' outbox rows, expected exactly 1"
ALERT_FROM_FAULT_S=$(elapsed "$T0_RT" "$OUTBOX_B_EPOCH")
ALERT_FROM_LAND_S=$(elapsed "$T_B_RT" "$OUTBOX_B_EPOCH")
echo "   alert B raised ${ALERT_FROM_FAULT_S}s after the fault, ${ALERT_FROM_LAND_S}s after batch B landed (detect.outbox created_at)"
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
[ "$GRAFANA_DB" = "ok" ] || fail "grafana health=$GRAFANA_DB"
DS_OK=$(curl -s -u "admin:${CAIB_GRAFANA_ADMIN_PASSWORD:-admin}" \
  "http://localhost:${CAIB_GRAFANA_PORT:-3000}/api/datasources/uid/caib/health" \
  | uv run python -c "import json,sys; print(json.load(sys.stdin).get('status'))")
[ "$DS_OK" = "OK" ] || fail "datasource health=$DS_OK"

echo "== timescale jobs follow the new primary"
JOBS=$(sql "SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name IN ('policy_compression','policy_retention')")
[ "$JOBS" -ge 2 ] || fail "expected compression+retention jobs, got $JOBS"

echo "== old primary rejoins as a streaming replica on the new timeline"
T_RESTORE=$(date +%s)
restore_node
# /replica 200 alone is not "rejoined": a partitioned leader demotes to a
# read-only replica of its OWN timeline and answers 200 before pg_rewind runs.
# Rejoined = the cluster is back to 1 leader + 2 replicas streaming on the
# leader's timeline with no lag.
wait_cluster_healthy 180 || {
  echo "FAIL: spilo$PRIMARY_N did not rejoin as a streaming replica on the new timeline in 180s"
  docker logs --tail 20 "caib-spilo${PRIMARY_N}-1" 2>&1 | sed 's/^/   diag: /'; exit 1; }
REJOIN_S=$(( $(date +%s) - T_RESTORE ))
FAULT_NODE=""
TL_NEW=$(node_timeline "$NEW_PRIMARY"); TL_OLD=$(node_timeline "$PRIMARY_N")
[ "$TL_OLD" = "$TL_NEW" ] && [ "$TL_NEW" -gt "$TL_BEFORE" ] \
  || fail "timelines: before=$TL_BEFORE new leader=$TL_NEW rejoined node=$TL_OLD"
REJOIN_HOW=$(docker logs --since "$T_RESTORE" "caib-spilo${PRIMARY_N}-1" 2>&1 \
  | grep -o -m1 -E 'running pg_rewind|reinitializ|bootstrap.*replica' || true)
case "$REJOIN_HOW" in
  "running pg_rewind") REJOIN_HOW="pg_rewind" ;;
  "") REJOIN_HOW="plain restart (WAL already a prefix of the new timeline)" ;;
  *) REJOIN_HOW="reinit ($REJOIN_HOW)" ;;
esac
# The old node must replay the new leader's WAL past a point taken after the
# rejoin: it follows the new timeline, not its own.
LEADER_LSN=$(node_sql "$NEW_PRIMARY" "SELECT pg_current_wal_lsn()") || fail "could not read the new leader's LSN"
CATCHUP_DEADLINE=$(( $(date +%s) + 30 ))
until [ "$(node_sql "$PRIMARY_N" "SELECT pg_last_wal_replay_lsn() >= '$LEADER_LSN'::pg_lsn" 2>/dev/null)" = t ]; do
  [ "$(date +%s)" -gt "$CATCHUP_DEADLINE" ] && fail "rejoined spilo$PRIMARY_N did not replay the new leader's WAL to $LEADER_LSN in 30s"
  sleep 1
done
echo "   spilo$PRIMARY_N rejoined in ${REJOIN_S}s via $REJOIN_HOW; timeline $TL_BEFORE -> $TL_NEW on all nodes; replayed the new leader past $LEADER_LSN"
if [ "$MODE" = partition ]; then
  for i in 1 2 3; do
    LEFT=$(node_sql "$i" "SELECT count(*) FROM public.ha_chaos_split WHERE run LIKE '$RUN_ID%'") \
      || fail "could not read the split-brain probe table on spilo$i"
    [ "$LEFT" = 0 ] || fail "DIVERGENT TIMELINE ACCEPTED — spilo$i has $LEFT rows the isolated leader wrote"
  done
  echo "   the $ACCEPTED rows the isolated leader accepted are on no node: its divergent WAL was rewound"
  node_sql "$NEW_PRIMARY" "DROP TABLE IF EXISTS public.ha_chaos_split" >/dev/null || true
fi

echo "PASS [$MODE]: promotion ${ELECTION_S}s (bound ${PROMO_MIN_S}-${PROMO_MAX_S}s), writes resumed ${RESUME_S}s, $COUNT events, lost acknowledged writes: ${LOST} (probe A: ${LOST_A}), grafana up, jobs on new primary, node rejoined via $REJOIN_HOW in ${REJOIN_S}s"
echo "PASS [$MODE]: detection continuity: alert B ${ALERT_FROM_FAULT_S}s after fault (${ALERT_FROM_LAND_S}s after landing), A $PRE_COUNT_A -> $CNT_A, counts exact, heartbeats advanced"
[ "$MODE" = partition ] && echo "PASS [$MODE]: no split brain: isolated leader's last accepted write ${LAST_OK_S}s, refusing from ${DEMOTE_S}s, new leader at ${ELECTION_S}s; $ACCEPTED isolated writes discarded on rejoin"
exit 0
