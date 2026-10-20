.PHONY: setup run test surface clean

setup:
	python3 setup.py

run:
	python3 run.py

# Pure unit tests for the leaf modules. No deps, no venv, no services.
test:
	python3 tests/test_relquality.py
	python3 tests/test_race_rules.py
	python3 tests/test_tmdbcache.py
	python3 tests/test_tmdb_retention.py
	python3 tests/test_animemap.py
	python3 tests/test_http_clients.py
	python3 tests/test_reltracks.py
	python3 tests/test_watchrule.py
	python3 tests/test_bundlecheck.py
	python3 tests/test_srcevict.py
	python3 tests/test_subsearch.py
	python3 tests/test_packslice.py
	python3 tests/test_stallrule.py
	python3 tests/test_gpugate.py
	python3 tests/test_hdrmap.py
	python3 tests/test_ixwait.py
	python3 tests/test_indexer_tests.py
	python3 tests/test_race_rescue.py
	python3 tests/test_od_revive.py
	python3 tests/test_reaper.py
	python3 tests/test_clientlog.py
	python3 tests/test_eplabel.py
	python3 tests/test_epgroups.py
	python3 tests/test_seasonfolders.py
	python3 tests/test_titleslot.py
	python3 tests/test_retry_candidates.py
	python3 tests/test_watchpurge.py
	python3 tests/test_delete_locked.py
	python3 tests/test_prep_gate.py
	python3 tests/test_prep_space.py
	python3 tests/test_error_heal.py
	python3 tests/test_storage_volumes.py
	python3 tests/test_devactivity.py
	python3 tests/test_bookmarks.py
	python3 tests/test_diagnostics.py
	python3 tests/test_lib_io_timing.py
	python3 tests/test_analyzer_fp.py
	python3 tests/test_discovery.py
	python3 tests/test_appchannel.py
	python3 tests/test_voicestatus.py
	python3 tests/test_voicepick.py
	python3 tests/test_backend_layout.py

# The backend's outside surface (routes in order, request models, every name
# main exports) against tests/fixtures/main_surface.json. Imports main, so it
# needs the venv. Run it after every move while main.py is split into backend/.
surface:
	.venv/bin/python tests/surface_snapshot.py

clean:
	rm -rf .venv __pycache__ .env
	find . -name "*.pyc" -delete
