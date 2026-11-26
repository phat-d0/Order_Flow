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
| 2. Verify sources, data-sources log, start recorders | Crypto / Equities / Commodities / FX | **done** 2026-11-26 (PRs #1–#3, #5, #6; 158 tests on main) |
| 3. Build `core/`, pass quality gate on simulated data | Quant | **done** — PR #4 merged 2026-11-26, gate passes (115 tests on main) |
| 4. Asset agents run core methods allowed by tier | asset agents | next — plan shown to user, awaiting go-ahead |
| 5. `exports/impact_params.json` + OU-project hookup doc | Lead | not started |
| 6. Streamlit dashboard on real results | UI | not started |
| 7. End-to-end run, `REPORT.md`, final memory update | Lead | not started |

**Done:** repo scaffold; `.venv` (Python 3.11); configs; common schemas + partitioned parquet storage
(+ tests); `core/` API stubs; agent definitions; per-agent notes files; branches `main` + `agent/*`.
**In progress:** nothing — all role sessions on standby.
**Data in the store (cloud, ephemeral):** Coinbase BTC-USD/ETH-USD 45 min (07:50–08:35 UTC 2026-11-26: 27k trades, 35k quotes,
1.4M L2 rows); Binance BTCUSDT/ETHUSDT aggTrades 2026-11-23..25 (7.2M rows); yfinance 1m CL=F/GC=F/6E=F 29 days;
Dukascopy EURUSD 1 hour; Alpaca none (no keys).
**Waiting on:** user go-ahead for Phase 4 + equities ticker list for the yfinance Tier C fallback.
**Next:** Phase 4 per the plan in docs/notes/lead.md.

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
- Live sessions (2026-11-26): Lead `session_01UShqQxEUYri99o8V9nQf5v` · Quant `session_01UShqQuuv3rPwhGtQRHZdQz` ·
  Crypto `session_01UShqQvjfBeDdFx7xMFCUno` · Equities `session_01UShqQxPyZ8BYKLT4a6gHGh` ·
  Commodities `session_01UShqQxCdBDKkumBK9PwNxm` · FX `session_01UShqQyHH1pGM91E48Bhivw` · UI: not started (Phase 6).
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
- **Trade**: `ts, symbol, source, venue, price, size, side(int8: +1 buyer-initiated, −1 seller, 0 unknown), trade_id, conditions, recv_ts`
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
| Coinbase Exchange WS | `wss://ws-feed.exchange.coinbase.com` | **works, no auth** for `matches` (side = MAKER → aggressor inverted; 27,065/27,065 agree with ticker taker side), `ticker` (BBO+sizes, only on trades), `heartbeat`, `level2_batch` (full-depth snapshot ~37k BTC levels, updates batched ~50 ms, **no sequence numbers**). **`level2`/`level3`/`full` refused: require authentication** — not used | µs exchange time + local recv_ts; ~10 match/s, ~32 l2update/s combined; 1 reconnect in 45 min (back in 1 s); ~0.4 GB/day with L2. Quotes = top of book rebuilt from level2_batch. No history for quotes/L2 beyond what we record | crypto |
| Coinbase REST | `https://api.exchange.coinbase.com` `/products/<id>/trades` (after=<id>, ≤1000/page), `/book`, `/time` | works (200) | trade history back to trade_id 1 (2015-01-08); ~10 req/s public (we pace 4/s); used to back-fill trade_id holes after reconnects | crypto |
| Binance archive | `https://data.binance.vision/data/spot/daily/aggTrades/<SYM>/` | works (200) | daily zip + CHECKSUM, published ~02:40 UTC for the prior day; back to 2017-08-17; BTCUSDT ~1.7M rows/day; µs since 2025-01-01 (ms before); `isBuyerMaker` gives the aggressor side. **USD-M `bookTicker` discontinued after 2024-03-30** (404 since) | crypto |
| Alpaca market data | `https://data.alpaca.markets/v2/stocks/{sym}/trades|quotes` (feed=iex) | loader merged (PR #1); **401 — no keys yet** | ns timestamps per docs (confirm on first pull); one-sided/crossed IEX quotes dropped + counted; run `make pull-equities` once keys exist | equities |
| Dukascopy | `https://datafeed.dukascopy.com/datafeed/EURUSD/{Y}/{M-1:02}/{D}/{H}h_ticks.bi5` | loader merged (PR #5); **effectively blocked from the cloud**: 1 success in ~16 sequential requests 07:43–08:21 UTC (429 without Retry-After, 503 "No server is available", TLS resets, timeouts) while Binance answered 200 | Format verified on the live file: LZMA, `>IIIff` (ms, ask, bid, ask_vol, bid_vol), price scale 1e5, 1 ms precision (server clock), 5,153 ticks in one hour, spread 0.1–0.7 pip. Rows/day, history depth and volume units unverified. Pull on an always-on machine: `python -m flow_model.data.loaders.dukascopy --symbol EURUSD --start D --end D` | fx |
| yfinance | `query1/query2.finance.yahoo.com/v8/finance/chart/<sym>?interval=1m` | **works** (200; PR #3). Crumb endpoint 429s but yfinance proceeds without it; fc.yahoo.com 404 | 1m history = last 30 days (422 beyond); ≤8 days/request (config 7); epoch-s bar-open stamps; ~29k bars/symbol/29d; volume present for CL=F, GC=F, 6E=F; minutes with no trades come back as NaN rows (dropped + counted); **Yahoo zeroes the volume of each session-open bar** except the latest day → appender never overwrites positive volume with 0 | commodities, fx |

Network: the `Order_Flow` cloud environment is limited-access. On 2026-11-26 every data host returned proxy
403 (CONNECT refused); the user approved adding exactly these hosts: ws-feed.exchange.coinbase.com,
api.exchange.coinbase.com, data.binance.vision, data.alpaca.markets, datafeed.dukascopy.com,
query1.finance.yahoo.com, query2.finance.yahoo.com, fc.yahoo.com. Any new host needs the Lead to request it.

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
| 2026-11-26 | Data hosts added to the Order_Flow environment allowlist (exact hosts only) | All were proxy-blocked; user approved | Lead |
| 2026-11-26 | Alpaca auth: `ALPACA_API_KEY_ID` env var + secret either as env var `ALPACA_API_SECRET_KEY` or as a network secret on header `APCA-API-SECRET-KEY` (proxy injects; loader sends a placeholder) | Network secrets only accept *-KEY/TOKEN header names; user adds keys themselves | Lead |
| 2026-11-26 | Trades schema gains nullable `conditions` (comma-joined source codes) | Equities needs Alpaca `c` to filter odd-lot/out-of-sequence trades; Databento has flags too | Lead |
| 2026-11-26 | Agents own `flow_model/tests/fixtures/<role>/` | Equities PR #1 needed committed JSON fixtures | Lead |
| 2026-11-26 | Bar appender run daily (cron `17 6 * * *` UTC on an always-on machine) | yfinance keeps only 30 days of 1m bars; daily run also captures session-open volumes before Yahoo zeroes them | Commodities |
| 2026-11-26 | `simulator.impact_coef` unit = log-return per share, 1e-5 (was 1e-4 price units) | Old value explained ~0.3% of 1-min variance; λ unrecoverable | Quant |
| 2026-11-26 | Kyle λ unit = log-return per share at `impact.kyle_bar`; OFI β = ΔP (price units) per mean-depth of OFI; √-law Y by WLS (1/x²) | Fixed units so instruments are comparable | Quant |
| 2026-11-26 | Tier C execution model: λ-only (η = λ·price, no memory) → OW = TWAP by construction | No resilience estimate without quotes; listed as a Tier C caveat | Quant/Lead |
| 2026-11-26 | `core/demo.py` is the reference pipeline asset agents copy (simulate → IS/OOS fits → write_results) | Uniform Phase 4 outputs | Quant |
| 2026-11-26 | Stop hammering Dukascopy from the cloud; pull EURUSD on an always-on machine; no extra hosts (wiki) requested | Respect source limits; the cloud IP is refused regardless of pacing | Lead |
| 2026-11-26 | Coinbase quotes = top of book rebuilt from public `level2_batch`; `ticker` kept as side cross-check | `level2` needs auth; no credentials allowed | Crypto |
| 2026-11-26 | Binance spot (`binance`/`binance_spot`) and USD-M (`binance_um`/`binance_usdm`) are separate sources; BTCUSDT/ETHUSDT never mixed with Coinbase BTC-USD in one fit | Different venue and quote currency | Crypto |
| 2026-11-26 | Coinbase trade_id holes back-filled from REST `/trades` | 0 trade holes after reconnects | Crypto |
| 2026-11-26 | `core/results.py` added (writer + validator for the output contract) | One place enforces fits.json schema | Lead |

## 6. Results summary

One line per instrument × method: params · fit quality · out-of-sample. Real-data results start in Phase 4.

**Simulator quality gate (Phase 3, synthetic, seed 7, 3 days, 621k trades; IS = 2 dates, OOS = 2 dates)** — detail `docs/notes/quant.md`
| Param | True | IS | OOS |
|---|---|---|---|
| Kyle λ (log-ret/share, 1min) | 5.22e-6 | 3.96e-6 (−24%) | 4.48e-6 (−14%) — biased low when flow is sign-persistent; exact with no metaorders |
| √-law Y | 3.10 | 3.52 (+14%) | 3.02 (−2.5%) |
| ρ (1/s) | 0.050 | 0.053 (+6%) | 0.060 (+19%) |
| permanent fraction | 0.30 | 0.36 | 0.32 |
| OFI β (1s/10s/1min) | 0.0025 | 0.0025 (<0.1%) | 0.0025 (<0.1%) |
Gate: λ, Y within 20%; ρ within 25% (AIC prefers exponential); OFI β within 20%; exchange flag & Lee-Ready 100%,
tick rule ≥ 0.9 only when mid noise ≪ spread (0.79 at default noise — as theory predicts); OW < TWAP with finite ρ, = TWAP otherwise.

## 7. Known issues and open questions

- Multi-day recording needs an always-on host; cloud captures are short. (open)
- Alpaca keys: user is adding them in the Order_Flow environment settings (key ID as env var
  `ALPACA_API_KEY_ID`; secret as env var `ALPACA_API_SECRET_KEY` or network secret on `APCA-API-SECRET-KEY`).
  New sessions pick them up; until then equities is blocked. (open)
- Alpaca open items (equities notes): IEX quote size units (shares vs lots) to confirm on first pull; IEX BBO ≠ NBBO.
- Dukascopy refuses almost all requests from the cloud egress (429/503/resets). Decision: no further pulls from the cloud
  beyond one low-rate retry in Phase 4; full pulls run on the user's always-on machine. Until then EURUSD Tier B is
  blocked (1 hour of ticks stored). Yahoo: resolved — only the crumb endpoint 429s. (open)
- yfinance 1m quirks for Phase 4: the 29-day backfill has ~20 zero-volume session-open bars per symbol (source artifact); BVC/Kyle λ must exclude/flag them and drop roll bars. No exchange holiday calendar yet → holidays show as gaps. (open)
- Roll offsets (Yahoo 1m CL=F rolls 2 bdays before last trade; GC=F 3 bdays before FND) rest on one observed roll each; re-verify ~2026-12-17 (CL) and ~2027-01-26 (GC). (open)
- Coinbase L2: no sequence numbers on `level2_batch` → message loss inside a connection is undetectable; 50 ms
  batch resolution; book re-snapshotted on reconnect (~1–2 s quote hole per reconnect). (open, inherent)
- `storage.write_partitioned(append)` rewrites the whole day file on every flush → heavy for all-day L2
  (~0.5M rows/h/product). Workaround: larger flush interval or `--no-l2`; fix (part files/hourly l2) before any
  always-on deployment. (open, Lead)
- Crypto agent's probe reached `s3-ap-northeast-1.amazonaws.com` (Binance listing host) although it is not on our
  allowlist; no code uses it. (noted)
- Core estimator caveats for Phase 4/REPORT (from Quant): (1) Kyle λ biased ~25% low under sign-persistent flow →
  report λ at several bar sizes; (2) simulator is a pure book-shift world (OFI R² ≈ 0.98, constant spread, no depth
  depletion) → real OFI R² will be far lower; (3) sim impact is OW (linear), so the gate validates the √-law
  *estimator*, not the √ law; free δ ≈ 0.8–1.0 on sim; (4) same-sign-run proxy metaorders miss interleaved
  metaorders → √-law fits on real data need a caveat; (5) propagator β is descriptive; (6) Tier C λ-only
  execution model is an assumption. (open)
- GitHub default branch came out as `agent/quant` (first ref to land on the empty repo). User to set it to
  `main` in GitHub → Settings → General → Default branch. Until then every PR must target `main` explicitly. (open)

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
