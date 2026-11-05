.PHONY: install fmt lint typecheck test check pre-pr openapi

install:
	uv sync --all-packages
	uv run pre-commit install --hook-type pre-commit --hook-type pre-push

fmt:
	uv run ruff format
	uv run ruff check --fix

lint:
	uv run ruff format --check
	uv run ruff check

typecheck:
	uv run mypy libs/*/src labs/*/src

test:
	uv run pytest

openapi:
	uv run python labs/03-orders/scripts/export_openapi.py

check: lint typecheck test
pre-pr: fmt lint typecheck openapi
