.PHONY: run test lint

run:
	PYTHONPATH=src python -m uvicorn intern_agent.api.app:app --reload --port 8000

test:
	pytest -q

lint:
	ruff check src tests
