.PHONY: install format lint type test security docs benchmark check run seed

install:
	python -m pip install -e '.[dev]'

format:
	ruff format .
	ruff check --fix .

lint:
	ruff check .
	ruff format --check .

type:
	mypy src

test:
	pytest --cov --cov-branch --cov-report=term-missing

security:
	python scripts/scan_secrets.py

docs:
	python scripts/check_docs.py

benchmark:
	python scripts/run_benchmark.py --requests 250 --concurrency 25 --assert-slo

check: lint type test security docs benchmark

run:
	aegis --reload

seed:
	python scripts/seed_demo.py
