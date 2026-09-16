.PHONY: install test run run-prod worker migrate

install:
	python -m pip install -e '.[dev]'

test:
	pytest -q

run:
	AGENTFORGE_ENV=development AGENTFORGE_AUTO_CREATE_SCHEMA=true uvicorn agentforge_server.app:app --app-dir server --host 0.0.0.0 --port 8080

run-prod:
	AGENTFORGE_ENV=production AGENTFORGE_AUTO_CREATE_SCHEMA=false alembic upgrade head
	AGENTFORGE_ENV=production AGENTFORGE_AUTO_CREATE_SCHEMA=false uvicorn agentforge_server.app:app --app-dir server --host 0.0.0.0 --port 8080

migrate:
	alembic upgrade head

worker:
	python -m agentforge_server.worker
