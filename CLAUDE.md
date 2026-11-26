# CLAUDE.md — Order Flow & Price Impact project (project memory)

> **Read this file and your own `docs/notes/<agent>.md` before doing anything.**
> After context compression or in a new session, re-read both. Trust the files over recollection.
> If it isn't written here or in `docs/notes/`, assume it will be forgotten.

---

## 1. Project goal

Build an order-flow and price-impact model inspired by fluid flow (order flow pushes price) and
elastic recovery (liquidity refills after a shock). Measure: how signed trading pressure moves prices
(OFI, Kyle's λ, square-root law), how fast liquidity recovers (resilience ρ, propagator kernel), and the
cheapest way to execute an order (Obizhaeva-Wang vs Almgren-Chriss vs TWAP/VWAP).

**Hard rules**
- **Research and learning only.** No broker connections, no orders, no account features beyond
  read-only market data.
- **Free data only** for now. Paid feeds (e.g. Databento) come later: an upgrade must mean *adding a
  loader*, never rewriting `core/`.
- Respect every source's rate limits and terms. No scraping around blocks, no VPN/geo workarounds.
- Never print, log or commit API keys. `.env` and `data/` are never committed.
- No magic numbers in code: every parameter lives in `flow_model/config/*.yaml`.
- Report failures and data gaps as results; never silently drop them.
- Fit on earlier dates, validate on later dates; always report in-sample and out-of-sample separately.

## 2. Current status

| Phase | Owner | State |
|---|---|---|
| 1. Scaffold, memory, contracts, schemas, agent defs | Lead | **done** (2026-11-26) |
| 2. Verify sources, data-sources log, start recorders | Crypto / Equities / Commodities / FX | next — awaiting user go-ahead |
| 3. Build `core/`, pass quality gate on simulated data | Quant | next (runs in parallel with 2) |
| 4. Asset agents run core methods allowed by tier | asset agents | not started |
| 5. `exports/impact_params.json` + OU-project hookup doc | Lead | not started |
| 6. Streamlit dashboard on real results | UI | not started |
| 7. End-to-end run, `REPORT.md`, final memory update | Lead | not started |

**Done:** repo scaffold; `.venv` (Python 3.11); configs; common schemas + partitioned parquet storage
(+ tests); `core/` API stubs; agent definitions; per-agent notes files; branches `main` + `agent/*`.
**In progress:** nothing.
**Next:** user go-ahead for Phases 2+3 → Lead spawns role sessions (see §3 "Team").

## 3. Architecture and contracts

Full, binding detail lives in **[`docs/CONTRACTS.md`](docs/CONTRACTS.md)**. Summary:

### Team (mirrors the user's soccer / OU team layout)
- **Lead session** "Order Flow model lead" coordinates, reviews, merges. It does not write `core/`.
- **Role sessions** are separate cloud sessions titled `Order Flow team: <Role>`, tagged
  `order-flow-team` + `order-flow-team:<role>`, each working on its own branch `agent/<role>`.
  Roles: `quant`, `crypto`, `equities`, `commodities`, `fx`, `ui`. Brief = `.claude/agents/<role>.md`.
- Flow: Lead sends a task → role commits to `agent/<role>`, pushes, opens a PR into `main`, and messages
  the Lead with a report → Lead reviews against `docs/CONTRACTS.md` → Lead merges → Lead merges each
  agent's notes into this file.
- `main` is the integration branch. `claude/elegant-wozniak-nl3pae` is the Lead session's working branch
  and is kept equal to `main`.

### Repo layout
```
CLAUDE.md  REPORT.md(phase 7)  Makefile  pyproject.toml  .env.example
docs/CONTRACTS.md  docs/notes/<agent>.md
.claude/agents/<role>.md
data/<source>/<symbol>/<kind>/<YYYY-MM-DD>.parquet     # gitignored raw store, kind ∈ trades|quotes|l2|bars
flow_model/
  settings.py                 # load_config(), instrument(), list_instruments()  (Lead)
  data/schema.py              # Trade, Quote, L2Update, Bar schemas + validate/coerce   (Lead)
  data/storage.py             # write_partitioned / read_range                         (Lead)
  data/loaders/base.py        # Loader ABC every source implements                     (Lead)
  data/loaders/{coinbase,binance,alpaca,dukascopy,yfinance_bars}.py                    (asset agents)
  data/recorders/             # long-running / daily collectors                        (asset agents)
  core/                       # Quant only: simulator signing ofi impact resilience execution seasonality metrics results
  assets/{crypto,equities,commodities,fx}/   # each exposes run(...) -> list[Path]
  ui/app.py                   # reads only results/ and exports/
  results/<asset_class>/<symbol>/   fits.json + chart parquet files (committed)
  exports/impact_params.json        # for the OU project (Phase 5)
  config/*.yaml   tests/
```

### Data schemas (`flow_model/data/schema.py`)
All timestamps `timestamp[ns, tz=UTC]`. Every row carries `symbol`, `source`, `venue`.
- **Trade**: `ts, symbol, source, venue, price, size, side(int8: +1 buyer-initiated, −1 seller, 0 unknown), trade_id, recv_ts`
- **Quote** (top of book): `ts, symbol, source, venue, bid_px, bid_sz, ask_px, ask_sz, recv_ts` (sizes may be NaN)
- **L2Update**: `ts, symbol, source, venue, book_side(int8 +1 bid / −1 ask), price, size(new level total; 0 = removed), is_snapshot, recv_ts`
- **Bar**: `ts(bar open), symbol, source, venue, interval, open, high, low, close, volume`

### `core/` public API (signatures fixed; Quant may add optional kwargs)
`simulator.simulate(params) -> SimResult(trades, quotes, truth)` ·
`signing.{exchange_side, tick_rule, lee_ready, bulk_volume_classification, sign_trades, signing_accuracy}` ·
`ofi.{event_ofi, aggregate_ofi, fit_ofi, ofi_by_window}` ·
`impact.{build_metaorders, fit_sqrt_law, kyle_lambda, amihud}` ·
`resilience.{large_trade_events, event_study, fit_exponential, fit_power_law, fit_propagator, compare_resilience}` ·
`execution.{twap, vwap, almgren_chriss, obizhaeva_wang, expected_cost, compare_schedules}` ·
`seasonality.{intraday_profile, deseasonalize}` · `metrics.{FitResult, ols_fit, train_test_split_by_date}` ·
`results.{write_results, validate_results}`. Exact signatures: `docs/CONTRACTS.md` §2.

### Output contract
`flow_model/results/<asset_class>/<symbol>/fits.json` (per-method `status`, `reason`, `in_sample`,
`out_of_sample`, tier, sources, caveats, date ranges, sample sizes) + `impact_curve.parquet`,
`ofi_bins.parquet`, `resilience_decay.parquet`, `intraday_profile.parquet`, `execution.parquet`.
Schema: `docs/CONTRACTS.md` §3.

### Instrument tiers (`flow_model/config/instruments.yaml`)
| Symbol | Class | Tier | Primary source | Notes |
|---|---|---|---|---|
| BTC-USD, ETH-USD | crypto | **A** full tick | Coinbase Exchange WS (`matches`, `ticker`, `level2`) | Binance archive as secondary history (reachability TBD) |
| SPY, AAPL, MSFT, NVDA | equities | **B** partial venue | Alpaca IEX (keys from env) | IEX ≈ small share of US volume → venue-level estimates |
| EURUSD | fx | **B** partial venue | Dukascopy tick quotes | bid/ask only; volumes are Dukascopy's own |
| CL=F, GC=F | commodities | **C** bars only | yfinance 1m | ~7 days per request → daily recorder; flag roll dates |
| 6E=F | fx | **C** bars only | yfinance 1m | compare with Dukascopy EURUSD where overlapping |

Method × tier: signing-accuracy A · OFI A,B · sqrt-law A (B with caveats) · Kyle λ A,B,C · Amihud A,B,C ·
resilience A,B · execution A,B,C (C uses λ-only model) · seasonality A,B,C.

## 4. Data sources log

Every entry: endpoint/URL · what it returned · limits (history, rate, geo) · workarounds. Detail in the
owning agent's notes.

| Source | Endpoint | Status | Returned / limits | Owner |
|---|---|---|---|---|
| Coinbase Exchange WS | `wss://ws-feed.exchange.coinbase.com` | untested | — | crypto |
| Binance archive | `https://data.binance.vision/` | untested | — | crypto |
| Alpaca market data | `https://data.alpaca.markets/v2/stocks/...` (feed=iex) | untested; needs keys | — | equities |
| Dukascopy | `https://datafeed.dukascopy.com/datafeed/EURUSD/...bi5` | untested | — | fx |
| yfinance | Yahoo chart API via `yfinance` | untested | — | commodities, fx |

## 5. Decisions log

| Date | Decision | Why | By |
|---|---|---|---|
| 2026-11-26 | Team = Lead session + one cloud session per role on `agent/<role>` branches, PRs into `main` | User asked to mirror the soccer team structure | Lead |
| 2026-11-26 | `main` + `agent/{quant,crypto,equities,commodities,fx,ui}` branches created | User approved | Lead |
| 2026-11-26 | Recorders run short sessions (minutes–hours) in cloud; multi-day capture runs on an always-on machine | Cloud containers are ephemeral; user approved | Lead |
| 2026-11-26 | Raw store path `data/<source>/<symbol>/<kind>/<date>.parquet` (adds `<kind>` level) | One symbol has trades, quotes, l2 and bars on the same date | Lead |
| 2026-11-26 | Canonical in-memory frame = pandas with pyarrow schemas; polars allowed inside loaders | One interchange type for `core/`; pyarrow enforces dtypes on disk | Lead |
| 2026-11-26 | Timestamps int64 ns UTC everywhere; each loader records source precision in config | Mixed ms/µs/ns sources | Lead |
| 2026-11-26 | Alpaca keys read from env (`.env` locally, or env secrets in cloud); loader skips cleanly if absent | Keys never pass through chat | Lead |
| 2026-11-26 | Equities default set SPY, AAPL, MSFT, NVDA | "SPY + 2–3 liquid names" | Lead |
| 2026-11-26 | `core/results.py` added (writer + validator for the output contract) | One place enforces fits.json schema | Lead |

## 6. Results summary

_No results yet._ One line per instrument × method: params · fit quality · out-of-sample.

## 7. Known issues and open questions

- Multi-day recording needs an always-on host; cloud captures are short. (open)
- Alpaca keys: user must add `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` to `.env` (local) or the
  cloud environment's secrets. Until then equities stays blocked. (open)
- Network policy of this cloud environment may block some sources; Phase 2 records it. (open)

## 8. How to run

```bash
make install                      # python3.11 venv + editable install with dev deps
make test                         # pytest
make record-crypto MINUTES=30     # (Phase 2) Coinbase WS recorder
make record-bars                  # (Phase 2) yfinance 1m bar appender — run daily
make run                          # (Phase 4/7) full pipeline -> flow_model/results/
make ui                           # (Phase 6) streamlit dashboard
```
Secrets: copy `.env.example` to `.env` and fill it in yourself. Never paste keys into chat.
