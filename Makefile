.PHONY: up down logs test backend-test frontend-test migrate seed shell-backend shell-frontend contract-gen contract-test
up:      ; docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
down:    ; docker compose down -v
logs:    ; docker compose logs -f --tail=200
migrate: ; docker compose exec backend alembic upgrade head
seed:    ; docker compose exec backend python -m superconductor_db.ingest.supercon_seed
backend-test: ; docker compose exec backend pytest --cov=superconductor_db --cov-report=term-missing
frontend-test:; docker compose exec frontend npm run test:coverage
test: backend-test frontend-test
shell-backend: ; docker compose exec backend bash
shell-frontend:; docker compose exec frontend sh
contract-gen: ; cd backend && PYTHONPATH=src python scripts/generate_contract_schemas.py --out ../contracts
contract-test: ; python contracts/diff.py --verbose