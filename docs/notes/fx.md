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
