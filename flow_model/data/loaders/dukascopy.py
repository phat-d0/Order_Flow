"""Dukascopy historical tick quotes (FX, Tier B: one venue, bid/ask only, no trades).

Source: ``{base_url}/{SYMBOL}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5`` — one file per UTC hour.
The month in the URL is **0-based**. Each file is LZMA ("alone" format) compressed; the payload is a
sequence of 20-byte big-endian records::

    uint32 ms_offset   milliseconds since the start of the hour (UTC)
    uint32 ask         price in points (price = points / price_scale; EURUSD scale 1e5)
    uint32 bid
    float32 ask_vol    Dukascopy's own liquidity at the ask (their units, not market volume)
    float32 bid_vol

An empty body (or 404) means no ticks in that hour (weekend, holiday, outage). Volumes are Dukascopy's
own and are written as ``ask_sz`` / ``bid_sz``; ``venue`` is ``"dukascopy"``. There are no trades.

Every parameter (URL, scales, pacing, backoff, timeouts) comes from ``config/sources.yaml`` -> ``dukascopy``.
Network access is sequential with a pause between requests and exponential backoff on 429/5xx.
"""

from __future__ import annotations

import argparse
import json
import logging
import lzma
import struct
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from flow_model import settings
from flow_model.data import schema, storage
from flow_model.data.loaders.base import Loader, SourceCheck, SourceUnavailable, _utc

log = logging.getLogger(__name__)

RECORD = struct.Struct(">IIIff")
_DTYPE = np.dtype([("ms", ">u4"), ("ask", ">u4"), ("bid", ">u4"), ("ask_vol", ">f4"), ("bid_vol", ">f4")])

HttpGet = Callable[[str], tuple[int, bytes, dict[str, str]]]


@dataclass
class HourResult:
    """Outcome of one hourly file request (kept so gaps are reported, never silently dropped)."""

    hour: str
    url: str
    status: str  # "ok" | "empty" | "failed"
    http_status: int | None
    n_rows: int = 0
    n_crossed_dropped: int = 0
    attempts: int = 0
    detail: str = ""


