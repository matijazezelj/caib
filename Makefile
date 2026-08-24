COMPOSE ?= docker compose

.PHONY: up down logs migrate test demo bench ha-up ha-failover-test new-module

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

ha-up ha-failover-test new-module:
	@echo "$@: not built yet — see Build phases in caib-build-spec.md"; exit 1
