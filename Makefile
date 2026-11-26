PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
MINUTES ?= 30

.PHONY: venv install test test-offline record-crypto record-bars pull-equities run ui

venv:
	python3.11 -m venv .venv

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

test-offline:
	$(PY) -m pytest -m "not network"

record-crypto:   # Phase 2 (Crypto agent): Coinbase websocket recorder
	$(PY) -m flow_model.data.recorders.coinbase_ws --minutes $(MINUTES)

record-bars:     # Phase 2 (Commodities agent): append new yfinance 1m bars; run once a day
	$(PY) -m flow_model.data.recorders.bar_appender

pull-equities:   # Equities: last full RTH day of IEX trades+quotes (needs Alpaca keys in env)
	$(PY) -m flow_model.data.loaders.alpaca pull

run:             # Phase 4/7: full pipeline
	$(PY) run_all.py

ui:              # Phase 6
	$(PY) -m streamlit run flow_model/ui/app.py
