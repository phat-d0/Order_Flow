"""Coinbase Exchange WebSocket recorder (public channels only, read-only market data).

Records ``matches`` -> trades, ``level2_batch`` -> l2 rows + top-of-book quotes rebuilt from the book,
``ticker`` -> taker-side cross-check (and quotes if ``quotes_from: ticker``), ``heartbeat`` -> liveness and
trade-id gap checks. Flushes every ``flush_every_s`` through ``storage.write_partitioned`` (append +
de-duplicate, split by UTC date, so daily rollover is automatic and restarts are safe).

Gaps (trade-id holes, reconnects, stale feed, crossed book) are appended to
``<logs_dir>/coinbase_gaps.jsonl`` as ``{"start", "end", "kind", "symbol", "reason", ...}``.

CLI::

    python -m flow_model.data.recorders.coinbase_ws --minutes 30 --symbols BTC-USD ETH-USD
    python -m flow_model.data.recorders.coinbase_ws --minutes 0      # run until SIGINT/SIGTERM
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import pandas as pd

from flow_model.data import storage
from flow_model.data.loaders import coinbase as cb
from flow_model.settings import REPO_ROOT, list_instruments

log = logging.getLogger("coinbase_ws")


def _now() -> pd.Timestamp:
    return pd.Timestamp(time.time_ns(), unit="ns", tz="UTC")


def _iso(ts: pd.Timestamp | None) -> str | None:
    return None if ts is None else ts.isoformat()


# --------------------------------------------------------------------------- order book


@dataclass
class Book:
    """Price -> size per side, with incrementally tracked best bid/ask."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    best_bid: float | None = None
    best_ask: float | None = None

    def reset(self, bids: list[list[str]], asks: list[list[str]]) -> None:
        """Replace the book from a snapshot."""
        self.bids = {float(p): float(s) for p, s in bids if float(s) > 0}
        self.asks = {float(p): float(s) for p, s in asks if float(s) > 0}
        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None

    def apply(self, side: str, price: float, size: float) -> None:
        """Apply one level change (size = new total, 0 removes)."""
        levels = self.bids if side == "buy" else self.asks
        if size > 0:
            levels[price] = size
            if side == "buy" and (self.best_bid is None or price > self.best_bid):
                self.best_bid = price
            if side == "sell" and (self.best_ask is None or price < self.best_ask):
                self.best_ask = price
        else:
            levels.pop(price, None)
            if side == "buy" and price == self.best_bid:
                self.best_bid = max(self.bids) if self.bids else None
            if side == "sell" and price == self.best_ask:
                self.best_ask = min(self.asks) if self.asks else None

    def top(self) -> tuple[float, float, float, float] | None:
        """(bid_px, bid_sz, ask_px, ask_sz) or None if a side is empty."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_bid, self.bids[self.best_bid], self.best_ask, self.asks[self.best_ask]


# --------------------------------------------------------------------------- message processor


class FeedProcessor:
    """Turns Coinbase WS messages into schema rows, gap records and stats. No network, no disk."""

    def __init__(self, conf: dict[str, Any]):
        self.cfg = conf
        self.venue = conf.get("venue", cb.SOURCE)
        self.quotes_from = conf.get("quotes_from", "l2_book")
        self.record_l2 = bool(conf.get("record_l2", True))
        self.buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.gaps: list[dict[str, Any]] = []
        self.books: dict[str, Book] = {}
        self.last_trade: dict[str, tuple[int, pd.Timestamp]] = {}
        self.last_seq: dict[str, int] = {}
        self.last_top: dict[str, tuple[float, float, float, float]] = {}
        self.pending_backfill: list[tuple[str, int, int, dict[str, Any]]] = []
        self.ticker_side: dict[tuple[str, str], int] = {}
        self.stats: dict[str, Any] = {
            "messages": defaultdict(int),
            "rows": defaultdict(int),
            "side_check": {"agree": 0, "disagree": 0},
            "errors": [],
        }

    # -- gap bookkeeping
    def add_gap(self, start: Any, end: Any, kind: str, symbol: str | None, reason: str, **extra: Any) -> dict:
        """Record a gap (also logged)."""
        gap = {
            "start": _iso(start) if isinstance(start, pd.Timestamp) else start,
            "end": _iso(end) if isinstance(end, pd.Timestamp) else end,
            "kind": kind,
            "symbol": symbol,
            "reason": reason,
            "logged_at": _iso(_now()),
            **extra,
        }
        self.gaps.append(gap)
        log.warning("GAP %s", json.dumps(gap))
        return gap

    # -- dispatch
    def process(self, msg: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        """Handle one decoded message."""
        mtype = msg.get("type", "?")
        self.stats["messages"][mtype] += 1
        handler = getattr(self, f"_on_{mtype}", None)
        if handler is not None:
            handler(msg, recv_ts)

    def _check_seq(self, msg: dict[str, Any]) -> None:
        sym, seq = msg.get("product_id"), msg.get("sequence")
        if sym is None or seq is None:
            return
        prev = self.last_seq.get(sym)
        if prev is not None and int(seq) < prev:
            self.stats["messages"]["out_of_order"] += 1
        self.last_seq[sym] = max(int(seq), prev or 0)

    def _on_match(self, msg: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        self._check_seq(msg)
        row = cb.match_row(msg, recv_ts, self.venue)
        sym, tid = row["symbol"], int(msg["trade_id"])
        prev = self.last_trade.get(sym)
        if prev is not None:
            prev_id, prev_ts = prev
            if tid <= prev_id:
                self.stats["messages"]["duplicate_trade"] += 1
                return
            if tid > prev_id + 1:
                gap = self.add_gap(
                    prev_ts, row["ts"], "trades", sym,
                    f"trade_id gap: {tid - prev_id - 1} missing (ids {prev_id + 1}..{tid - 1})",
                    missing=tid - prev_id - 1, backfilled=0,
                )
                self.pending_backfill.append((sym, prev_id, tid, gap))
        self.last_trade[sym] = (tid, row["ts"])
        self.buffers["trades"].append(row)
        side = self.ticker_side.pop((sym, str(tid)), None)
        if side is not None:
            self._side_check(side, row["side"])

    _on_last_match = _on_match

    def _side_check(self, ticker_taker: int, match_aggressor: int) -> None:
        key = "agree" if ticker_taker == match_aggressor else "disagree"
        self.stats["side_check"][key] += 1

    def _on_ticker(self, msg: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        self._check_seq(msg)
        sym, tid = msg.get("product_id"), msg.get("trade_id")
        if tid is not None:
            # ticker for trade X normally arrives right after match X; keep the map small
            matched = [r for r in self.buffers["trades"][-50:] if r["symbol"] == sym and r["trade_id"] == str(tid)]
            if matched:
                self._side_check(cb.taker_side(msg.get("side", "")), matched[-1]["side"])
            elif len(self.ticker_side) < 10_000:
                self.ticker_side[(sym, str(tid))] = cb.taker_side(msg.get("side", ""))
        if self.quotes_from == "ticker":
            row = cb.ticker_quote_row(msg, recv_ts, self.venue)
            if row is not None:
                self.buffers["quotes"].append(row)

    def _on_snapshot(self, msg: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        sym = msg["product_id"]
        book = self.books.setdefault(sym, Book())
        book.reset(msg.get("bids", []), msg.get("asks", []))
        if self.record_l2:
            self.buffers["l2"].extend(cb.l2_rows(msg, recv_ts, self.venue))
        self._emit_top(sym, cb.parse_time(msg["time"]) if msg.get("time") else recv_ts, recv_ts)

    def _on_l2update(self, msg: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        sym = msg["product_id"]
        book = self.books.get(sym)
        if book is None:
            self.stats["messages"]["l2update_before_snapshot"] += 1
            return
        for side, price, size in msg.get("changes", []):
            book.apply(side, float(price), float(size))
        if self.record_l2:
            self.buffers["l2"].extend(cb.l2_rows(msg, recv_ts, self.venue))
        self._emit_top(sym, cb.parse_time(msg["time"]) if msg.get("time") else recv_ts, recv_ts)

    def _emit_top(self, sym: str, ts: pd.Timestamp, recv_ts: pd.Timestamp) -> None:
        if self.quotes_from != "l2_book":
            return
        top = self.books[sym].top()
        if top is None or top == self.last_top.get(sym):
            return
        if top[0] > top[2]:
            self.stats["messages"]["crossed_book"] += 1
            if self.stats["messages"]["crossed_book"] <= 20:
                self.add_gap(ts, ts, "quotes", sym, f"crossed book bid {top[0]} > ask {top[2]}; quote skipped")
            return
        self.last_top[sym] = top
        self.buffers["quotes"].append(
            {"ts": ts, "symbol": sym, "source": cb.SOURCE, "venue": self.venue, "bid_px": top[0],
             "bid_sz": top[1], "ask_px": top[2], "ask_sz": top[3], "recv_ts": recv_ts}
        )

    def _on_heartbeat(self, msg: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        sym, last_id = msg.get("product_id"), msg.get("last_trade_id")
        prev = self.last_trade.get(sym)
        if last_id is None or prev is None:
            return
        if int(last_id) > prev[0]:
            # Feed is ordered, so the heartbeat saying a newer trade exists means we missed it.
            self.stats["messages"]["heartbeat_ahead"] += 1

    def _on_error(self, msg: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        err = {"at": _iso(recv_ts), "message": msg.get("message"), "reason": msg.get("reason")}
        self.stats["errors"].append(err)
        log.error("server error: %s", err)

    def _on_subscriptions(self, msg: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        log.info("subscribed: %s", json.dumps(msg.get("channels")))

    # -- connection lifecycle
    def on_disconnect(self) -> None:
        """Forget book state; the next connection's snapshot rebuilds it."""
        self.books.clear()
        self.last_top.clear()
        self.ticker_side.clear()

    def drain(self) -> dict[str, list[dict[str, Any]]]:
        """Hand over buffered rows and start new buffers."""
        out, self.buffers = self.buffers, defaultdict(list)
        for kind, rows in out.items():
            self.stats["rows"][kind] += len(rows)
        return out


