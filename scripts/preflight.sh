#!/usr/bin/env bash
# Refuse to publish a stack that still carries the shipped example passwords on
# a non-loopback address. Defaults behind 127.0.0.1 are a dev convenience;
# defaults on a routable interface are an incident waiting to happen, and the
# ":?set in .env" guards in compose only catch *unset*, never *unchanged*.
#
# Escape hatch for a deliberate lab: CAIB_ALLOW_DEFAULT_SECRETS=1.
set -eu

cd "$(dirname "$0")/.."

ENV_FILE=${CAIB_ENV_FILE:-.env}
[ -f "$ENV_FILE" ] || exit 0

value_of() { grep "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- || true; }

SECRETS="CAIB_PG_SUPERPASS CAIB_PG_APP_PASSWORD CAIB_PG_RO_PASSWORD
         CAIB_HA_REPLICATION_PASSWORD CAIB_GRAFANA_ADMIN_PASSWORD"

unchanged=""
for var in $SECRETS; do
  example=$(value_of "$var" .env.example)
  current=$(value_of "$var" "$ENV_FILE")
  if [ -n "$example" ] && [ "$current" = "$example" ]; then
    unchanged="$unchanged $var"
  fi
done

[ -n "$unchanged" ] || exit 0

bind=$(value_of CAIB_BIND_ADDR "$ENV_FILE")
case "${bind:-127.0.0.1}" in
  127.0.0.1|localhost|::1) loopback=yes ;;
  *) loopback=no ;;
esac

echo "warning: $ENV_FILE still uses the example password for:$unchanged" >&2

if [ "$loopback" = yes ] || [ "${CAIB_ALLOW_DEFAULT_SECRETS:-}" = "1" ]; then
  echo "         (published on ${bind:-127.0.0.1} only — fine for a dev box)" >&2
  exit 0
fi

cat >&2 <<MSG

refusing to start: those passwords are published in this repository and
CAIB_BIND_ADDR=${bind} exposes the stack beyond loopback.

  fix:      edit $ENV_FILE and set new values (rm $ENV_FILE && make .env regenerates)
  override: CAIB_ALLOW_DEFAULT_SECRETS=1 make up

MSG
exit 1
