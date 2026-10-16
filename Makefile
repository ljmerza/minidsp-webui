COMPOSE_DEV := docker compose -f docker-compose.dev.yml

.PHONY: help dev-up dev-down dev-logs lint format test test-integration

help:
	@echo "dev-up            build and start the dev stack (mock minidspd on 127.0.0.1:15380, webui on 127.0.0.1:8480)"
	@echo "dev-down          stop and remove the dev stack"
	@echo "dev-logs          follow the dev stack's logs"
	@echo "lint              ruff check + format check"
	@echo "format            ruff format + import sorting"
	@echo "test              unit tests (no docker needed)"
	@echo "test-integration  integration tests against the dev stack (starts/stops it if it isn't running)"

dev-up:
	mkdir -p .devdata
	$(COMPOSE_DEV) up -d --build --wait

dev-down:
	$(COMPOSE_DEV) down --remove-orphans

dev-logs:
	$(COMPOSE_DEV) logs -f --tail=200

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --select I --fix .
	uv run ruff format .

test:
	uv run pytest

test-integration:
	scripts/test-integration.sh
