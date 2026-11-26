"""Coinbase Exchange loader (Tier A): message parsers + REST trade history.

Coinbase's ``side`` on ``match`` / ``last_match`` messages and on REST ``/products/<id>/trades`` is the
**maker** order's side. The aggressor (taker) is the opposite: maker ``"buy"`` means a seller hit the bid
(``side = -1``); maker ``"sell"`` means a buyer lifted the ask (``side = +1``). The ``ticker`` channel's
``side`` is the taker side and is used only to cross-check this mapping.

Historical quotes / L2 are not offered by Coinbase: they exist only where the live recorder
(``flow_model.data.recorders.coinbase_ws``) captured them, so ``fetch`` raises ``NotSupported`` for them;
read those with ``load(..., fetch_missing=False)``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd
import requests

from flow_model.data import schema
from flow_model.data.loaders.base import Loader, NotSupported, SourceCheck, SourceUnavailable
from flow_model.settings import load_config

SOURCE = "coinbase"


def cfg() -> dict[str, Any]:
    """The ``coinbase`` section of ``sources.yaml``."""
    return load_config("sources")["coinbase"]


# --------------------------------------------------------------------------- message parsing (pure)


def aggressor_side(maker_side: str) -> int:
    """Aggressor sign from Coinbase's maker side: ``"buy"`` -> -1, ``"sell"`` -> +1, else 0."""
    return {"buy": -1, "sell": 1}.get(str(maker_side).lower(), 0)


def taker_side(ticker_side: str) -> int:
    """Aggressor sign from the ``ticker`` channel, whose ``side`` is already the taker side."""
    return {"buy": 1, "sell": -1}.get(str(ticker_side).lower(), 0)


def parse_time(value: str) -> pd.Timestamp:
    """Coinbase ISO-8601 time (µs, sometimes ns, ``Z`` suffix) -> UTC ``Timestamp``."""
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def match_row(msg: dict[str, Any], recv_ts: pd.Timestamp | None, venue: str = SOURCE) -> dict[str, Any]:
    """One ``trades`` row from a WS ``match``/``last_match`` message."""
    return {
        "ts": parse_time(msg["time"]),
        "symbol": msg["product_id"],
        "source": SOURCE,
        "venue": venue,
        "price": float(msg["price"]),
        "size": float(msg["size"]),
        "side": aggressor_side(msg["side"]),
        "trade_id": str(msg["trade_id"]),
        "recv_ts": recv_ts,
    }


def rest_trade_row(item: dict[str, Any], symbol: str, venue: str = SOURCE) -> dict[str, Any]:
    """One ``trades`` row from a REST ``/products/<id>/trades`` item (``recv_ts`` null: historical)."""
    return {
        "ts": parse_time(item["time"]),
        "symbol": symbol,
        "source": SOURCE,
        "venue": venue,
        "price": float(item["price"]),
        "size": float(item["size"]),
        "side": aggressor_side(item["side"]),
        "trade_id": str(item["trade_id"]),
        "recv_ts": None,
    }


def ticker_quote_row(
    msg: dict[str, Any], recv_ts: pd.Timestamp | None, venue: str = SOURCE
) -> dict[str, Any] | None:
    """A ``quotes`` row from a ``ticker`` message (best bid/ask + sizes), or None if incomplete."""
    try:
        row = {
            "ts": parse_time(msg["time"]),
            "symbol": msg["product_id"],
            "source": SOURCE,
            "venue": venue,
            "bid_px": float(msg["best_bid"]),
            "bid_sz": float(msg.get("best_bid_size") or "nan"),
            "ask_px": float(msg["best_ask"]),
            "ask_sz": float(msg.get("best_ask_size") or "nan"),
            "recv_ts": recv_ts,
        }
    except (KeyError, TypeError, ValueError):
        return None
    if row["bid_px"] <= 0 or row["ask_px"] <= 0 or row["bid_px"] > row["ask_px"]:
        return None
    return row


def l2_rows(
    msg: dict[str, Any], recv_ts: pd.Timestamp | None, venue: str = SOURCE
) -> list[dict[str, Any]]:
    """``l2`` rows from a ``snapshot`` (is_snapshot=True) or ``l2update`` message."""
    ts = parse_time(msg["time"]) if msg.get("time") else recv_ts
    base = {"ts": ts, "symbol": msg["product_id"], "source": SOURCE, "venue": venue, "recv_ts": recv_ts}
    if msg["type"] == "snapshot":
        levels = [(1, p, s) for p, s in msg.get("bids", [])] + [(-1, p, s) for p, s in msg.get("asks", [])]
        snap = True
    else:
        levels = [(1 if side == "buy" else -1, p, s) for side, p, s in msg.get("changes", [])]
        snap = False
    return [
        {**base, "book_side": bs, "price": float(p), "size": float(s), "is_snapshot": snap}
        for bs, p, s in levels
    ]


