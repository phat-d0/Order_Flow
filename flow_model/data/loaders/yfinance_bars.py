"""Yahoo Finance 1-minute bars via ``yfinance`` (Tier C: CL=F, GC=F, 6E=F).

Owned by the Commodities agent; the FX agent imports it for 6E=F, so nothing here is commodity-specific.

Verified from the cloud environment on 2026-11-26 (see ``docs/notes/commodities.md``):

* Endpoint ``https://query{1,2}.finance.yahoo.com/v8/finance/chart/<symbol>?interval=1m`` returns 200.
  The crumb endpoint answers 429; yfinance carries on without a crumb and the chart call still works.
* 1m data: at most 8 days per request ("Only 8 days worth of 1m granularity data are allowed to be
  fetched per request") and only "within the last 30 days" (HTTP 422 otherwise).
* Timestamps are epoch seconds labelled at bar open; yfinance localises them to the exchange tz
  (America/New_York for CME futures). We convert to UTC.
* Volume artifact: the first bar of every response, and the first bar of each trading day except the
  most recent one, come back with volume 0 (re-requesting later does not fix it). We request
  ``lead_in_minutes`` early and discard the lead-in; session-open zeros are kept and counted
  (``n_zero_volume_after_gap``). The daily appender captures the latest open while it is still correct.
* Minutes with no trades come back as all-NaN rows (with ``keepna=True``); they are dropped here and
  counted, and they show up as missing minutes in :func:`session_gaps`.

Every parameter comes from ``sources.yaml`` -> ``yfinance``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from flow_model.data import schema
from flow_model.data.loaders.base import Loader, NotSupported, SourceCheck, SourceUnavailable
from flow_model.settings import load_config

log = logging.getLogger(__name__)

#: ``client(symbol, start, end, interval, timeout_s, prepost) -> raw frame`` with a tz-aware DatetimeIndex
#: and columns Open, High, Low, Close, Volume (the shape ``yfinance.Ticker.history`` returns).
Client = Callable[[str, datetime, datetime, str, float, bool], pd.DataFrame]

_RAW_COLS = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
_SAMPLE = 5  # max example timestamps kept per issue in reports (report size only, not a model parameter)


class RateLimited(Exception):
    """Yahoo answered with a rate-limit (HTTP 429) — retried with backoff."""


class NoData(Exception):
    """Yahoo returned no prices for a window (weekend, holiday, delisted) — not an error."""


class Rejected(Exception):
    """Yahoo refused the request permanently (e.g. HTTP 422: window too long or too old)."""


def yfinance_client(
    symbol: str, start: datetime, end: datetime, interval: str, timeout_s: float, prepost: bool
) -> pd.DataFrame:
    """Default client: one ``yfinance.Ticker.history`` call, errors mapped to this module's exceptions."""
    import yfinance as yf
    from yfinance import exceptions as yfe

    previous = yf.config.debug.hide_exceptions
    yf.config.debug.hide_exceptions = False
    try:
        return yf.Ticker(symbol).history(
            start=start, end=end, interval=interval, prepost=prepost, actions=False,
            auto_adjust=False, back_adjust=False, repair=False, keepna=True, timeout=timeout_s,
        )
    except yfe.YFRateLimitError as exc:
        raise RateLimited(str(exc)) from exc
    except yfe.YFPricesMissingError as exc:
        raise NoData(str(exc)) from exc
    except (yfe.YFInvalidPeriodError, yfe.YFTickerMissingError, yfe.YFTzMissingError) as exc:
        raise Rejected(str(exc)) from exc
    except Exception as exc:  # yfinance re-raises Yahoo's 422 as a generic exception
        if "not available" in str(exc) or "Unprocessable" in str(exc):
            raise Rejected(str(exc)) from exc
        raise
    finally:
        yf.config.debug.hide_exceptions = previous


@dataclass
class FetchReport:
    """What one ``fetch_with_report`` call did: requests, retries, and every row it did not keep."""

    symbol: str
    start: str
    end: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    n_raw: int = 0
    n_kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    dropped_examples: dict[str, list[str]] = field(default_factory=dict)
    n_zero_volume: int = 0
    n_zero_volume_after_gap: int = 0  # session-open bars: Yahoo reports 0 except on the latest trading day
    zero_volume_examples: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return asdict(self)


