.PHONY: up down logs test backend-test migrate seed shell-backend
up:      ; docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
down:    ; docker compose down -v
logs:    ; docker compose logs -f --tail=200
migrate: ; docker compose exec worker alembic upgrade head
seed:    ; docker compose exec worker python -m superconductor_db.ingest.supercon_seed
backend-test: ; docker compose exec worker pytest --cov=superconductor_db --cov-report=term-missing
test: backend-test
shell-backend: ; docker compose exec worker bash
