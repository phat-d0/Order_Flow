"""Intraday volume/volatility/spread profiles and deseasonalisation.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def time_of_day_bucket(ts: pd.Series, bucket: str, tz: str | None = None) -> pd.Series:
    """Time-of-day bucket label "HH:MM" (bucket start) of each timestamp, in ``tz`` (default UTC)."""
    secs = _seconds_of_day(ts, tz)
    step = int(pd.Timedelta(bucket).total_seconds())
    codes = (secs // step) * step
    uniq, inv = np.unique(codes, return_inverse=True)
    labels = np.array([f"{s // 3600:02d}:{(s % 3600) // 60:02d}" for s in uniq], dtype=object)
    return pd.Series(labels[inv], index=ts.index, name="bucket")


def _seconds_of_day(ts: pd.Series, tz: str | None) -> np.ndarray:
    """Integer seconds since local midnight in ``tz`` (UTC if None)."""
    t = pd.DatetimeIndex(pd.to_datetime(ts, utc=True))
    if tz:
        t = t.tz_convert(tz)
    local = t.tz_localize(None).as_unit("ns").asi8
    return (local % (86400 * 10**9)) // 10**9


def _in_session(ts: pd.Series, tz: str | None, session: tuple[str, str]) -> np.ndarray:
    tod = _seconds_of_day(ts, tz)
    lo = pd.Timedelta(session[0] + ":00").total_seconds()
    hi = pd.Timedelta(session[1] + ":00").total_seconds()
    return (tod >= lo) & (tod < hi)


def intraday_profile(
    df: pd.DataFrame,
    value_col: str,
    bucket: str,
    ts_col: str = "ts",
    tz: str | None = None,
    session: tuple[str, str] | None = None,
) -> pd.DataFrame:
    """Average of ``value_col`` per time-of-day bucket; columns ``bucket, mean, median, n``.

    ``tz`` sets the clock (e.g. "America/New_York" for equities RTH, None = UTC for crypto);
    ``session`` ("09:30", "16:00") keeps only rows inside [start, end) in that clock.
    Buckets are labelled by their start, "HH:MM".
    """
    d = df[[ts_col, value_col]].dropna()
    if session is not None:
        d = d[_in_session(d[ts_col], tz, tuple(session))]
    if d.empty:
        return pd.DataFrame(columns=["bucket", "mean", "median", "n"])
    b = time_of_day_bucket(d[ts_col], bucket, tz)
    g = d[value_col].groupby(b.to_numpy())
    out = pd.DataFrame({"mean": g.mean(), "median": g.median(), "n": g.size()})
    out.index.name = "bucket"
    return out.reset_index().sort_values("bucket", ignore_index=True)


def deseasonalize(
    df: pd.DataFrame, profile: pd.DataFrame, value_col: str, bucket: str, ts_col: str = "ts", tz: str | None = None
) -> pd.Series:
    """``value_col`` divided by its bucket's profile mean (NaN where the bucket is missing or 0)."""
    b = time_of_day_bucket(df[ts_col], bucket, tz)
    means = profile.set_index("bucket")["mean"].replace(0.0, np.nan)
    return (df[value_col] / b.map(means).to_numpy()).rename(f"{value_col}_deseason")
