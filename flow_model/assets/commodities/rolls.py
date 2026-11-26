"""Contract-roll detection for Yahoo continuous futures (``CL=F``, ``GC=F``, ...).

Yahoo's ``=F`` series switch from the expiring contract to the next one without back-adjustment, so
the roll shows up as a price jump that is not a market move. Two independent signals:

1. **Price jumps** — 1m log return far outside the recent robust volatility (:func:`detect_jumps`).
2. **Calendar** — approximate exchange roll dates from the instrument's ``roll_rule``
   (:func:`calendar_roll_dates`). Business days ignore exchange holidays, so dates can be one day off.

:func:`flag_rolls` combines them into fits.json-style gap/caveat entries. Parameters live in
``sources.yaml`` -> ``yfinance.roll_detection`` and ``instruments.yaml`` -> ``<symbol>.roll_rule``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from flow_model.settings import instrument, load_config

#: Consistency factor turning a median absolute deviation into a normal-equivalent sigma (a constant
#: of the normal distribution, not a tunable parameter).
MAD_TO_SIGMA = 1.4826


def _bday_offset(day: pd.Timestamp, n: int) -> pd.Timestamp:
    return (day + pd.offsets.BDay(n)).normalize()


def _nymex_cl(year: int, month: int) -> pd.Timestamp:
    """Last trade of the CL contract whose delivery month follows ``month``: 3 business days before the
    25th of ``month`` (counted from the business day before the 25th when the 25th is not one)."""
    d25 = pd.Timestamp(year=year, month=month, day=25)
    anchor = d25 if d25.day_of_week < 5 else _bday_offset(d25, -1)
    return _bday_offset(anchor, -3)


def _last_bday_of_month(year: int, month: int) -> pd.Timestamp:
    return (pd.Timestamp(year=year, month=month, day=1) + pd.offsets.BMonthEnd(0)).normalize()


def _imm_last_trade(year: int, month: int) -> pd.Timestamp:
    """Two business days before the third Wednesday (CME currency futures)."""
    first = pd.Timestamp(year=year, month=month, day=1)
    third_wed = first + pd.Timedelta(days=(2 - first.day_of_week) % 7 + 14)
    return _bday_offset(third_wed, -2)


def calendar_roll_dates(rule: dict[str, Any], start: date | str, end: date | str) -> list[pd.Timestamp]:
    """Approximate roll dates (naive dates) in ``[start, end]`` for a ``roll_rule`` from instruments.yaml.

    Rules: ``nymex_cl`` (monthly), ``last_bday_before_delivery`` (needs ``delivery_months``; last business
    day of the month before each delivery month ≈ first notice day), ``imm_quarterly`` (needs
    ``months``; 2 business days before the third Wednesday).
    """
    lo, hi = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if lo.tzinfo is not None:
        lo, hi = lo.tz_localize(None), hi.tz_localize(None)
    out: list[pd.Timestamp] = []
    for period in pd.period_range(lo - pd.DateOffset(months=1), hi + pd.DateOffset(months=1), freq="M"):
        y, m = period.year, period.month
        name = rule["rule"]
        if name == "nymex_cl":
            out.append(_nymex_cl(y, m))
        elif name == "last_bday_before_delivery":
            nxt = period + 1
            if nxt.month in rule["delivery_months"]:
                out.append(_last_bday_of_month(y, m))
        elif name == "imm_quarterly":
            if m in rule["months"]:
                out.append(_imm_last_trade(y, m))
        else:
            raise ValueError(f"unknown roll rule {name!r}")
    return sorted(d for d in set(out) if lo <= d <= hi)


def detect_jumps(bars: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    """Bars whose close-to-close log return exceeds ``z_threshold`` robust sigmas.

    Sigma is a rolling MAD of the previous ``vol_window_bars`` returns (falls back to the rolling std
    when the MAD is zero, as in very quiet minutes). Returns ``ts, prev_ts, ret, sigma, z, gap_minutes``.
    """
    cols = ["ts", "prev_ts", "ret", "sigma", "z", "gap_minutes"]
    if len(bars) < 2:
        return pd.DataFrame(columns=cols)
    frame = bars.sort_values("ts")[["ts", "close"]].reset_index(drop=True)
    ret = np.log(frame["close"]).diff()
    window, min_p = cfg["vol_window_bars"], cfg["min_periods_bars"]
    prior = ret.shift(1)
    med = prior.rolling(window, min_periods=min_p).median()
    mad = (prior - med).abs().rolling(window, min_periods=min_p).median() * MAD_TO_SIGMA
    std = prior.rolling(window, min_periods=min_p).std()
    sigma = mad.where(mad > 0, std).replace(0, np.nan)
    z = (ret - med.fillna(0)).abs() / sigma
    out = pd.DataFrame({
        "ts": frame["ts"], "prev_ts": frame["ts"].shift(1), "ret": ret, "sigma": sigma, "z": z,
        "gap_minutes": (frame["ts"] - frame["ts"].shift(1)).dt.total_seconds() / 60.0,
    })
    return out.loc[out["z"] > cfg["z_threshold"], cols].reset_index(drop=True)


def yahoo_roll_dates(rule: dict[str, Any], start: date | str, end: date | str) -> list[pd.Timestamp]:
    """Calendar roll dates shifted by the rule's ``yahoo_offset_bdays`` (when Yahoo's 1m ``=F`` series
    actually switches contract relative to the exchange date; observed, see notes)."""
    offset = int(rule.get("yahoo_offset_bdays", 0))
    pad = pd.Timedelta(days=abs(offset) * 2 + 7)
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    dates = [_bday_offset(d, offset) if offset else d for d in calendar_roll_dates(rule, lo - pad, hi + pad)]
    return [d for d in dates if lo.normalize() <= d <= hi.normalize()]


def match_contract(continuous: pd.DataFrame, contract: pd.DataFrame, tol: float) -> pd.DataFrame:
    """Per-bar check of which bars of a continuous series equal a specific contract's close.

    Returns ``ts, continuous, contract, match``; the first ``match`` after a run of non-matches is the
    roll minute. Used to verify roll dates against Yahoo's single-contract tickers (e.g. ``CLF27.NYM``).
    """
    joined = pd.merge(continuous[["ts", "close"]], contract[["ts", "close"]], on="ts", suffixes=("_cont", "_k"))
    joined.columns = ["ts", "continuous", "contract"]
    joined["match"] = (joined["continuous"] - joined["contract"]).abs() <= tol
    return joined


def flag_rolls(
    bars: pd.DataFrame, symbol: str, cfg: dict[str, Any] | None = None, rule: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Suspected contract-roll bars for one symbol, ready for fits.json ``data.gaps`` / ``caveats``.

    For each expected Yahoo roll date (:func:`yahoo_roll_dates`) the largest jump (:func:`detect_jumps`)
    within ``calendar_tolerance_bdays`` business days is the suspected roll. Other jumps stay in
    ``jumps`` (news moves, not data problems). Returns ``{"calendar_rolls", "rolls", "jumps", "gaps",
    "caveats"}``; ``rolls`` is a list of dicts with ``calendar_date, ts, prev_ts, ret, z`` (ts None if
    no jump was found near the date, which is itself reported as a caveat).
    """
    cfg = cfg if cfg is not None else load_config("sources")["yfinance"]["roll_detection"]
    rule = rule if rule is not None else instrument(symbol).get("roll_rule")
    jumps = detect_jumps(bars, cfg)
    result: dict[str, Any] = {"calendar_rolls": [], "rolls": [], "jumps": jumps, "gaps": [], "caveats": []}
    if rule is None:
        result["caveats"].append(f"{symbol}: no roll_rule configured; contract rolls not checked")
        return result
    if bars.empty:
        return result
    first, last = bars["ts"].min(), bars["ts"].max()
    rolls = yahoo_roll_dates(rule, first.tz_localize(None).date(), last.tz_localize(None).date())
    result["calendar_rolls"] = [d.date().isoformat() for d in rolls]
    tol = int(cfg["calendar_tolerance_bdays"])
    jump_day = jumps["ts"].dt.tz_localize(None).dt.normalize() if len(jumps) else pd.Series(dtype="datetime64[ns]")
    for d in rolls:
        lo, hi = _bday_offset(d, -tol), _bday_offset(d, tol)
        near = jumps.loc[(jump_day >= lo) & (jump_day <= hi)] if len(jumps) else jumps
        if near.empty:
            result["rolls"].append({"calendar_date": d.date().isoformat(), "ts": None, "prev_ts": None,
                                    "ret": None, "z": None})
            result["caveats"].append(f"{symbol}: expected Yahoo roll ~{d.date()} but no jump above "
                                     f"{cfg['z_threshold']} sigma nearby; roll may be small or undetected")
            continue
        row = near.loc[near["z"].idxmax()]
        result["rolls"].append({"calendar_date": d.date().isoformat(), "ts": row["ts"].isoformat(),
                                "prev_ts": row["prev_ts"].isoformat(), "ret": float(row["ret"]),
                                "z": float(row["z"])})
        result["gaps"].append({"start": row["prev_ts"].isoformat(), "end": row["ts"].isoformat(), "kind": "bars",
                               "reason": f"suspected contract roll (expected ~{d.date()}): log return "
                                         f"{row['ret']:+.4%} = {row['z']:.1f} sigma; series not back-adjusted"})
        result["caveats"].append(f"{symbol}: contract roll at {row['ts']} (return {row['ret']:+.4%}); "
                                 "exclude this bar from return-based fits")
    return result
