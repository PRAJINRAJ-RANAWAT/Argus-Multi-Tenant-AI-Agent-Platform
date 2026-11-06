.PHONY: install lint fmt typecheck test check up down logs migrate

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

down:
	docker compose down

logs:
	docker compose logs -f

migrate:
	uv run argus-migrate
