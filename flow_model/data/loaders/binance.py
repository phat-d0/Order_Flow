"""Binance public data archive (https://data.binance.vision) — secondary crypto history.

Two loaders, kept as **separate sources** so they never mix with each other or with Coinbase:

* ``BinanceSpotLoader`` (source ``binance``, venue ``binance_spot``): spot daily ``aggTrades`` -> trades.
  Symbols are Binance's (``BTCUSDT``: USDT-quoted, a different venue). ``BTC-USD`` is accepted and mapped
  via ``instruments.yaml`` ``binance_symbol``, but rows are always labelled ``BTCUSDT``.
* ``BinanceUMLoader`` (source ``binance_um``, venue ``binance_usdm``): USD-M futures daily ``bookTicker``
  -> quotes. Binance stopped publishing these after ``futures_bookticker_last_date`` (2024-03-30).

aggTrades CSV (no header): agg_id, price, qty, first_id, last_id, transact_time, is_buyer_maker,
is_best_match. ``is_buyer_maker`` True -> the seller was the aggressor (side -1). transact_time is µs
since 2025-01-01 and ms before; detected per file by magnitude.

CLI: ``python -m flow_model.data.loaders.binance --symbols BTC-USD ETH-USD --days 3``
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd
import requests

from flow_model.data import schema, storage
from flow_model.data.loaders.base import Loader, NotSupported, SourceCheck, SourceUnavailable
from flow_model.settings import load_config

AGG_COLS = ["agg_id", "price", "qty", "first_id", "last_id", "transact_time", "is_buyer_maker", "is_best_match"]
BOOK_COLS = ["update_id", "best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty",
             "transaction_time", "event_time"]


def cfg() -> dict[str, Any]:
    """The ``binance`` section of ``sources.yaml``."""
    return load_config("sources")["binance"]


def binance_symbol(symbol: str) -> str:
    """``BTC-USD`` -> ``BTCUSDT`` via instruments.yaml; Binance symbols pass through unchanged."""
    meta = load_config("instruments")["instruments"].get(symbol, {})
    return str(meta.get("binance_symbol", symbol)).upper()


def epoch_to_ts(values: pd.Series, us_threshold: float) -> pd.Series:
    """Epoch integers (ms or µs, decided on the first value) -> ``datetime64[ns, UTC]``."""
    v = pd.to_numeric(values).astype("int64")
    unit = "us" if len(v) and float(v.iloc[0]) > us_threshold else "ms"
    return pd.to_datetime(v, unit=unit, utc=True).astype("datetime64[ns, UTC]")


def _bool(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().isin(["true", "1"])


def _has_header(first_line: bytes) -> bool:
    head = first_line.split(b",")[0].strip()
    return not head.lstrip(b"-").isdigit()


def parse_aggtrades_csv(raw: bytes | io.BytesIO, symbol: str, conf: dict[str, Any] | None = None) -> pd.DataFrame:
    """aggTrades CSV bytes -> schema ``trades`` frame (sorted, ``recv_ts`` null)."""
    conf = conf or cfg()
    data = raw.getvalue() if isinstance(raw, io.BytesIO) else raw
    if not data.strip():
        return schema.empty_frame("trades")
    header = 0 if _has_header(data.split(b"\n", 1)[0]) else None
    df = pd.read_csv(io.BytesIO(data), header=header, names=AGG_COLS if header is None else None)
    if header is not None:
        df.columns = AGG_COLS[: len(df.columns)]
    out = pd.DataFrame({
        "ts": epoch_to_ts(df["transact_time"], float(conf["us_threshold"])),
        "symbol": symbol,
        "source": "binance",
        "venue": conf["spot_venue"],
        "price": df["price"].astype("float64"),
        "size": df["qty"].astype("float64"),
        "side": np.where(_bool(df["is_buyer_maker"]), -1, 1).astype("int8"),
        "trade_id": df["agg_id"].astype("int64").astype(str),
        "recv_ts": pd.NaT,
    })
    return schema.coerce(out, "trades").sort_values("ts", kind="stable").reset_index(drop=True)


def parse_bookticker_chunks(chunks: Iterator[pd.DataFrame], symbol: str, conf: dict[str, Any],
                            start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """bookTicker CSV chunks -> schema ``quotes`` frame within ``[start, end)``."""
    parts = []
    for df in chunks:
        ts = epoch_to_ts(df["transaction_time"], float(conf["us_threshold"]))
        keep = (ts >= start) & (ts < end) & (df["best_bid_price"] <= df["best_ask_price"])
        if keep.any():
            parts.append(pd.DataFrame({
                "ts": ts[keep], "symbol": symbol, "source": conf["futures_source"],
                "venue": conf["futures_venue"], "bid_px": df.loc[keep, "best_bid_price"],
                "bid_sz": df.loc[keep, "best_bid_qty"], "ask_px": df.loc[keep, "best_ask_price"],
                "ask_sz": df.loc[keep, "best_ask_qty"], "recv_ts": pd.NaT,
            }))
    if not parts:
        return schema.empty_frame("quotes")
    out = schema.coerce(pd.concat(parts, ignore_index=True), "quotes")
    return out.sort_values("ts", kind="stable").reset_index(drop=True)


class _Archive:
    """Downloads one daily zip, verifies its .CHECKSUM, paces requests."""

    def __init__(self, conf: dict[str, Any], session: requests.Session | None = None):
        self.cfg = conf
        self.session = session or requests.Session()
        self._last = 0.0

    def url(self, path: str, symbol: str, kind: str, day: date) -> str:
        return f"{self.cfg['archive_url']}/{path}/{symbol}/{symbol}-{kind}-{day.isoformat()}.zip"

    def _get(self, url: str, stream: bool = False) -> requests.Response:
        wait = self._last + float(self.cfg["request_pause_s"]) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            resp = self.session.get(url, timeout=float(self.cfg["download_timeout_s"]), stream=stream)
        except requests.RequestException as exc:
            raise SourceUnavailable(f"GET {url}: {type(exc).__name__}: {exc}") from exc
        if resp.status_code == 404:
            raise SourceUnavailable(f"GET {url}: 404 (file not published)")
        if resp.status_code != 200:
            raise SourceUnavailable(f"GET {url}: HTTP {resp.status_code}")
        return resp

    def download(self, url: str, dest: Path) -> Path:
        """Stream ``url`` to ``dest`` and verify the SHA-256 checksum file if configured."""
        sha = hashlib.sha256()
        with self._get(url, stream=True) as resp, open(dest, "wb") as fh:
            for block in resp.iter_content(chunk_size=1 << 20):
                fh.write(block)
                sha.update(block)
        if self.cfg.get("verify_checksum"):
            expected = self._get(url + ".CHECKSUM").text.split()[0].strip().lower()
            if expected != sha.hexdigest():
                raise SourceUnavailable(f"checksum mismatch for {url}")
        return dest


def _day_range(start: datetime, end: datetime) -> list[date]:
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    lo = lo.tz_localize("UTC") if lo.tzinfo is None else lo.tz_convert("UTC")
    hi = hi.tz_localize("UTC") if hi.tzinfo is None else hi.tz_convert("UTC")
    days, d = [], lo.normalize()
    while d < hi:
        days.append(d.date())
        d += pd.Timedelta(days=1)
    return days


def _utc(value: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


class BinanceSpotLoader(Loader):
    """Binance spot aggTrades from the public daily archive (venue ``binance_spot``)."""

    source = "binance"
    venue = "binance_spot"
    tier = "A"
    kinds = frozenset({"trades"})

    def __init__(self, conf: dict[str, Any] | None = None, session: requests.Session | None = None):
        self.cfg = conf or cfg()
        self.venue = self.cfg["spot_venue"]
        self.archive = _Archive(self.cfg, session)

    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Trades in ``[start, end)``; one daily file per UTC date (raises if a file is not published)."""
        self._require(kind)
        sym = binance_symbol(symbol)
        lo, hi = _utc(start), _utc(end)
        parts = []
        with tempfile.TemporaryDirectory() as tmp:
            for day in _day_range(start, end):
                url = self.archive.url(self.cfg["spot_aggtrades_path"], sym, "aggTrades", day)
                path = self.archive.download(url, Path(tmp) / Path(url).name)
                with zipfile.ZipFile(path) as zf:
                    frame = parse_aggtrades_csv(zf.read(zf.namelist()[0]), sym, self.cfg)
                parts.append(frame[(frame["ts"] >= lo) & (frame["ts"] < hi)])
                path.unlink()
        if not parts:
            return schema.empty_frame("trades")
        return pd.concat(parts, ignore_index=True).sort_values("ts", kind="stable").reset_index(drop=True)

    def load(self, kind: str, symbol: str, start: datetime, end: datetime, root: Path | None = None,
             fetch_missing: bool = True) -> pd.DataFrame:
        """As ``Loader.load`` but with the symbol mapped to Binance's (``BTC-USD`` -> ``BTCUSDT``)."""
        return super().load(kind, binance_symbol(symbol), start, end, root, fetch_missing)

    def check(self) -> SourceCheck:
        """HEAD yesterday's BTCUSDT aggTrades file. Never raises."""
        day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        url = self.archive.url(self.cfg["spot_aggtrades_path"], "BTCUSDT", "aggTrades", day)
        try:
            resp = self.archive.session.head(url, timeout=float(self.cfg["download_timeout_s"]))
            ok = resp.status_code == 200
            return SourceCheck(self.source, ok, url, f"HTTP {resp.status_code}",
                               extra={"content_length": resp.headers.get("content-length")})
        except Exception as exc:  # noqa: BLE001 - check() must never raise
            return SourceCheck(self.source, False, url, f"{type(exc).__name__}: {exc}")