@dataclass
class FetchReport:
    """Per-hour log of the last ``fetch`` call."""

    symbol: str
    hours: list[HourResult] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Counts by status, rows and the list of empty / failed hours."""
        by = {s: [h.hour for h in self.hours if h.status == s] for s in ("ok", "empty", "failed")}
        return {
            "symbol": self.symbol,
            "hours_requested": len(self.hours),
            "hours_ok": len(by["ok"]),
            "hours_empty": by["empty"],
            "hours_failed": by["failed"],
            "rows": sum(h.n_rows for h in self.hours),
            "crossed_dropped": sum(h.n_crossed_dropped for h in self.hours),
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {"summary": self.summary(), "hours": [asdict(h) for h in self.hours]}


def hour_url(base_url: str, symbol: str, hour: datetime) -> str:
    """URL of the hourly tick file starting at ``hour`` (UTC). Month is 0-based in the path."""
    h = _utc(hour)
    return f"{base_url}/{symbol}/{h.year:04d}/{h.month - 1:02d}/{h.day:02d}/{h.hour:02d}h_ticks.bi5"


def decode_bi5(raw: bytes) -> np.ndarray:
    """Decompress (if needed) and parse a .bi5 payload into a structured array (ms, ask, bid, vols)."""
    if not raw:
        return np.empty(0, dtype=_DTYPE)
    payload = lzma.decompress(raw, format=lzma.FORMAT_AUTO)
    if len(payload) % RECORD.size:
        raise ValueError(f"payload length {len(payload)} is not a multiple of {RECORD.size}")
    return np.frombuffer(payload, dtype=_DTYPE)


def encode_bi5(records: list[tuple[int, int, int, float, float]]) -> bytes:
    """Inverse of ``decode_bi5`` (LZMA alone format). Used for test fixtures."""
    return lzma.compress(b"".join(RECORD.pack(*r) for r in records), format=lzma.FORMAT_ALONE)


def records_to_frame(
    rec: np.ndarray, hour: datetime, symbol: str, price_scale: float, source: str, venue: str
) -> pd.DataFrame:
    """Turn decoded records of one hour into a (not yet crossed-filtered) quotes frame."""
    base = pd.Timestamp(_utc(hour))
    ts = base + pd.to_timedelta(rec["ms"].astype("int64"), unit="ms")
    n = len(rec)
    frame = pd.DataFrame(
        {
            "ts": ts,
            "symbol": [symbol] * n,
            "source": [source] * n,
            "venue": [venue] * n,
            "bid_px": rec["bid"].astype("float64") / price_scale,
            "bid_sz": rec["bid_vol"].astype("float64"),
            "ask_px": rec["ask"].astype("float64") / price_scale,
            "ask_sz": rec["ask_vol"].astype("float64"),
            "recv_ts": pd.NaT,
        }
    )
    return schema.coerce(frame, "quotes")


def _requests_get(timeout_s: float, user_agent: str) -> HttpGet:
    session = requests.Session()
    session.headers["User-Agent"] = user_agent

    def get(url: str) -> tuple[int, bytes, dict[str, str]]:
        resp = session.get(url, timeout=timeout_s)
        return resp.status_code, resp.content, dict(resp.headers)

    return get


class DukascopyLoader(Loader):
    """Loader for Dukascopy hourly ``.bi5`` tick-quote files (kind ``quotes`` only)."""

    source = "dukascopy"
    venue = "dukascopy"
    tier = "B"
    kinds = frozenset({"quotes"})

    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        http_get: HttpGet | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg if cfg is not None else settings.load_config("sources")["dukascopy"]
        self._get = http_get or _requests_get(float(self.cfg["timeout_s"]), str(self.cfg["user_agent"]))
        self._sleep = sleep
        self._requested = False
        self.last_report: FetchReport | None = None

    # -- helpers ---------------------------------------------------------------------------------
    def price_scale(self, symbol: str) -> float:
        """Points per unit price for ``symbol`` (config ``price_scale``)."""
        scales = self.cfg["price_scale"]
        if symbol not in scales:
            raise KeyError(f"dukascopy.price_scale has no entry for {symbol!r}; add and verify it")
        return float(scales[symbol])

    def _request(self, url: str) -> tuple[int | None, bytes, int, str]:
        """GET with pause + exponential backoff on retryable statuses. Returns (status, body, attempts, detail)."""
        retry_on = set(self.cfg["retry_statuses"])
        backoff = list(self.cfg["backoff_s"])
        status: int | None = None
        detail = ""
        for attempt in range(1, len(backoff) + 2):
            if self._requested:
                self._sleep(float(self.cfg["request_pause_s"]))
            self._requested = True
            try:
                status, body, _headers = self._get(url)
            except requests.RequestException as exc:  # network-level: reset, timeout, DNS
                status, body, detail = None, b"", f"{type(exc).__name__}: {exc}"
            else:
                detail = ""
                if status not in retry_on:
                    return status, body, attempt, detail
                detail = f"HTTP {status}: {body[:120]!r}"
            if attempt > len(backoff):
                break
            wait = float(backoff[attempt - 1])
            log.warning("dukascopy %s -> %s; backing off %.0fs (attempt %d)", url, detail, wait, attempt)
            self._sleep(wait)
        return status, b"", len(backoff) + 1, detail

    def fetch_hour(self, symbol: str, hour: datetime) -> tuple[pd.DataFrame, HourResult]:
        """Download and decode one hourly file. Never raises for HTTP errors; see ``HourResult.status``."""
        hour = _utc(hour).replace(minute=0, second=0, microsecond=0)
        url = hour_url(self.cfg["base_url"], symbol, hour)
        status, body, attempts, detail = self._request(url)
        res = HourResult(hour=hour.isoformat(), url=url, status="failed", http_status=status, attempts=attempts)
        if status in set(self.cfg["empty_statuses"]) or (status == 200 and not body):
            res.status, res.detail = "empty", "no ticks in hour" if status == 200 else f"HTTP {status}"
            log.info("dukascopy %s %s: empty (%s)", symbol, hour.isoformat(), res.detail)
            return schema.empty_frame("quotes"), res
        if status != 200:
            res.detail = detail or f"HTTP {status}"
            log.error("dukascopy %s %s: failed (%s)", symbol, hour.isoformat(), res.detail)
            return schema.empty_frame("quotes"), res
        try:
            rec = decode_bi5(body)
        except (lzma.LZMAError, ValueError) as exc:
            res.detail = f"decode error: {exc}"
            log.error("dukascopy %s %s: %s", symbol, hour.isoformat(), res.detail)
            return schema.empty_frame("quotes"), res
        frame = records_to_frame(rec, hour, symbol, self.price_scale(symbol), self.source, self.venue)
        crossed = frame["bid_px"] > frame["ask_px"]
        res.n_crossed_dropped = int(crossed.sum())
        if res.n_crossed_dropped:
            log.warning("dukascopy %s %s: dropped %d crossed quotes", symbol, hour, res.n_crossed_dropped)
            frame = frame.loc[~crossed].reset_index(drop=True)
        res.status, res.n_rows = ("ok", len(frame)) if len(frame) else ("empty", 0)
        return frame, res

    # -- Loader API ------------------------------------------------------------------------------
    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Quotes in ``[start, end)``. Raises ``SourceUnavailable`` if any hour failed after retries.

        The per-hour outcome (ok / empty / failed) is kept in ``self.last_report``.
        """
        self._require(kind)
        lo, hi = _utc(start), _utc(end)
        self.last_report = FetchReport(symbol=symbol)
        frames = []
        hour = lo.replace(minute=0, second=0, microsecond=0)
        while hour < hi:
            frame, res = self.fetch_hour(symbol, hour)
            self.last_report.hours.append(res)
            frames.append(frame)
            hour += timedelta(hours=1)
        failed = self.last_report.summary()["hours_failed"]
        if failed:
            raise SourceUnavailable(f"dukascopy {symbol}: {len(failed)} hour(s) failed, first {failed[0]}")
        out = pd.concat([f for f in frames if len(f)] or [schema.empty_frame("quotes")], ignore_index=True)
        out = out[(out["ts"] >= pd.Timestamp(lo)) & (out["ts"] < pd.Timestamp(hi))]
        return schema.coerce(out.sort_values("ts", kind="stable"), "quotes")

    def check(self) -> SourceCheck:
        """Request one recent weekday hour; never raises."""
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hour -= timedelta(hours=int(self.cfg["check_lag_hours"]))
        while hour.weekday() >= 5:
            hour -= timedelta(days=1)
        symbol = str(self.cfg["check_symbol"])
        url = hour_url(self.cfg["base_url"], symbol, hour)
        try:
            frame, res = self.fetch_hour(symbol, hour)
        except Exception as exc:  # noqa: BLE001 - check() must never raise
            return SourceCheck(self.source, False, url, f"{type(exc).__name__}: {exc}")
        return SourceCheck(
            self.source, res.status != "failed", url,
            f"{res.status} http={res.http_status} rows={len(frame)} {res.detail}".strip(),
            extra=asdict(res),
        )


