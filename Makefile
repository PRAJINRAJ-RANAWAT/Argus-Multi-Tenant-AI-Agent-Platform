.PHONY: demo demo-down install lint fmt typecheck test check up down logs migrate

install:
	uv sync
	git config core.hooksPath .githooks

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:
	uv run mypy

test:
	uv run pytest -q

check: lint typecheck test

up:
	docker compose up -d --build

DEMO = docker compose -f docker-compose.yml -f docker-compose.observability.yml -f docker-compose.demo.yml

demo:  ## local demo: full stack + observability + fake GitHub, seeded with PRs
	$(DEMO) up -d --build
	@sleep 8
	uv run argus-demo

demo-down:
	$(DEMO) down

down:
	docker compose down

logs:
	docker compose logs -f

migrate:
	uv run argus-migrate
