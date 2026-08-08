.PHONY: help setup test eval eval-sroie eval-test lint clean

PY := PYTHONPATH=src python3

help:
	@echo "setup       install deps"
	@echo "test        run the test suite"
	@echo "eval        baseline on synthetic/dev  (works with no data download)"
	@echo "eval-sroie  baseline on sroie/dev      (needs make data)"
	@echo "eval-test   OPEN THE TEST SPLIT - end of phase only"
	@echo "data        download SROIE into data/sroie"

setup:
	pip install -r requirements.txt

data:
	$(PY) scripts/get_sroie.py

test:
	$(PY) -m pytest tests/ -q

eval:
	$(PY) -m ledger.eval.run_eval --corpus synthetic --split dev

eval-easy:
	$(PY) -m ledger.eval.run_eval --corpus synthetic --split dev --difficulty easy

eval-sroie:
	$(PY) -m ledger.eval.run_eval --corpus sroie --split dev

eval-test:
	$(PY) -m ledger.eval.run_eval --corpus sroie --split test

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
