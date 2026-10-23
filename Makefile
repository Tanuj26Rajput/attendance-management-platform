.PHONY: install dev-install run dashboard test lint format migrate revision seed \
	docker-up docker-down prod-build prod-migrate prod-up prod-down prod-logs

install:
	python -m pip install -r requirements.txt

dev-install:
	python -m pip install -r requirements-dev.txt

run:
	uvicorn app.main:app --reload --port 8000

dashboard:
	streamlit run dashboard/app.py

test:
	pytest

lint:
	ruff check . && mypy app

format:
	ruff format . && ruff check --fix .

revision:
	alembic revision --autogenerate -m "$(m)"

migrate:
	alembic upgrade head

# Demo data. Never wired into migrate or into application startup: it refuses
# to run outside APP_ENV=local/dev/test, and refuses again without --yes.
seed:
	python -m app.seed --yes

docker-up:
	docker compose up --build

docker-down:
	docker compose down -v

# --- production ---------------------------------------------------------
# prod-migrate is a separate target on purpose: it must run once per deploy,
# before prod-up, never from inside an application replica.
prod-build:
	docker compose -f docker-compose.prod.yml build

prod-migrate:
	docker compose -f docker-compose.prod.yml --profile migrate run --rm migrate

prod-up:
	docker compose -f docker-compose.prod.yml up -d

prod-down:
	docker compose -f docker-compose.prod.yml down

prod-logs:
	docker compose -f docker-compose.prod.yml logs -f