# -- CLI: pull whole UTC days into the store -------------------------------------------------------
def pull_days(
    loader: DukascopyLoader, symbol: str, days: list[date], root: Path | None = None
) -> list[dict[str, Any]]:
    """Fetch each UTC day hour by hour, store what arrived, and return one report per day.

    Each hour is stored as it arrives (re-runs de-duplicate). Failed hours are reported; after
    ``max_consecutive_failed_hours`` failures in a row the pull stops (``aborted: true``) rather than
    keep hitting a source that is refusing us.
    """
    reports = []
    limit = int(loader.cfg["max_consecutive_failed_hours"])
    streak = 0
    for day in days:
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        frames, report, written = [], FetchReport(symbol=symbol), set()
        aborted = False
        for h in range(24):
            frame, res = loader.fetch_hour(symbol, start + timedelta(hours=h))
            report.hours.append(res)
            frames.append(frame)
            written.update(storage.write_partitioned(frame, "quotes", root=root))  # hour by hour: restart-safe
            streak = streak + 1 if res.status == "failed" else 0
            if streak >= limit:
                log.error("dukascopy: %d consecutive failed hours; stopping the pull", streak)
                aborted = True
                break
        data = pd.concat([f for f in frames if len(f)] or [schema.empty_frame("quotes")], ignore_index=True)
        entry = {"date": day.isoformat(), **report.summary(), "aborted": aborted,
                 "files": sorted(str(p) for p in written)}
        if len(data):
            gaps = data["ts"].diff().dt.total_seconds()
            entry["max_gap_s"] = float(gaps.max()) if len(data) > 1 else None
            entry["min_nonzero_dt_ms"] = float(gaps[gaps > 0].min() * 1000) if (gaps > 0).any() else None
        reports.append(entry)
        log.info("dukascopy %s %s: %s", symbol, day, json.dumps(report.summary()))
        if aborted:
            break
    return reports


def main(argv: list[str] | None = None) -> int:
    """``python -m flow_model.data.loaders.dukascopy --symbol EURUSD --start 2026-11-17 --end 2026-11-21``."""
    p = argparse.ArgumentParser(description=main.__doc__)
    p.add_argument("--symbol", default="EURUSD")
    p.add_argument("--start", required=True, help="first UTC date, YYYY-MM-DD")
    p.add_argument("--end", required=True, help="last UTC date (inclusive), YYYY-MM-DD")
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default logs/...)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    first, last = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    reports = pull_days(DukascopyLoader(), args.symbol, days, root=settings.storage_root())
    out = args.report or settings.REPO_ROOT / "logs" / f"dukascopy_{args.symbol}_{first}_{last}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reports, indent=2))
    print(json.dumps([{k: v for k, v in r.items() if k != "files"} for r in reports], indent=2))
    return 0 if all(not r["hours_failed"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
