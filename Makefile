.PHONY: install fmt lint typecheck test check lint-check

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

check: lint typecheck test
lint-check: fmt lint typecheck
