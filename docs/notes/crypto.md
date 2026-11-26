# Crypto agent notes

Append as you work. Entry format: `### YYYY-MM-DD — <decision|finding|problem|dead-end|done>: <title>` then bullets.
Newest entries at the bottom. The Lead merges summaries into `CLAUDE.md`.


### 2026-11-26 — finding: source verification from the Order_Flow cloud container
All probes from this machine through the agent proxy (allowlist: ws-feed.exchange.coinbase.com,
api.exchange.coinbase.com, data.binance.vision).

| Source / endpoint | Status | What came back | Limits | Timestamp precision / clock |
|---|---|---|---|---|
| WS `wss://ws-feed.exchange.coinbase.com` `matches` | OK, no auth | `last_match` on subscribe, then `match` {trade_id, maker/taker order ids, **side = maker side**, size, price, product_id, sequence, time} | ~7 msg/s BTC-USD, ~1/s ETH-USD (07:45 UTC) | `time` µs ISO-8601 Z (exchange clock); recorder adds `recv_ts` (local ns) |
| WS `ticker` | OK, no auth | one per trade: price, best_bid/best_ask **+ sizes**, side (= **taker** side), trade_id, 24h stats | updates only on trades (not on book changes) | µs |
| WS `level2` | **refused** | `{"type":"error","message":"Failed to subscribe","reason":"level2, level3, and full channels now require authentication. https://docs.cloud.coinbase.com/exchange/docs/websocket-auth"}` | auth needed (not used: no credentials by rule) | — |
| WS `full` / `level3` | **refused** | same error as level2 | auth needed | — |
| WS `level2_batch` | OK, no auth | server reports it as channel `level2_50`; **full-depth** snapshot (BTC ~11.6k bids / 25.6k asks levels; ETH ~3.7k / 15.3k) then `l2update` {changes:[[side,price,new_size]], time} batched ~50 ms | ~15 l2update msg/s per product; **no sequence numbers** on l2update | µs `time` (batch time; not monotone across messages within a few ms) |
| WS `heartbeat` | OK | 1/s per product with last_trade_id, sequence | — | second resolution |
| REST `https://api.exchange.coinbase.com` `/time`, `/products/BTC-USD`, `/products/<id>/trades`, `/products/<id>/book?level=1` | 200 | trades paginate by `after=<trade_id>` (returns ids < after, newest first, limit ≤ 1000, `cb-before/cb-after` headers); history reaches trade_id 1 (2015-01-08) | public ~10 req/s (we pace 4/s); no rate-limit headers returned | trades `time` µs; book `time` ns |
| `https://data.binance.vision/data/spot/daily/aggTrades/{BTC,ETH}USDT/...zip` | 200 | daily zip + `.CHECKSUM` (sha256). BTCUSDT 2026-11-25: 25.1 MB zip / 150 MB csv / 1,729,771 rows; ETHUSDT 15.0 MB. Today's file 404 until ~02:40 UTC next day. History back to 2017-08-17 | no documented rate limit; we pause 1 s per request | `transact_time` **µs** since 2025-01-01, **ms** before (auto-detected); CSV has no header (some older files do) |
| `.../futures/um/daily/bookTicker/BTCUSDT/...zip` | 200 up to **2024-03-30**, 404 from 2024-03-31 on | header `update_id,best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,transaction_time,event_time`; ~90–340 MB zip per day; monthly file 2024-03 = 6.7 GB | archive discontinued | ms |

