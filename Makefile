.PHONY: help install up down logs test lint format type check ci clean bootstrap

help:
	@echo "Poly Meridian — make targets"
	@echo ""
	@echo "  install     Install Python deps via uv (incl. dev extras)"
	@echo "  up          docker compose up -d (db, redis, agent, prometheus, grafana)"
	@echo "  down        Stop all services"
	@echo "  logs        Tail agent logs"
	@echo "  bootstrap   Run DB bootstrap SQL against running db service"
	@echo "  test        Run pytest"
	@echo "  lint        Run ruff"
	@echo "  format      Run black"
	@echo "  type        Run mypy --strict"
	@echo "  check       lint + type + test"
	@echo "  ci          Everything CI runs"
	@echo "  clean       Remove caches and build artifacts"

install:
	uv pip install -e ".[dev,polymarket]"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f agent

bootstrap:
	docker compose exec -T db psql -U poly -d poly_meridian < scripts/bootstrap_db.sh || true

test:
	pytest

lint:
	ruff check src tests

format:
	black src tests
	ruff check --fix src tests

type:
	mypy

check: lint type test

ci: check

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
