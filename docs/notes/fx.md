# FX agent notes

Append as you work. Entry format: `### YYYY-MM-DD — <decision|finding|problem|dead-end|done>: <title>` then bullets.
Newest entries at the bottom. The Lead merges summaries into `CLAUDE.md`.


### 2026-11-26 — done: Dukascopy loader built (`flow_model/data/loaders/dukascopy.py`)
- `DukascopyLoader(Loader)`: source/venue `dukascopy`, tier B, kinds `{quotes}`. `fetch()` walks UTC hours,
  `fetch_hour()` returns `(frame, HourResult)` with status `ok|empty|failed`; `last_report` keeps per-hour outcomes.
- Format implemented: URL month 0-based; LZMA alone (`FORMAT_AUTO` decode); 20-byte big-endian records
  `>IIIff` = ms offset, ask pts, bid pts, ask_vol, bid_vol; price = pts / `price_scale[symbol]` (EURUSD 1e5).
  **Not yet verified against a live file** (source refused us, see below) — the format is the widely documented one.
- Empty hour = HTTP 200 with empty body or 404 → `empty`, logged, not an error. Crossed quotes (bid>ask) are
  dropped and counted (`n_crossed_dropped`), never silent.
- Retries: pause `request_pause_s` before every request after the first; backoff `backoff_s` on 429/5xx/network
  errors; after that the hour is `failed`. `fetch()` raises `SourceUnavailable` if any hour failed (so `Loader.load`
  never stores a silently partial day). The CLI `pull_days` stores what arrived and reports failed hours.
- CLI: `.venv/bin/python -m flow_model.data.loaders.dukascopy --symbol EURUSD --start YYYY-MM-DD --end YYYY-MM-DD`
  → store + JSON report in `logs/`; exit 1 if any hour failed. Safe to re-run (storage appends + de-dupes).
- No trades exist in this source → signing / sqrt-law are `not_applicable` for EURUSD (Phase 4).

### 2026-11-26 — decision: config keys added under `dukascopy:` in sources.yaml (Lead informed)
- `price_scale` changed from scalar to per-symbol map `{EURUSD: 100000}` (other pairs, e.g. JPY, use 1e3).
- New: `backoff_s`, `retry_statuses`, `empty_statuses`, `timeout_s`, `user_agent`, `check_symbol`,
  `check_lag_hours`, `volume_note`. `request_pause_s` raised 0.5 → 2.0 because of the 429s.

### 2026-11-26 — decision: test fixture is synthetic LZMA built in-test
- `*.bi5` is gitignored repo-wide, and no real file could be downloaded; `encode_bi5()` builds LZMA-alone
  payloads in the tests. 11 tests, no network.

### 2026-11-26 — decision: 6E=F overlap plan (no own loader)
- 6E=F 1m bars come from the Commodities agent's `yfinance_bars` loader (import it in Phase 4).
- Overlap: CME 6E trades ~Sun 22:00 – Fri 21:00 UTC with a daily 1h break; Dukascopy EURUSD is 24x5.
  Compare on common 1m buckets: mid from Dukascopy resampled to 1m vs 6E close (6E is USD per EUR, same
  direction; futures basis ≈ carry, near-constant intraday) — return correlation, lead/lag, spread (bps) vs
  Amihud, intraday profiles. yfinance 1m only covers ~7–30 days back, so overlap needs a recent Dukascopy pull.

### 2026-11-26 — finding: datafeed.dukascopy.com mostly refuses this cloud egress (429 / 503 / TLS resets)
All requests sequential, ≥30 s apart after the first failure. Times UTC.
| Time | Request | Result |
|---|---|---|
| 07:43:36 | `EURUSD/2026/10/24/10h_ticks.bi5` (curl) | proxy CONNECT 200, then TLS handshake reset by peer (curl 35) |
| 07:43:53 | same, retry | TLS reset after ~12 s |
| 07:44:10 | `HEAD https://datafeed.dukascopy.com/` | **429**; body `{"error": "Too Many Requests. For instructions, see: https://wiki.dukascopy.com/en/development/data-export/"}`, no Retry-After |
| 07:44:10 | control: `HEAD https://data.binance.vision/` | 200 in 0.5 s (proxy itself is fine) |
| 07:45:31 / 07:47:32 | 10h file (backoff probe, +60 s / +120 s) | **503** "No server is available to handle this request" |
| 07:51:38 | same (+240 s) | connection reset |
| 07:59:48 | same (+480 s) | **200, 25 313 bytes** → 5153 ticks (only success of the day) |
| 08:00–08:21 | day pull 2026-11-24, hours 00 and 01, 5 attempts each (backoff 30/60/120/300 s) | 503 ×5, reset ×3, read timeout (60 s) ×1, 0 successes → stopped by hand |
| 08:21:22 | history spot-check `EURUSD/2003/04/05/10h_ticks.bi5` (one try) | **429** |
- Success rate ≈ 1 / 16 requests over ~40 min. The 429 body points to a data-export wiki; `wiki.dukascopy.com`
  is not on the environment allowlist (DNS fails), so its rules could not be read. Not worked around.
- Likely cause (unconfirmed): the shared cloud egress IP is rate-limited/blocked by Dukascopy's front end.
- Data stored: **1 hour only** (2026-11-24 10:00–11:00 UTC, 5153 rows) in `data/dukascopy/EURUSD/quotes/2026-11-24.parquet`.
  The 3–5 full days requested were **not** obtained.
- History depth: **unverified** (2003 spot-check got 429). Commonly cited start is 2003-05 for EURUSD.

### 2026-11-26 — finding: format verified on the one live file
- LZMA alone header `5d 00004000` + uncompressed size 103 060 = 5153 × 20 bytes ✓; `>IIIff` records decode cleanly.
- ms offsets 116 … 3 598 931, monotonic, no duplicate ms; min Δt 52 ms → **precision = 1 ms** (ns remainder always 0).
- Price scale 1e5 ✓: bid 1.09344 / ask 1.09347; spread 0.1–0.7 pips (median 0.3); 0 crossed quotes.
- Volumes: 0.12–29.0, steps of 0.9/1.12/1.8 → look like millions of base currency (Dukascopy's own liquidity). Unit unconfirmed.
- Clock: Dukascopy server timestamps (UTC hour + ms offset); no receive time (`recv_ts` null, historical download).

### 2026-11-26 — decision: pull_days stores hour by hour and stops after 2 failed hours in a row
- Config `max_consecutive_failed_hours: 2`. A refusing source should not get 24×5 retries per day.

### 2026-11-26 — problem (open): how to get 3–5 days of EURUSD
Options for the Lead: (a) run the CLI on the always-on machine (home IP, not the shared cloud egress);
(b) allow `wiki.dukascopy.com` so we can read their export rules; (c) retry here later at low rate.
Command: `.venv/bin/python -m flow_model.data.loaders.dukascopy --symbol EURUSD --start 2026-11-23 --end 2026-11-25`.