# --------------------------------------------------------------------------- recorder


class Recorder:
    """Connect / subscribe / reconnect loop around a ``FeedProcessor``; periodic flushing to the store."""

    def __init__(
        self,
        symbols: list[str],
        conf: dict[str, Any] | None = None,
        root: Path | None = None,
        logs_dir: Path | None = None,
        minutes: float = 30.0,
    ):
        self.cfg = conf or cb.cfg()
        self.symbols = symbols
        self.root = root
        self.logs_dir = Path(logs_dir or REPO_ROOT / self.cfg["logs_dir"])
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.deadline = None if minutes <= 0 else time.monotonic() + minutes * 60
        self.proc = FeedProcessor(self.cfg)
        self.rest = cb.CoinbaseREST(self.cfg)
        self.stop = asyncio.Event()
        self.reconnects = 0
        self.started = _now()
        self._gaps_written = 0
        self._last_msg_ts: pd.Timestamp | None = None

    @property
    def gaps_path(self) -> Path:
        """JSONL file every gap is appended to."""
        return self.logs_dir / "coinbase_gaps.jsonl"

    def _channels(self) -> list[str]:
        chans = list(self.cfg["channels"])
        if not self.proc.record_l2 and self.proc.quotes_from != "l2_book":
            chans = [c for c in chans if not c.startswith("level2")]
        return chans

    def _time_left(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    async def run(self) -> dict[str, Any]:
        """Record until the deadline or a stop signal; returns the stats summary."""
        import websockets

        backoff = list(self.cfg["reconnect_backoff_s"])
        attempt = 0
        flusher = asyncio.create_task(self._flush_loop())
        ssl_ctx = ssl.create_default_context()
        try:
            while not self.stop.is_set() and (self._time_left() is None or self._time_left() > 0):
                connected_at = time.monotonic()
                reason = "closed"
                try:
                    async with websockets.connect(
                        self.cfg["ws_url"], ssl=ssl_ctx, max_size=int(self.cfg["max_message_bytes"]),
                        open_timeout=float(self.cfg["open_timeout_s"]), ping_interval=20, ping_timeout=20,
                    ) as ws:
                        await ws.send(json.dumps(
                            {"type": "subscribe", "product_ids": self.symbols, "channels": self._channels()}
                        ))
                        log.info("connected; channels=%s symbols=%s", self._channels(), self.symbols)
                        reason = await self._consume(ws)
                except Exception as exc:  # noqa: BLE001 - any network error -> reconnect
                    reason = f"{type(exc).__name__}: {exc}"
                self.proc.on_disconnect()
                if self.stop.is_set() or (self._time_left() is not None and self._time_left() <= 0):
                    break
                if time.monotonic() - connected_at >= float(self.cfg["backoff_reset_after_s"]):
                    attempt = 0
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
                self.reconnects += 1
                self.proc.add_gap(self._last_msg_ts, None, "all", None,
                                  f"disconnect ({reason}); reconnect #{self.reconnects} in {delay}s; "
                                  "book rebuilt from new snapshot", reconnect=True)
                log.warning("disconnected: %s; reconnecting in %ss", reason, delay)
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stop.set()
            await flusher
            await self.flush()
        return self.summary()

    async def _consume(self, ws: Any) -> str:
        timeout = float(self.cfg["heartbeat_timeout_s"])
        while not self.stop.is_set():
            left = self._time_left()
            if left is not None and left <= 0:
                return "deadline"
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(timeout, left) if left else timeout)
            except asyncio.TimeoutError:
                if left is not None and left <= timeout:
                    return "deadline"
                self.proc.add_gap(self._last_msg_ts, _now(), "all", None, f"stale: no message for {timeout}s")
                return "stale"
            recv_ts = _now()
            self._last_msg_ts = recv_ts
            try:
                self.proc.process(json.loads(raw), recv_ts)
            except Exception as exc:  # noqa: BLE001 - one bad message must not kill the recorder
                self.proc.stats["messages"]["parse_error"] += 1
                log.exception("bad message: %s", exc)
        return "stopped"

    async def _flush_loop(self) -> None:
        every = float(self.cfg["flush_every_s"])
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=every)
            except asyncio.TimeoutError:
                await self.flush()

    async def flush(self) -> None:
        """Back-fill pending trade gaps, then write buffers and new gap records."""
        await self._backfill()
        batches = self.proc.drain()
        for kind, rows in batches.items():
            if rows:
                try:
                    paths = await asyncio.to_thread(storage.write_partitioned, cb.frame(rows, kind), kind, self.root)
                    log.info("flushed %d %s rows -> %s", len(rows), kind, [p.name for p in paths])
                except Exception as exc:  # noqa: BLE001 - keep recording; report the loss
                    self.proc.add_gap(rows[0]["ts"], rows[-1]["ts"], kind, None, f"flush failed: {exc}",
                                      lost_rows=len(rows))
        new = self.proc.gaps[self._gaps_written:]
        if new:
            with open(self.gaps_path, "a", encoding="utf-8") as fh:
                for gap in new:
                    fh.write(json.dumps(gap) + "\n")
            self._gaps_written = len(self.proc.gaps)

    async def _backfill(self) -> None:
        if not self.cfg.get("backfill_trades_on_gap"):
            self.proc.pending_backfill.clear()
            return
        pending, self.proc.pending_backfill = self.proc.pending_backfill, []
        for sym, lo, hi, gap in pending:
            try:
                items = await asyncio.to_thread(self.rest.trades_between_ids, sym, lo, hi)
            except Exception as exc:  # noqa: BLE001
                gap["backfill_error"] = str(exc)
                continue
            self.proc.buffers["trades"].extend(cb.rest_trade_row(t, sym, self.proc.venue) for t in items)
            gap["backfilled"] = len(items)
            gap["reason"] += f"; back-filled {len(items)} from REST"
            log.info("back-filled %d trades for %s (%d..%d)", len(items), sym, lo, hi)

    def summary(self) -> dict[str, Any]:
        """Run statistics (rows per kind, message counts and rates, gaps, reconnects)."""
        secs = max((_now() - self.started).total_seconds(), 1e-9)
        msgs = dict(self.proc.stats["messages"])
        return {
            "started": _iso(self.started),
            "ended": _iso(_now()),
            "seconds": round(secs, 1),
            "symbols": self.symbols,
            "channels": self._channels(),
            "quotes_from": self.proc.quotes_from,
            "rows": dict(self.proc.stats["rows"]),
            "messages": msgs,
            "msg_rate_per_s": {k: round(v / secs, 3) for k, v in msgs.items()},
            "side_check_ticker_vs_match": self.proc.stats["side_check"],
            "reconnects": self.reconnects,
            "gaps": len(self.proc.gaps),
            "gap_records": self.proc.gaps,
            "server_errors": self.proc.stats["errors"],
        }


