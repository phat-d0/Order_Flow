# Commodities agent notes

Append as you work. Entry format: `### YYYY-MM-DD — <decision|finding|problem|dead-end|done>: <title>` then bullets.
Newest entries at the bottom. The Lead merges summaries into `CLAUDE.md`.


### 2026-11-26 — finding: Yahoo 1m bars reachable from the cloud environment
- `https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1m&range=1d` → **200** (07:44 UTC); query2 → 200.
  `https://fc.yahoo.com` → 404 (cookie host; expected). yfinance's crumb fetch logs **429** on every call,
  then continues without a crumb and the chart call succeeds. ~40 chart requests this session, no 429 on chart.
  The Lead's earlier 429 was probably the crumb endpoint, or transient.
- **Max window per 1m request: 8 days.** 9 and 10 days → HTTP 422 "Only 8 days worth of 1m granularity data
  are allowed to be fetched per request". Config keeps `max_days_per_request: 7`.
- **History depth: 30 days.** Start 31 or 40 days back → "The requested range must be within the last 30 days".
  Config `lookback_days: 29`.
- Timestamps: epoch **seconds**, bar-open label, all on whole minutes; yfinance localises them to
  America/New_York → we convert to UTC. Data runs to ~real time (last bar 23:58 ET on 2026-11-25).
- Volume is present for CL=F, GC=F and 6E=F (≈99.9 % of bars > 0).
- Minutes with no trades come back as all-NaN rows (with `keepna=True`, ~20 % of raw rows). These are dropped and
  counted (`nan_price`), and show up as missing session minutes.

### 2026-11-26 — finding: Yahoo zeroes some volumes (an artifact of the source, not real zero-trade minutes)
- The **first bar of every response** has volume 0 (e.g. 23:00 UTC bar: 0 when it opens the response, 45 otherwise).
- The **first bar of each trading day** (18:00 ET; Sunday 18:10 ET) has volume 0 for every day *except the most
  recent trading day* in Yahoo's store. Re-requesting small windows later does **not** recover it (tested 3 ways).
- Handling: loader requests `lead_in_minutes` early and discards it (fixes the first-row case); session-open zeros
  are kept (prices are fine) and counted as `n_zero_volume_after_gap`; the appender never replaces a stored
  positive volume with 0. Run daily: each run then captures the latest session open while it is still correct.
  Days the appender missed keep volume 0 on the open bar (21/20/23 such bars in the 29-day backfill).

### 2026-11-26 — finding: missing-minute patterns (Globex session 18:00–17:00 ET, Sun 18:00 – Fri 17:00)
- Sunday reopen: 18:00–18:10 ET always missing (first bar 18:10 ET).
- 6E=F: 23:59–00:09 ET missing every day (10 min) — Yahoo-side, not CME.
- Scattered 5–13 min holes (all three symbols at the same times, e.g. 2026-11-04 10:13 UTC, 2026-11-23 00:17 UTC)
  → Yahoo feed outages, not market halts.
- No bars after 2026-11-26 04:58 UTC (23:58 ET 11-25) at run time 07:51 UTC: US Thanksgiving (CME holiday
  hours). Holidays are not in the session model, so they appear as gaps (reported, not hidden).
- A few bars at 17:00 ET (inside the daily halt) exist: counted as `outside_session_bars`, kept.

### 2026-11-26 — finding: when Yahoo's 1m continuous futures roll (checked against single-contract tickers)
- Yahoo serves single contracts: `CLF27.NYM`, `CLG27.NYM`, `GCZ26.CMX`, `GCG27.CMX` (daily + 1m). `CLZ26.NYM`
  (expired) → "possibly delisted".