def _utc(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def chunk_windows(start: datetime, end: datetime, max_days: float) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Split ``[start, end)`` into consecutive windows no longer than ``max_days``."""
    lo, hi = _utc(start), _utc(end)
    step = pd.Timedelta(days=max_days)
    out = []
    while lo < hi:
        out.append((lo, min(lo + step, hi)))
        lo = lo + step
    return out


def normalize_bars(
    raw: pd.DataFrame, symbol: str, source: str, venue: str, interval: str, report: FetchReport | None = None
) -> pd.DataFrame:
    """Turn a yfinance-shaped frame into schema ``bars`` (UTC). Every dropped row is counted in ``report``.

    Drop reasons: ``nan_price`` (Yahoo placeholder minute with no trades), ``nan_volume``,
    ``nonpositive_price``, ``inconsistent_ohlc`` (high/low not bounding open/close), ``duplicate_ts``
    (keeps the last). Zero-volume bars are kept and counted.
    """
    report = report or FetchReport(symbol=symbol, start="", end="")
    if raw is None or raw.empty:
        return schema.empty_frame("bars")
    missing = [c for c in _RAW_COLS if c not in raw.columns]
    if missing:
        raise SourceUnavailable(f"yfinance frame for {symbol} lacks columns {missing}")
    frame = raw[list(_RAW_COLS)].rename(columns=_RAW_COLS).copy()
    idx = pd.DatetimeIndex(raw.index)
    frame["ts"] = (idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")).as_unit("ns")
    frame = frame.reset_index(drop=True)
    report.n_raw += len(frame)

    def drop(mask: pd.Series, reason: str) -> None:
        nonlocal frame
        if mask.any():
            report.dropped[reason] = report.dropped.get(reason, 0) + int(mask.sum())
            examples = report.dropped_examples.setdefault(reason, [])
            examples.extend(str(t) for t in frame.loc[mask, "ts"].head(_SAMPLE - len(examples)))
        frame = frame.loc[~mask].reset_index(drop=True)

    prices = ["open", "high", "low", "close"]
    drop(frame[prices].isna().any(axis=1), "nan_price")
    drop(frame["volume"].isna(), "nan_volume")
    drop((frame[prices] <= 0).any(axis=1), "nonpositive_price")
    bad = (frame["high"] < frame[["open", "close", "low"]].max(axis=1)) | (
        frame["low"] > frame[["open", "close"]].min(axis=1)
    )
    drop(bad, "inconsistent_ohlc")
    frame = frame.sort_values("ts", kind="stable").reset_index(drop=True)
    drop(frame["ts"].duplicated(keep="last"), "duplicate_ts")

    zero = frame["volume"] == 0
    report.n_zero_volume += int(zero.sum())
    report.zero_volume_examples.extend(
        str(t) for t in frame.loc[zero, "ts"].head(max(0, _SAMPLE - len(report.zero_volume_examples)))
    )
    frame["symbol"], frame["source"], frame["venue"], frame["interval"] = symbol, source, venue, interval
    out = schema.coerce(frame, "bars")
    report.n_kept += len(out)
    return out


def session_minutes(start: datetime, end: datetime, session: dict[str, Any], interval_minutes: int) -> pd.DatetimeIndex:
    """Expected bar-open times (UTC) in ``[start, end)`` for a session spec from ``sources.yaml``.

    Spec keys: ``tz``; ``break_start``/``break_end`` ("HH:MM", daily halt, local time);
    ``week_open_day``/``week_open`` and ``week_close_day``/``week_close`` (Monday=0). Exchange
    holidays are not modelled: they appear as gaps, which is the honest outcome for a data log.
    """
    lo, hi = _utc(start).ceil(f"{interval_minutes}min"), _utc(end)
    if lo >= hi:
        return pd.DatetimeIndex([], tz="UTC")
    grid = pd.date_range(lo, hi, freq=f"{interval_minutes}min", inclusive="left")
    local = grid.tz_convert(session["tz"])
    hhmm = local.hour * 60 + local.minute

    def minutes(text: str) -> int:
        h, m = text.split(":")
        return int(h) * 60 + int(m)

    keep = np.ones(len(grid), dtype=bool)
    if session.get("break_start") and session.get("break_end"):
        keep &= ~((hhmm >= minutes(session["break_start"])) & (hhmm < minutes(session["break_end"])))
    week_pos = local.day_of_week * 1440 + hhmm
    open_pos = session["week_open_day"] * 1440 + minutes(session["week_open"])
    close_pos = session["week_close_day"] * 1440 + minutes(session["week_close"])
    if open_pos > close_pos:  # week wraps (e.g. Sunday evening -> Friday afternoon)
        keep &= (week_pos >= open_pos) | (week_pos < close_pos)
    else:
        keep &= (week_pos >= open_pos) & (week_pos < close_pos)
    return grid[keep]


def session_gaps(
    bars: pd.DataFrame,
    start: datetime,
    end: datetime,
    session: dict[str, Any],
    interval_minutes: int,
    min_report_minutes: int,
    reason: str = "no bar from source in session minute(s)",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Missing in-session minutes in ``[start, end)``.

    Returns (runs of at least ``min_report_minutes`` missing minutes as contract-style gap dicts,
    summary counts ``expected, present, missing, missing_runs, short_runs_minutes``). Short runs are
    counted in the summary, not listed, so the log stays readable for thin markets.
    """
    expected = session_minutes(start, end, session, interval_minutes)
    present = pd.DatetimeIndex(bars["ts"]) if len(bars) else pd.DatetimeIndex([], tz="UTC")
    missing = expected.difference(present)
    step = pd.Timedelta(minutes=interval_minutes)
    runs: list[tuple[pd.Timestamp, pd.Timestamp, int]] = []
    for ts in missing:
        if runs and ts - runs[-1][1] == step:
            runs[-1] = (runs[-1][0], ts, runs[-1][2] + 1)
        else:
            runs.append((ts, ts, 1))
    long_runs = [r for r in runs if r[2] >= min_report_minutes]
    gaps = [
        {"start": a.isoformat(), "end": (b + step).isoformat(), "kind": "bars", "minutes": n, "reason": reason}
        for a, b, n in long_runs
    ]
    summary = {
        "expected": int(len(expected)),
        "present": int(len(expected.intersection(present))),
        "missing": int(len(missing)),
        "missing_runs": len(runs),
        "short_runs_minutes": int(sum(r[2] for r in runs if r[2] < min_report_minutes)),
        "outside_session_bars": int(len(present.difference(expected))),
    }
    return gaps, summary


class YFinanceBarsLoader(Loader):
    """1-minute OHLCV bars from Yahoo Finance (continuous futures ``=F`` are not back-adjusted)."""

    source = "yfinance"
    venue = "yahoo"
    tier = "C"
    kinds = frozenset({"bars"})

    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        client: Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg if cfg is not None else load_config("sources")["yfinance"]
        self.client = client or yfinance_client
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ public API
    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Schema ``bars`` for ``[start, end)``; see :meth:`fetch_with_report` for what was dropped."""
        frame, _ = self.fetch_with_report(kind, symbol, start, end)
        return frame

    def fetch_with_report(
        self, kind: str, symbol: str, start: datetime, end: datetime
    ) -> tuple[pd.DataFrame, FetchReport]:
        """Chunked download of ``[start, end)`` plus a :class:`FetchReport` of every request and drop.

        The part of the window older than ``lookback_days`` is not requested (Yahoo refuses it) and is
        recorded as a gap. Raises ``SourceUnavailable`` if nothing in the window is fetchable or if a
        request still fails after ``max_retries`` retries.
        """
        self._require(kind)
        lo, hi = _utc(start), min(_utc(end), _utc(self.now()))
        report = FetchReport(symbol=symbol, start=lo.isoformat(), end=hi.isoformat())
        oldest = _utc(self.now()) - pd.Timedelta(days=self.cfg["lookback_days"])
        if lo < oldest - pd.Timedelta(minutes=self.cfg["interval_minutes"]):
            report.gaps.append({"start": lo.isoformat(), "end": min(oldest, hi).isoformat(), "kind": "bars",
                                "reason": f"older than Yahoo 1m lookback ({self.cfg['lookback_days']} days)"})
            report.warnings.append(f"window start {lo} clipped to lookback limit {oldest}")
            log.warning("%s: %s", symbol, report.warnings[-1])
        lo = max(lo, oldest)
        if lo >= hi:
            raise SourceUnavailable(f"{symbol}: [{start}, {end}) is outside Yahoo's 1m window")
        parts = []
        for i, (a, b) in enumerate(chunk_windows(lo, hi, self.cfg["max_days_per_request"])):
            if i:
                self.sleep(self.cfg["request_pause_s"])
            lead = pd.Timedelta(minutes=self.cfg["lead_in_minutes"])
            raw = self._request(symbol, a - lead, b, report)  # Yahoo zeroes the first bar's volume
            part = normalize_bars(raw, symbol, self.source, self.venue, self.cfg["interval"], report)
            parts.append(part.loc[part["ts"] >= a])
        frame = pd.concat(parts, ignore_index=True) if parts else schema.empty_frame("bars")
        frame = frame.loc[(frame["ts"] >= lo) & (frame["ts"] < hi)]
        dup = frame["ts"].duplicated(keep="last")
        if dup.any():  # chunk edges overlap by at most one bar
            report.dropped["duplicate_ts"] = report.dropped.get("duplicate_ts", 0) + int(dup.sum())
            frame = frame.loc[~dup]
        frame = frame.sort_values("ts", kind="stable").reset_index(drop=True)
        report.n_kept = len(frame)
        report.n_zero_volume = int((frame["volume"] == 0).sum())
        report.zero_volume_examples = [str(t) for t in frame.loc[frame["volume"] == 0, "ts"].head(_SAMPLE)]
        after_gap = (frame["volume"] == 0) & (
            frame["ts"].diff() > pd.Timedelta(minutes=self.cfg["interval_minutes"]))
        report.n_zero_volume_after_gap = int(after_gap.sum())
        if report.dropped:
            log.info("%s: dropped rows %s (raw %d, kept %d)", symbol, report.dropped, report.n_raw, len(frame))
        if report.n_zero_volume:
            log.info("%s: %d zero-volume bars kept", symbol, report.n_zero_volume)
        return frame, report

    def check(self) -> SourceCheck:
        """One small request (last day of ``check_symbol``); never raises."""
        symbol = self.cfg["check_symbol"]
        endpoint = self.cfg["chart_url"].format(symbol=symbol)
        now = _utc(self.now())
        try:
            frame, report = self.fetch_with_report("bars", symbol, now - pd.Timedelta(days=1), now)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
            return SourceCheck(self.source, False, endpoint, f"{type(exc).__name__}: {exc}")
        detail = f"{len(frame)} bars, last {frame['ts'].max() if len(frame) else None}"
        return SourceCheck(self.source, True, endpoint, detail, extra={"report": report.to_dict()})

    # ------------------------------------------------------------------ internals
    def _request(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp, report: FetchReport) -> pd.DataFrame:
        """One window with exponential backoff on rate limits / transient errors."""
        retries, base, cap = self.cfg["max_retries"], self.cfg["backoff_base_s"], self.cfg["backoff_max_s"]
        for attempt in range(retries + 1):
            entry = {"start": start.isoformat(), "end": end.isoformat(), "attempt": attempt,
                     "at": datetime.now(timezone.utc).isoformat()}
            try:
                raw = self.client(symbol, start.to_pydatetime(), end.to_pydatetime(), self.cfg["interval"],
                                  self.cfg["timeout_s"], self.cfg["prepost"])
                entry.update(outcome="ok", rows=0 if raw is None else len(raw))
                report.requests.append(entry)
                return raw
            except NoData as exc:
                entry.update(outcome="no_data", detail=str(exc)[:200])
                report.requests.append(entry)
                return pd.DataFrame()
            except Rejected as exc:
                entry.update(outcome="rejected", detail=str(exc)[:300])
                report.requests.append(entry)
                raise SourceUnavailable(f"{symbol}: Yahoo rejected {start}..{end}: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 - rate limit or transient network error: back off
                kind = "rate_limited" if isinstance(exc, RateLimited) else "error"
                entry.update(outcome=kind, detail=f"{type(exc).__name__}: {str(exc)[:200]}")
                report.requests.append(entry)
                if attempt == retries:
                    raise SourceUnavailable(f"{symbol}: {kind} after {retries} retries: {exc}") from exc
                wait = min(cap, base * 2**attempt)
                log.warning("%s: %s on %s..%s, retry %d/%d in %.0fs", symbol, kind, start, end,
                            attempt + 1, retries, wait)
                self.sleep(wait)
        raise AssertionError("unreachable")


__all__ = [
    "FetchReport", "NoData", "NotSupported", "RateLimited", "Rejected", "YFinanceBarsLoader",
    "chunk_windows", "normalize_bars", "session_gaps", "session_minutes", "yfinance_client",
]
