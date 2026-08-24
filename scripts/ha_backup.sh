#!/usr/bin/env bash
# Leader-aware HA backup: asks each node's Patroni REST API who the primary is
# and runs pgbackrest there. spilo1 is NOT reliably the leader — never hardcode
# a node in cron. Usage (host cron): scripts/ha_backup.sh full|incr
set -euo pipefail
TYPE="${1:-incr}"

for n in 1 2 3; do
  port=$((8007 + n))
  # /primary returns 200 only on the current leader.
  if curl -sf -o /dev/null "http://127.0.0.1:${port}/primary" 2>/dev/null; then
    echo "primary is spilo${n}; running ${TYPE} backup"
    exec docker exec -u postgres "caib-spilo${n}-1" \
      pgbackrest --stanza=caib backup --type="${TYPE}"
  fi
done

echo "no Patroni primary found on ports 8008-8010" >&2
exit 1
