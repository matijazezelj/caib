# shellcheck shell=bash
# Shared helpers for the HA tests (sourced by scripts/ha_failover_test.sh).
# Expects APP_URL, RUN_ID and RULE to be set by the caller.

COMPOSE_NET=caib_default

sql() { uv run python -c "
import psycopg, sys
with psycopg.connect('$APP_URL', connect_timeout=5) as c:
    cur = c.execute(sys.argv[1])
    if cur.description:
        r = cur.fetchone()
        print(r[0] if r else '')
" "$1"; }

# psql on one node over its local socket (pg_hba: local trust) — bypasses
# HAProxy and the docker network, so it works on a partitioned node too.
node_sql() { # $1 = node number, $2 = sql
  timeout 5 docker exec "caib-spilo$1-1" psql -d caib -U postgres -qAtX -c "$2"
}

elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }
secs() { cut -d. -f1 <<<"$1"; }  # integer part of an elapsed value

FAILED=""
fail() { FAILED=1; echo "FAIL: $*"; exit 1; }
# Unguarded command failures (set -e) and signals would otherwise exit with a
# traceback or nothing at all; make every non-zero exit carry a FAIL line.
on_err() { # $1 = line, $2 = status
  [ "$BASH_SUBSHELL" = 0 ] || return 0
  [ -n "$FAILED" ] && return 0
  FAILED=1; echo "FAIL: command failed (exit $2) at ${BASH_SOURCE[1]:-$0}:$1: ${BASH_COMMAND}"
}

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

# Cluster/HAProxy/detect state, printed by the EXIT trap after any failure.
failure_diag() {
  echo "   diag: cluster members:"; cluster_members 2>/dev/null | sed 's/^/     /' || echo "     (no REST endpoint answers)"
  echo "   diag: haproxy :5000 UP: $(haproxy_primary_up 2>/dev/null || echo '?')"
  detect_diag
}

patroni_role() { # $1 = rest port, $2 = role path
  curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1/$2" 2>/dev/null || echo 000
}

rest_port() { echo $((8007 + $1)); }

find_primary() {
  for i in 1 2 3; do
    if [ "$(patroni_role "$(rest_port "$i")" primary)" = "200" ]; then echo "$i"; return 0; fi
  done
  return 1
}

# "member role state timeline lag" per line, from any reachable node's view.
cluster_members() {
  for i in 1 2 3; do
    curl -s -m 2 "http://127.0.0.1:$(rest_port "$i")/cluster" 2>/dev/null | python3 -c "
import json, sys
for m in json.load(sys.stdin)['members']:
    print(m['name'], m['role'], m['state'], m.get('timeline', '?'), m.get('lag', 0))
" 2>/dev/null && return 0
  done
  return 1
}

# One leader, two streaming replicas, all on the leader's timeline, no lag.
cluster_healthy() {
  cluster_members | python3 -c "
import sys
rows = [l.split() for l in sys.stdin if l.strip()]
leaders = [r for r in rows if r[1] == 'leader' and r[2] == 'running']
reps = [r for r in rows if r[1] == 'replica' and r[2] == 'streaming']
ok = (len(rows) == 3 and len(leaders) == 1 and len(reps) == 2
      and all(r[3] == leaders[0][3] for r in reps) and all(r[4] == '0' for r in reps))
sys.exit(0 if ok else 1)
"
}

wait_cluster_healthy() { # $1 = deadline seconds
  local t0=$SECONDS
  until cluster_healthy; do
    if [ $((SECONDS - t0)) -gt "$1" ]; then
      echo "   diag: cluster members:"; cluster_members | sed 's/^/     /' || echo "     (no REST endpoint answers)"
      return 1
    fi
    sleep 2
  done
}

patroni_dcs() { # $1 = key in Patroni's dynamic config (ttl, loop_wait, ...)
  for i in 1 2 3; do
    curl -s -m 2 "http://127.0.0.1:$(rest_port "$i")/config" 2>/dev/null \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])" 2>/dev/null && return 0
  done
  return 1
}

node_timeline() { # $1 = node number
  curl -s -m 2 "http://127.0.0.1:$(rest_port "$1")/patroni" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('timeline', ''))" 2>/dev/null
}

# Servers HAProxy's write listener (:5000) considers UP, e.g. "pg2".
haproxy_primary_up() {
  curl -s -m 2 'http://127.0.0.1:7000/;csv' \
    | awk -F, '$1 == "primary" && $2 ~ /^pg[0-9]$/ && $18 == "UP" { print $2 }' | paste -sd' '
}

node_ip() { # $1 = node number
  docker inspect -f "{{(index .NetworkSettings.Networks \"$COMPOSE_NET\").IPAddress}}" "caib-spilo$1-1" 2>/dev/null
}

node_on_network() { # $1 = node number
  [ -n "$(node_ip "$1")" ]
}

# Epoch seconds (with ms) of Patroni's "promoted self to leader" on node $1 at
# or after epoch $2 — Patroni logs it right after acquiring the leader key, so
# it is the earliest moment the node could be writable.
promotion_epoch() {
  docker logs --since "$(date -u -d "@$(secs "$2")" +%Y-%m-%dT%H:%M:%SZ)" "caib-spilo$1-1" 2>&1 \
    | grep -m1 -E 'promoted self to leader' \
    | sed -E 's/^([0-9-]+ [0-9:]+),([0-9]+).*/\1.\2 UTC/' \
    | { read -r ts && date -d "$ts" +%s.%N | cut -c1-14; }
}