def frame(rows: Iterable[dict[str, Any]], kind: str) -> pd.DataFrame:
    """Rows -> schema-coerced frame sorted by ``ts`` (stable)."""
    rows = list(rows)
    if not rows:
        return schema.empty_frame(kind)
    out = schema.coerce(pd.DataFrame(rows), kind)
    return out.sort_values("ts", kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------- REST client


class CoinbaseREST:
    """Minimal read-only public REST client with pacing and retries (no auth, no account endpoints)."""

    def __init__(self, conf: dict[str, Any] | None = None, session: requests.Session | None = None):
        self.cfg = conf or cfg()
        self.session = session or requests.Session()
        self._last = 0.0

    def get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        """Paced GET with retries on 429/5xx/network errors. Raises ``SourceUnavailable``."""
        url = self.cfg["rest_url"].rstrip("/") + path
        last_err = ""
        for attempt in range(int(self.cfg["rest_max_retries"]) + 1):
            wait = self._last + float(self.cfg["rest_pause_s"]) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=float(self.cfg["rest_timeout_s"]))
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return resp
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(float(self.cfg["rest_retry_backoff_s"]) * (2**attempt))
        raise SourceUnavailable(f"GET {url} {params or ''} failed: {last_err}")

    def trades_page(self, symbol: str, after: int | None = None) -> list[dict[str, Any]]:
        """Up to ``rest_page_limit`` trades with ``trade_id < after`` (newest first)."""
        params: dict[str, Any] = {"limit": int(self.cfg["rest_page_limit"])}
        if after is not None:
            params["after"] = int(after)
        return self.get(f"/products/{symbol}/trades", params).json()

    def latest_trade_id(self, symbol: str) -> int:
        """Most recent trade id."""
        page = self.get(f"/products/{symbol}/trades", {"limit": 1}).json()
        if not page:
            raise SourceUnavailable(f"no trades returned for {symbol}")
        return int(page[0]["trade_id"])

    def trade_time(self, symbol: str, trade_id: int) -> pd.Timestamp | None:
        """Timestamp of ``trade_id`` (None if absent)."""
        page = self.get(f"/products/{symbol}/trades", {"limit": 1, "after": int(trade_id) + 1}).json()
        if not page or int(page[0]["trade_id"]) != int(trade_id):
            return None
        return parse_time(page[0]["time"])

    def trades_between_ids(self, symbol: str, lo_id: int, hi_id: int) -> list[dict[str, Any]]:
        """Raw REST trades with ``lo_id < trade_id < hi_id`` (used to back-fill WS gaps)."""
        out: list[dict[str, Any]] = []
        cursor = hi_id
        while cursor - 1 > lo_id:
            page = self.trades_page(symbol, after=cursor)
            if not page:
                break
            out.extend(t for t in page if lo_id < int(t["trade_id"]) < hi_id)
            cursor = min(int(t["trade_id"]) for t in page)
            if len(out) >= int(self.cfg["backfill_max_trades"]):
                break
        return out

    def first_id_at_or_after(self, symbol: str, when: pd.Timestamp, hi: int) -> int:
        """Smallest trade id whose time is ``>= when`` (bisection over ids ``[1, hi]``)."""
        lo = 1
        for _ in range(int(self.cfg["bisect_max_steps"])):
            if lo >= hi:
                break
            mid = (lo + hi) // 2
            t = self.trade_time(symbol, mid)
            if t is None or t < when:
                lo = mid + 1
            else:
                hi = mid
        return lo


# --------------------------------------------------------------------------- Loader


class CoinbaseLoader(Loader):
    """Coinbase Exchange: REST trade history; quotes/L2 only from the live recorder."""

    source = SOURCE
    venue = SOURCE
    tier = "A"
    kinds = frozenset({"trades", "quotes", "l2"})

    def __init__(self, rest: CoinbaseREST | None = None):
        self.cfg = cfg()
        self.venue = self.cfg.get("venue", SOURCE)
        self.rest = rest or CoinbaseREST(self.cfg)

    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Trades in ``[start, end)`` via REST pagination. Quotes/L2 raise ``NotSupported``."""
        self._require(kind)
        if kind != "trades":
            raise NotSupported(
                f"coinbase has no historical {kind}; they exist only where coinbase_ws recorded them "
                "(use load(..., fetch_missing=False))"
            )
        lo_t, hi_t = _ts(start), _ts(end)
        hi_id = self.rest.first_id_at_or_after(symbol, hi_t, self.rest.latest_trade_id(symbol) + 1)
        rows: list[dict[str, Any]] = []
        cursor = hi_id
        while True:
            page = self.rest.trades_page(symbol, after=cursor)
            if not page:
                break
            batch = [rest_trade_row(t, symbol, self.venue) for t in page]
            rows.extend(r for r in batch if lo_t <= r["ts"] < hi_t)
            cursor = min(int(t["trade_id"]) for t in page)
            if min(r["ts"] for r in batch) < lo_t or cursor <= 1:
                break
        return frame(rows, "trades")

    def check(self) -> SourceCheck:
        """GET /time and one trade for BTC-USD. Never raises."""
        endpoint = self.cfg["rest_url"]
        try:
            server = self.rest.get("/time").json()
            last = self.rest.get("/products/BTC-USD/trades", {"limit": 1}).json()
            return SourceCheck(
                SOURCE, True, endpoint, "REST reachable", extra={"server_time": server, "last_trade": last}
            )
        except Exception as exc:  # noqa: BLE001 - check() must never raise
            return SourceCheck(SOURCE, False, endpoint, f"{type(exc).__name__}: {exc}")


def _ts(value: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize(timezone.utc) if ts.tzinfo is None else ts.tz_convert("UTC")
