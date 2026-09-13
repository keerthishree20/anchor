# Anchor. `make install && make test`

PY      ?= .venv/bin/python
VENV_PY ?= python3.12          # NOT python3: that is 3.6 on some machines

.PHONY: help install test test-fast test-crash bench bench-recovery demo clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

install: ## create .venv and install pytest (the store itself has no dependencies)
	$(VENV_PY) -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements-dev.txt

test: ## the whole suite, crash tests included
	$(PY) -m pytest -q

test-fast: ## everything except the tests that kill processes
	$(PY) -m pytest -q --ignore=tests/test_crash.py

test-crash: ## only the tests that kill processes
	$(PY) -m pytest -q tests/test_crash.py

bench: ## writes at each fsync setting, reads, compaction
	$(PY) -m bench.throughput --records 50000 --value-bytes 200 --fsync-records 3000

bench-recovery: ## reopen time with and without hint files
	$(PY) -m bench.recovery --sizes 10000,50000,200000 --value-bytes 200
	$(PY) -m bench.recovery --sizes 20000,60000 --value-bytes 4000 --segment-bytes 33554432
	$(PY) -m bench.recovery --sizes 10000,30000 --value-bytes 16000 --segment-bytes 67108864

demo: ## a store you can poke at
	$(PY) -m anchor.cli --path ./anchor-data put greeting hello
	$(PY) -m anchor.cli --path ./anchor-data put farewell goodbye
	$(PY) -m anchor.cli --path ./anchor-data list --values
	$(PY) -m anchor.cli --path ./anchor-data stats

clean:
	rm -rf .venv .pytest_cache anchor-data **/__pycache__
