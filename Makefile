.PHONY: setup run test clean

setup:
	python3 setup.py

run:
	python3 run.py

# Pure unit tests for the leaf modules. No deps, no venv, no services.
test:
	python3 tests/test_relquality.py
	python3 tests/test_race_rules.py
	python3 tests/test_tmdbcache.py

clean:
	rm -rf .venv __pycache__ .env
	find . -name "*.pyc" -delete
