#!/usr/bin/env bash
# Create .env from .env.example, replacing every shipped password with a
# generated one. The example values are placeholders for reading, never for
# running: the previous `cp` meant the documented first run produced a stack
# whose passwords are published in this repo.
set -eu

cd "$(dirname "$0")/.."

if [ -e .env ]; then
  echo ".env already exists — leaving it alone."
  exit 0
fi

# od+tr: no SIGPIPE games, POSIX everywhere. 32 hex chars.
gen() { od -An -tx1 -N16 /dev/urandom | tr -d ' \n'; }

cp .env.example .env
for var in CAIB_PG_SUPERPASS CAIB_PG_APP_PASSWORD CAIB_PG_RO_PASSWORD \
           CAIB_HA_REPLICATION_PASSWORD CAIB_GRAFANA_ADMIN_PASSWORD; do
  sed -i.bak "s|^${var}=.*|${var}=$(gen)|" .env
done
rm -f .env.bak

echo "Created .env from .env.example with generated passwords."
echo "Grafana admin login: admin / $(grep '^CAIB_GRAFANA_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
echo "Services bind to \$CAIB_BIND_ADDR (default 127.0.0.1) — see docs/DEPLOY.md."