- The data.binance.vision HTML index lists files via `s3-ap-northeast-1.amazonaws.com`. One listing request to
  that host returned 200 (not on our allowlist; probably allowed by the environment's default policy). Not used
  by any code: loaders build file URLs directly on data.binance.vision.
- Empirical side check (recorder `side_check_ticker_vs_match`): ticker taker side == inverted match maker side for
  497/497 trades in the 1-minute smoke test → mapping confirmed.

### 2026-11-26 — decision: quotes = top of book rebuilt from `level2_batch`, not from `ticker`
- `ticker` only fires on trades, so it misses quote changes between trades (bad for OFI). `level2_batch` is
  public and full-depth, so the recorder maintains the book and emits a quote row whenever best bid/ask price
  or size changes (ts = l2update batch time, ~50 ms granularity). `ticker` is still subscribed for the side
  cross-check; `quotes_from: ticker` is a config/CLI fallback if `level2_batch` is ever withdrawn.
- Limitation: 50 ms batching → OFI from these quotes is at batch resolution, not per-event. No l2update
  sequence numbers → an L2 message lost inside a connection can't be detected; the book is rebuilt from a fresh
  snapshot on every reconnect.

### 2026-11-26 — decision: separate sources for Binance spot vs USD-M futures
- `binance` (venue `binance_spot`, aggTrades → trades) and `binance_um` (venue `binance_usdm`, bookTicker →
  quotes) are different sources so spot trades and futures quotes never get combined (e.g. in Lee-Ready).
  Symbols stay Binance's (`BTCUSDT`, USDT-quoted) — never mix with Coinbase `BTC-USD` in one fit.
- aggTrades = aggregated fills of one taker order at one price: trade counts are not comparable with Coinbase
  per-fill matches.

### 2026-11-26 — done: loaders, recorder, tests
- `flow_model/data/loaders/coinbase.py`: pure parsers (`aggressor_side`, `match_row`, `rest_trade_row`,
  `ticker_quote_row`, `l2_rows`), `CoinbaseREST` (paced, retried, read-only), `CoinbaseLoader` (trades via REST
  with trade_id bisection by time; quotes/L2 → `NotSupported`, they come only from the recorder:
  use `load(..., fetch_missing=False)`).
- `flow_model/data/loaders/binance.py`: `BinanceSpotLoader`, `BinanceUMLoader`, sha256 checksum verification,
  CLI `python -m flow_model.data.loaders.binance --symbols BTC-USD ETH-USD --days 3`.
- `flow_model/data/recorders/coinbase_ws.py`: `Book`, `FeedProcessor` (pure), `Recorder` (asyncio).
  Gap detection: trade_id holes (back-filled from REST `/trades`), duplicate trades dropped, heartbeat
  `last_trade_id` ahead counter, stale feed (no message for `heartbeat_timeout_s`) → reconnect, reconnects with
  backoff ladder, crossed book. Gaps → `logs/coinbase_gaps.jsonl`; run stats → `logs/coinbase_ws_stats_<start>.json`;
  log → `logs/coinbase_ws.log` (rotated at UTC midnight).
- Config keys added under `coinbase:` and `binance:` in `sources.yaml` (told the Lead).

### 2026-11-26 — problem: append-mode flush cost grows with the day file
- `storage.write_partitioned(mode="append")` re-reads, de-duplicates and rewrites the whole day file on every
  flush. Fine for short captures; on an always-on machine BTC-USD L2 is ~0.5M rows/h, so by late UTC day every
  60 s flush rewrites ~10M rows. Mitigation now: raise `flush_every_s` (e.g. 300) for long runs, or `--no-l2`.
  Proposal to Lead: let storage write `<date>/part-<n>.parquet` parts (read_range concatenates) or an hourly
  partition for l2.

### 2026-11-26 — how-to: run the recorder unattended on an always-on machine
```bash
git clone <repo> && cd Order_Flow && make install
# foreground test
.venv/bin/python -m flow_model.data.recorders.coinbase_ws --minutes 5 --symbols BTC-USD ETH-USD
# run forever (0 = until SIGINT/SIGTERM); safe to restart any time: append mode de-duplicates
nohup .venv/bin/python -m flow_model.data.recorders.coinbase_ws --minutes 0 >> logs/coinbase_ws.stdout 2>&1 &
```
systemd user unit (`~/.config/systemd/user/coinbase-ws.service`), restarts on crash and at boot:
```ini
[Unit]
Description=Coinbase WS recorder (Order Flow research, read-only)
After=network-online.target
[Service]
WorkingDirectory=%h/Order_Flow
ExecStart=%h/Order_Flow/.venv/bin/python -m flow_model.data.recorders.coinbase_ws --minutes 0
Restart=always
RestartSec=30
KillSignal=SIGTERM
TimeoutStopSec=120
[Install]
WantedBy=default.target
```
`systemctl --user enable --now coinbase-ws && loginctl enable-linger $USER`. Alternative: cron
`@reboot` + an hourly `--minutes 60` job (each run flushes and exits cleanly; restarts cost one snapshot).
SIGTERM/SIGINT → final flush + stats file. Check `logs/coinbase_gaps.jsonl` daily. Disk: expect roughly
(measure from the stats file) a few hundred MB/day of parquet with L2; use `--no-l2` to keep only trades +
book-derived quotes. Binance: run `python -m flow_model.data.loaders.binance --days 2` once a day after 03:00 UTC.
