# Equities agent notes

Append as you work. Entry format: `### YYYY-MM-DD — <decision|finding|problem|dead-end|done>: <title>` then bullets.
Newest entries at the bottom. The Lead merges summaries into `CLAUDE.md`.


### 2026-11-26 — finding: Alpaca reachable, 401 — keys not yet in this environment
- Endpoint `https://data.alpaca.markets/v2/stocks/SPY/trades?feed=iex&limit=1` → **HTTP 401** (nginx
  "401 Authorization Required", text/html) with no key / dummy key. Host is on the allowlist (proxy CONNECT 200).
- `get_environment` (2026-11-26 07:44 UTC): no `ALPACA_*` env vars in this container and no network secrets on
  the Order_Flow environment yet. So verification (rows, history depth, observed rate limits) is **pending keys**.
- `check()` from this machine: no key ID → `ok=False, "keys not configured"`; dummy key ID → `ok=False, "401 unauthorized"`.

### 2026-11-26 — done: `flow_model/data/loaders/alpaca.py` (AlpacaLoader)
- Kinds `trades`, `quotes`; `feed` = config (`iex`); venue = `iex`; tier B; read-only data endpoints only.
- Auth: header `APCA-API-KEY-ID` ← env `ALPACA_API_KEY_ID`; header `APCA-API-SECRET-KEY` ← env
  `ALPACA_API_SECRET_KEY`, else `secret_placeholder` (proxy injects the network secret). `.env` at repo root
  is loaded with python-dotenv (no override). `AlpacaCredentials.__repr__` shows only `configured` and
  `secret_source`; headers are built per request, never logged; errors carry status + Alpaca's `message` only.
- Pagination via `next_page_token` (cap `max_pages`); `limit=page_limit` (10 000); `sort=asc`.
- Rate limiting: requests spaced ≥ 60/`max_requests_per_min` s (200/min → 0.3 s). Retries on
  `retry_statuses` (429, 5xx) and connection errors with `retry_backoff_s` [1,2,5,10,30]; on 429 waits until
  `X-RateLimit-Reset` if that is longer. 401/403/422 fail fast → `SourceUnavailable`.
- Half-open window: Alpaca's `end` is inclusive, so rows with `ts == end` are dropped to match `[start, end)`.
- Per-fetch stats in `loader.last_stats`: pages, rows_raw, rows, ts_frac_digits_max, seconds, quote drops.

### 2026-11-26 — decision: field mapping and what is dropped
- Trades: `t`→ts, `p`→price, `s`→size, `i`→trade_id (string), side = **0** (Alpaca has no aggressor flag;
  Lee-Ready happens in core at Phase 4), recv_ts = null (historical).
  **Dropped:** `x` (exchange code; always `V` = IEX on this feed), `c` (trade condition codes, e.g. `@`, `I`
  odd-lot, `T` extended hours, `Z` out-of-sequence), `z` (tape A/B/C).
  Open: in Phase 4 we may want to filter condition codes (odd-lot `I`, out-of-sequence, corrections) before
  impact fits — that would need `c` kept; propose an optional extra column or a side table if so.
- Quotes: `t`→ts, `bp/bs`→bid_px/bid_sz, `ap/as`→ask_px/ask_sz. **Dropped:** `bx`/`ax` (exchange codes),
  `c` (quote condition), `z` (tape). Quotes are IEX's own BBO, **not the NBBO**.
- IEX quotes are often one-sided (price 0) or crossed vs itself; the schema forbids both, so they are
  removed and **counted** (`dropped_one_sided`, `dropped_crossed` in stats and in the pull summary). Locked
  (bid == ask) quotes are kept.
- Quote size units: passed through as Alpaca reports them. Alpaca historically documented sizes in round
  lots for some feeds; **verify units** on the first real pull (compare `bs/as` to trade sizes).

### 2026-11-26 — decision: config keys added under `alpaca:` in sources.yaml (told the Lead)
- `sort, timeout_s, retry_backoff_s, retry_statuses, max_pages, check_symbol, user_agent, rth_tz, rth_open,
  rth_close, lookback_days, full_day_probe_min, depth_probe_start, log_every_pages`.

### 2026-11-26 — done: run-it-once-keys-exist commands
```bash
.venv/bin/python -m flow_model.data.loaders.alpaca check          # JSON SourceCheck, exit 0 if ok
.venv/bin/python -m flow_model.data.loaders.alpaca pull           # last full RTH day, SPY AAPL MSFT NVDA, trades+quotes
.venv/bin/python -m flow_model.data.loaders.alpaca pull --date 2026-11-25 --symbols SPY
.venv/bin/python -m flow_model.data.loaders.alpaca depth          # earliest available trade per symbol (history depth)
```
- `pull` picks the most recent weekday whose session had trades in the last minute before 16:00 ET
  (skips weekends, holidays, half-days), fetches 09:30–16:00 ET, writes `data/alpaca/<SYM>/<kind>/<date>.parquet`,
  prints per symbol/kind: rows, pages, drops, ts precision digits, seconds, first/last ts, and the
  `X-RateLimit-*` headers seen. Failures are reported per entry (status=failed + reason), not fatal.
- Each command first runs `check()`; if it fails it prints the reason and exits 1 without fetching.
- Suggested Makefile target for the Lead: `pull-equities: $(PY) -m flow_model.data.loaders.alpaca pull`.

### 2026-11-26 — done: tests `flow_model/tests/test_equities_alpaca.py` (24 tests, mocked HTTP)
- Fixtures in `flow_model/tests/fixtures/equities/*.json` (Alpaca-shaped trades pages, quotes page, latest trade).
- Covers: header building (env secret / proxy placeholder / missing key), reprs + logs + errors never contain
  key or secret, pagination with page_token, schema validity, ns precision, half-open end, quote cleaning
  counts, 429/5xx backoff, retries exhausted, 401 fail-fast, no request without keys, check() never raises,
  rate limiter spacing, RTH window across DST, last-full-day selection, pull → store.
