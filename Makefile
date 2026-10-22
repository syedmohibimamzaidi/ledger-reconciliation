.PHONY: help venv setup test test-postgres migrate demo-ingestion eval eval-easy eval-sroie eval-test data clean

PYTHON ?= 3.12
VENV   := .venv
BIN    := $(VENV)/bin
STAMP  := $(VENV)/.deps

# Prefer uv when present: standalone Python builds avoid the system dylib
# breakage that Homebrew Python is prone to on macOS.
UV := $(shell command -v uv 2>/dev/null)

# Strip inherited interpreter config so results don't depend on whose shell
# ran them.
ENV := env -u PYTHONSTARTUP -u PYTHONHOME -u PYTHONPATH PYTHONNOUSERSITE=1
PY  := $(ENV) PYTHONPATH=src $(BIN)/python

$(BIN)/python:
ifdef UV
	$(ENV) uv venv --python $(PYTHON) $(VENV)
else
	$(ENV) python$(PYTHON) -m venv $(VENV)
endif

venv: $(BIN)/python

# Stamp file: dependencies reinstall only when requirements.txt changes,
# instead of on every make invocation.
$(STAMP): requirements.txt | $(BIN)/python
ifdef UV
	$(ENV) uv pip install --python $(BIN)/python -r requirements.txt
else
	$(ENV) $(BIN)/python -m pip install --upgrade pip
	$(ENV) $(BIN)/python -m pip install -r requirements.txt
endif
	@touch $(STAMP)

setup: $(STAMP)

data: setup
	$(PY) scripts/get_sroie.py

test: setup
	$(PY) -m pytest tests/ -q

test-postgres: setup
	@test -n "$$TEST_DATABASE_URL" || (echo 'Set TEST_DATABASE_URL first'; exit 1)
	$(PY) -m pytest tests/test_ingestion.py -q

migrate: setup
	$(PY) -m alembic upgrade head

demo-ingestion: setup
	$(PY) scripts/demo_ingestion.py

eval: setup
	$(PY) -m ledger.eval.run_eval --corpus synthetic --split dev

eval-easy: setup
	$(PY) -m ledger.eval.run_eval --corpus synthetic --split dev --difficulty easy

eval-sroie: setup
	$(PY) -m ledger.eval.run_eval --corpus sroie --split dev

eval-test: setup
	$(PY) -m ledger.eval.run_eval --corpus sroie --split test

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
