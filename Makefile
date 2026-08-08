.PHONY: help venv setup test eval eval-easy eval-sroie eval-test data clean

# Python used to create the venv. Override: make venv PYTHON=python3.11
PYTHON ?= python3.12
VENV   := .venv
BIN    := $(VENV)/bin

# Strip inherited interpreter config before running anything. Editors and
# shell integrations inject PYTHONSTARTUP/PYTHONHOME/PYTHONPATH, which break
# venv creation and make results depend on whose terminal you're in.
ENV := env -u PYTHONSTARTUP -u PYTHONHOME -u PYTHONPATH PYTHONNOUSERSITE=1
PY  := $(ENV) PYTHONPATH=src $(BIN)/python

help:
	@echo "venv        create .venv with $(PYTHON)"
	@echo "setup       venv + install dependencies"
	@echo "test        run the test suite"
	@echo "eval        baseline on synthetic/dev (no download needed)"
	@echo "eval-easy   zero-corruption sanity floor"
	@echo "eval-sroie  baseline on sroie/dev (needs make data)"
	@echo "eval-test   OPEN THE TEST SPLIT - end of phase only"
	@echo "data        download SROIE into data/sroie"

$(BIN)/python:
	$(ENV) $(PYTHON) -m venv $(VENV)

venv: $(BIN)/python

setup: venv
	$(ENV) $(BIN)/python -m pip install --upgrade pip
	$(ENV) $(BIN)/python -m pip install -r requirements.txt

data: setup
	$(PY) scripts/get_sroie.py

test: setup
	$(PY) -m pytest tests/ -q

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
