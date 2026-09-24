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
	python3 tests/test_animemap.py
	python3 tests/test_http_clients.py
	python3 tests/test_watchrule.py
	python3 tests/test_bundlecheck.py
	python3 tests/test_srcevict.py
	python3 tests/test_subsearch.py
	python3 tests/test_packslice.py
	python3 tests/test_reaper.py
	python3 tests/test_clientlog.py
	python3 tests/test_eplabel.py

clean:
	rm -rf .venv __pycache__ .env
	find . -name "*.pyc" -delete
