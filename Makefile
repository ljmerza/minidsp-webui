COMPOSE_DEV := docker compose -f docker-compose.dev.yml
EMBEDDED_IMAGE ?= minidsp-webui:embedded-test

.PHONY: help dev-up dev-up-embedded dev-down dev-logs lint format test test-integration image test-embedded e2e-setup test-e2e

help:
	@echo "dev-up            build and start the dev stack (mock minidspd on 127.0.0.1:15380, webui on 127.0.0.1:8480)"
	@echo "dev-up-embedded   build and start the all-in-one dev UI (bundled minidspd + mock device) on 127.0.0.1:8481"
	@echo "dev-down          stop and remove the dev stack (both variants)"
	@echo "dev-logs          follow the dev stack's logs"
	@echo "lint              ruff check + format check"
	@echo "format            ruff format + import sorting"
	@echo "test              unit tests (no docker needed)"
	@echo "test-integration  integration tests against the dev stack (starts/stops it if it isn't running)"
	@echo "image             build the production image as \$$(EMBEDDED_IMAGE) for linux/amd64"
	@echo "test-embedded     build the image and run it in embedded+mock mode (tests/integration/test_embedded.py)"
	@echo "e2e-setup         install Chromium for Playwright and download minidspd for the browser tests (once)"
	@echo "test-e2e          browser tests (Playwright) against the app with minidspd's mock device (tests/e2e)"

dev-up:
	mkdir -p .devdata
	$(COMPOSE_DEV) up -d --build --wait

dev-up-embedded:
	mkdir -p .devdata
	$(COMPOSE_DEV) --profile embedded up -d --build --wait webui-embedded

dev-down:
	$(COMPOSE_DEV) --profile embedded down --remove-orphans

dev-logs:
	$(COMPOSE_DEV) --profile embedded logs -f --tail=200

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

image:
	docker build --platform linux/amd64 -t $(EMBEDDED_IMAGE) .

test-embedded: image
	EMBEDDED_IMAGE=$(EMBEDDED_IMAGE) uv run pytest -m integration tests/integration/test_embedded.py -rfxX

e2e-setup:
	uv run playwright install chromium
	scripts/fetch-minidspd.sh

test-e2e:
	uv run pytest -m e2e tests/e2e -n $${E2E_WORKERS:-4} --dist loadfile