# --------------------------------------------------------------------------- CLI


def _setup_logging(logs_dir: Path, verbose: bool) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fmt.converter = time.gmtime
    file_h = TimedRotatingFileHandler(logs_dir / "coinbase_ws.log", when="midnight", utc=True, backupCount=30)
    file_h.setFormatter(fmt)
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [file_h, stream_h]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    conf = cb.cfg()
    p = argparse.ArgumentParser(description="Record Coinbase Exchange public WS data (research only).")
    p.add_argument("--minutes", type=float, default=30.0, help="capture length; 0 = until SIGINT/SIGTERM")
    p.add_argument("--symbols", nargs="+", default=list_instruments(asset_class="crypto"))
    p.add_argument("--root", type=Path, default=None, help="store root (default: sources.yaml storage.root)")
    p.add_argument("--logs-dir", type=Path, default=REPO_ROOT / conf["logs_dir"])
    p.add_argument("--no-l2", action="store_true", help="do not store l2 rows (quotes still from the book)")
    p.add_argument("--quotes-from", choices=["l2_book", "ticker"], default=conf["quotes_from"])
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    conf = {**conf, "quotes_from": args.quotes_from, "record_l2": conf["record_l2"] and not args.no_l2}
    _setup_logging(args.logs_dir, args.verbose)
    rec = Recorder(args.symbols, conf, args.root, args.logs_dir, args.minutes)

    async def _run() -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, rec.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        return await rec.run()

    summary = asyncio.run(_run())
    stats_path = args.logs_dir / f"coinbase_ws_stats_{summary['started'][:19].replace(':', '')}.json"
    stats_path.write_text(json.dumps(summary, indent=2, default=str))
    brief = {k: v for k, v in summary.items() if k != "gap_records"}
    log.info("done: %s (stats -> %s)", json.dumps(brief, default=str), stats_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