- **CL=F 1m switched CLZ26→CLF27 at 2026-11-18 08:31 UTC** (2 business days before CLZ26's last trade 11-20).
  Jump −1.46 % (z = 27.9).
- **GC=F 1m switched GCZ26→GCG27 at 2026-11-25 08:31 UTC** (3 business days before first notice day 11-30).
  Jump +0.88 % (z = 21.0).
- Yahoo's *daily* `=F` series rolls on different days (CL on 11-23, GC on 11-26). Don't mix daily and 1m roll dates.
- Encoded as `roll_rule.yahoo_offset_bdays` (−2 CL, −3 GC) in instruments.yaml. Each is based on **one** observed
  roll, so re-check it at the next roll with `rolls.match_contract`.
- Large z-jumps (> 8σ on 1m) also happen on news (≈40 per symbol per month, e.g. 13:30 UTC data releases), so the
  detector only calls a jump a roll when it is the largest jump within ±1 business day of an expected roll date.

### 2026-11-26 — decision: appender design
- Window = last stored bar − 60 min (Yahoo revises recent bars) or 29 days on first run; skip bars not yet complete
  (ts + 1m + 120 s > now); merge on `ts` (Yahoo's newer value wins, except a stored positive volume is never replaced by 0);
  rewrite the affected daily files in `overwrite` mode. Plain `append` would keep both versions of a revised bar
  (storage de-duplicates on every column), giving duplicate `ts`.
- Lock file `logs/bar_appender/.lock` so overlapping cron runs cannot both write. Per-symbol failures are logged
  and recorded; the others continue; exit code 1 if any failed.
- Run report JSON per run in `logs/bar_appender/run_<UTC>.json` + rotating `bar_appender.log`.

### 2026-11-26 — done: appender run from the cloud (2026-11-26 07:51 UTC, fresh store)
| Symbol | Bars stored | Range (UTC) | Requests | NaN rows dropped | Zero-vol (all at session open) | Missing session min | Gaps ≥ 5 min |
|---|---|---|---|---|---|---|---|
| CL=F | 28,456 | 2026-10-28 07:52 → 2026-11-26 04:58 | 5 | 7,347 | 21 | 464 / 28,917 | 17 |
| GC=F | 28,580 | same | 5 | 7,223 | 20 | 339 / 28,917 | 14 |
| 6E=F | 28,079 | same | 5 | 7,724 | 23 | 839 / 28,917 | 32 |
- The missing counts include the 167-minute trailing Thanksgiving gap. A second run straight after added +0, revised 0
  (idempotent). Raw store ~2.3 MB.

### 2026-11-26 — done: scheduling the appender on an always-on machine
- Yahoo keeps only 30 days of 1m data, and session-open volumes are only right while that day is the latest one,
  so run it **once per trading day** (more often is harmless).
- crontab (UTC machine; 06:17 UTC is after the Globex open of the trading day and outside Yahoo's busy US hours):
  `17 6 * * * cd /path/to/Order_Flow && .venv/bin/python -m flow_model.data.recorders.bar_appender >> logs/cron.log 2>&1`
  On a machine with a local clock: `CRON_TZ=UTC` above the line (cronie), or convert the time.
- macOS: the same line via `crontab -e` works if the Mac is awake; or a launchd plist with `StartCalendarInterval`.
- Check: `tail logs/bar_appender/bar_appender.log`; non-zero exit = at least one symbol failed (see run JSON).
- Options: `--symbols CL=F GC=F`, `--dry-run`, `--root <store>`.

### 2026-11-26 — done: Phase 2 files
- `flow_model/data/loaders/yfinance_bars.py` — `YFinanceBarsLoader` (`fetch`, `fetch_with_report`, `check`),
  `normalize_bars`, `chunk_windows`, `session_minutes`, `session_gaps`, `FetchReport`, injectable `client`.
  Generic: FX can use it for 6E=F unchanged.
- `flow_model/data/recorders/bar_appender.py` — `run()`, `append_symbol()`, CLI `main()`.
- `flow_model/assets/commodities/rolls.py` — `calendar_roll_dates`, `yahoo_roll_dates`, `detect_jumps`,
  `flag_rolls` (→ fits.json `data.gaps` + caveats), `match_contract`.
- Tests: `flow_model/tests/test_commodities_yfinance.py`, `test_commodities_appender_rolls.py`, fixture
  `flow_model/tests/fixtures/commodities/yf_1m_sample.csv` (synthetic rows).
- Config: keys added under `sources.yaml` → `yfinance:`; `roll_rule` added to CL=F and GC=F in instruments.yaml
  (entries expanded to block style). 6E=F has no `roll_rule` (FX owns it; `imm_quarterly` rule exists for them).

### 2026-11-26 — problem: open items
- Phase 4 should drop the roll bar (and ideally re-base pre-roll prices) before return-based fits; `flag_rolls`
  gives the bar. The 1m roll offsets come from one roll each, so re-verify at the next roll (CL ~2026-12-17, GC ~2027-01-26).
- Session-open volumes for days before the first appender run are 0 (source artifact): BVC/Kyle λ should
  exclude or flag those bars.
- Exchange holiday calendar not modelled; holidays appear as gaps.