class BinanceUMLoader(Loader):
    """Binance USD-M futures bookTicker (top of book) — archive ends at ``futures_bookticker_last_date``."""

    source = "binance_um"
    venue = "binance_usdm"
    tier = "A"
    kinds = frozenset({"quotes"})

    def __init__(self, conf: dict[str, Any] | None = None, session: requests.Session | None = None):
        self.cfg = conf or cfg()
        self.source = self.cfg["futures_source"]
        self.venue = self.cfg["futures_venue"]
        self.archive = _Archive(self.cfg, session)
        self.last_day = date.fromisoformat(str(self.cfg["futures_bookticker_last_date"]))

    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Quotes in ``[start, end)``. Files are large (~100-350 MB zipped per day); read in chunks."""
        self._require(kind)
        sym = binance_symbol(symbol)
        days = _day_range(start, end)
        if days and days[0] > self.last_day:
            raise NotSupported(f"binance bookTicker archive ends {self.last_day}; requested from {days[0]}")
        lo, hi = _utc(start), _utc(end)
        parts = []
        with tempfile.TemporaryDirectory() as tmp:
            for day in [d for d in days if d <= self.last_day]:
                url = self.archive.url(self.cfg["futures_bookticker_path"], sym, "bookTicker", day)
                path = self.archive.download(url, Path(tmp) / Path(url).name)
                with zipfile.ZipFile(path) as zf, zf.open(zf.namelist()[0]) as fh:
                    chunks = pd.read_csv(fh, chunksize=int(self.cfg["csv_chunk_rows"]))
                    parts.append(parse_bookticker_chunks(chunks, sym, self.cfg, lo, hi))
                path.unlink()
        if not parts:
            return schema.empty_frame("quotes")
        return pd.concat(parts, ignore_index=True).sort_values("ts", kind="stable").reset_index(drop=True)

    def load(self, kind: str, symbol: str, start: datetime, end: datetime, root: Path | None = None,
             fetch_missing: bool = True) -> pd.DataFrame:
        """As ``Loader.load`` with Binance symbol mapping."""
        return super().load(kind, binance_symbol(symbol), start, end, root, fetch_missing)

    def check(self) -> SourceCheck:
        """HEAD the last published BTCUSDT bookTicker file. Never raises."""
        url = self.archive.url(self.cfg["futures_bookticker_path"], "BTCUSDT", "bookTicker", self.last_day)
        try:
            resp = self.archive.session.head(url, timeout=float(self.cfg["download_timeout_s"]))
            return SourceCheck(self.source, resp.status_code == 200, url, f"HTTP {resp.status_code}",
                               extra={"last_day": self.last_day.isoformat()})
        except Exception as exc:  # noqa: BLE001
            return SourceCheck(self.source, False, url, f"{type(exc).__name__}: {exc}")


def download_recent(symbols: list[str], days: int, root: Path | None = None) -> list[dict[str, Any]]:
    """Fetch the most recent ``days`` published daily aggTrades files per symbol into the store.

    Returns one report row per (symbol, day) with ``status`` ok / unavailable and the row count.
    """
    loader = BinanceSpotLoader()
    today = datetime.now(timezone.utc).date()
    report = []
    for symbol in symbols:
        got, day = 0, today
        while got < days and (today - day).days <= days + 2:
            start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            entry = {"symbol": binance_symbol(symbol), "day": day.isoformat()}
            try:
                frame = loader.fetch("trades", symbol, start, start + timedelta(days=1))
                paths = storage.write_partitioned(frame, "trades", root=root)
                entry.update(status="ok", rows=len(frame), files=[str(p) for p in paths])
                got += 1
            except SourceUnavailable as exc:
                entry.update(status="unavailable", reason=str(exc))
            report.append(entry)
            day -= timedelta(days=1)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI: download recent spot aggTrades days into the store and print a JSON report."""
    p = argparse.ArgumentParser(description="Download Binance spot aggTrades (public archive) into the store.")
    p.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD"])
    p.add_argument("--days", type=int, default=int(cfg()["default_days"]))
    p.add_argument("--root", type=Path, default=None)
    args = p.parse_args(argv)
    print(json.dumps(download_recent(args.symbols, args.days, args.root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
