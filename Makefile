COMPOSE ?= docker compose
COMPOSE_HA ?= docker compose -f docker-compose.yml -f compose.ha.yml

.PHONY: up down logs migrate test demo bench ha-up ha-down ha-logs ha-failover-test new-module

.env:
	cp .env.example .env
	@echo "Created .env from .env.example (dev defaults). Change passwords for anything real."

up: .env
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

migrate: .env
	$(COMPOSE) run --rm migrate

test:
	uv run ruff check .
	uv run pytest -q

bench:
	uv run python scripts/bench.py

demo: .env
	@set -a; . ./.env; set +a; uv run python scripts/demo_seed.py

ha-up: .env
	$(COMPOSE) down --remove-orphans
	$(COMPOSE_HA) up -d --build

ha-down:
	$(COMPOSE_HA) down --remove-orphans

ha-logs:
	$(COMPOSE_HA) logs -f --tail=200

ha-failover-test:
	bash scripts/ha_failover_test.sh

new-module:
	@echo "$@: not built yet — see docs/NEW_MODULE_HOWTO.md for the manual recipe"; exit 1
