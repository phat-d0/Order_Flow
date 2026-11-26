"""Shared fit container, OLS helper and date-based train/test split.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


def json_safe(obj: Any) -> Any:
    """Recursively convert ``obj`` to JSON-safe Python (NaN/inf -> None, numpy -> builtins)."""
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [json_safe(v) for v in obj.tolist()]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        val = float(obj)
        return val if math.isfinite(val) else None
    if isinstance(obj, pd.Timestamp):
        return None if pd.isna(obj) else obj.isoformat()
    if isinstance(obj, pd.Timedelta):
        return None if pd.isna(obj) else obj.total_seconds()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if obj is None or obj is pd.NA or obj is pd.NaT:
        return None
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return json_safe(obj.to_dict())
    return obj if isinstance(obj, str) else str(obj)


@dataclass
class FitResult:
    """Result of any fit: parameters, standard errors, R², sample size and free-form extras."""

    name: str
    params: dict[str, float]
    se: dict[str, float] = field(default_factory=dict)
    r2: float | None = None
    n: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (NaN/inf -> None, numpy scalars -> Python)."""
        return json_safe(
            {
                "name": self.name,
                "params": self.params,
                "se": self.se,
                "r2": self.r2,
                "n": self.n,
                "extra": self.extra,
            }
        )

    @classmethod
    def failed(cls, name: str, reason: str, n: int = 0) -> "FitResult":
        """A FitResult carrying NaN params and the failure ``reason`` in ``extra``."""
        return cls(name=name, params={}, n=n, extra={"error": reason})


def _design(
    y: pd.Series | np.ndarray, X: pd.DataFrame | np.ndarray | pd.Series, add_constant: bool
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Aligned float arrays with NaN rows dropped and column names."""
    if isinstance(X, pd.Series):
        X = X.to_frame(X.name if X.name is not None else "x")
    if isinstance(X, pd.DataFrame):
        names = [str(c) for c in X.columns]
        Xa = X.to_numpy(dtype=float)
    else:
        Xa = np.asarray(X, dtype=float)
        if Xa.ndim == 1:
            Xa = Xa[:, None]
        names = [f"x{i}" for i in range(Xa.shape[1])]
    ya = np.asarray(y, dtype=float).ravel()
    if add_constant:
        Xa = np.column_stack([np.ones(len(ya)), Xa])
        names = ["const", *names]
    ok = np.isfinite(ya) & np.isfinite(Xa).all(axis=1)
    return ya[ok], Xa[ok], names


def ts_ns(ts: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    """Timestamps as int64 nanoseconds since the epoch (UTC), without building Python objects."""
    idx = pd.DatetimeIndex(ts)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC")
    return idx.as_unit("ns").asi8


def floor_ns(ts: pd.Series | pd.DatetimeIndex, freq: str | pd.Timedelta) -> np.ndarray:
    """``ts`` floored to multiples of ``freq`` (epoch-aligned, like ``Series.dt.floor``), int64 ns."""
    step = pd.Timedelta(freq).value
    t = ts_ns(ts)
    return (t // step) * step


def ns_to_utc(ns: np.ndarray) -> pd.DatetimeIndex:
    """int64 ns -> tz-aware UTC DatetimeIndex."""
    return pd.DatetimeIndex(np.asarray(ns, dtype="datetime64[ns]")).tz_localize("UTC")


def newey_west_cov(X: np.ndarray, resid: np.ndarray, lags: int) -> np.ndarray:
    """Newey-West (Bartlett kernel) HAC covariance of OLS coefficients."""
    n = len(resid)
    xtx_inv = np.linalg.pinv(X.T @ X)
    u = X * resid[:, None]
    S = u.T @ u
    for lag in range(1, min(lags, n - 1) + 1):
        w = 1.0 - lag / (lags + 1.0)
        gamma = u[lag:].T @ u[:-lag]
        S += w * (gamma + gamma.T)
    return xtx_inv @ S @ xtx_inv


def ols_fit(
    y: pd.Series | np.ndarray,
    X: pd.DataFrame | np.ndarray,
    add_constant: bool = True,
    hac_lags: int | None = None,
    name: str = "ols",
) -> FitResult:
    """OLS of ``y`` on ``X`` (optionally Newey-West HAC standard errors).

    Rows with any NaN are dropped. ``r2`` is the centred R² when a constant is included,
    otherwise the uncentred R². ``extra`` holds ``resid_std``, ``se_type`` and ``k``.
    """
    ya, Xa, names = _design(y, X, add_constant)
    n, k = Xa.shape
    if n <= k:
        return FitResult.failed(name, f"need more than {k} rows, got {n}", n=n)
    beta, *_ = np.linalg.lstsq(Xa, ya, rcond=None)
    resid = ya - Xa @ beta
    ssr = float(resid @ resid)
    sst = float(((ya - ya.mean()) ** 2).sum()) if add_constant else float(ya @ ya)
    if hac_lags:
        cov = newey_west_cov(Xa, resid, int(hac_lags))
        se_type = f"HAC(newey-west, lags={int(hac_lags)})"
    else:
        cov = np.linalg.pinv(Xa.T @ Xa) * ssr / (n - k)
        se_type = "classical"
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    return FitResult(
        name=name,
        params=dict(zip(names, beta.tolist())),
        se=dict(zip(names, se.tolist())),
        r2=1.0 - ssr / sst if sst > 0 else None,
        n=n,
        extra={"resid_std": float(np.sqrt(ssr / (n - k))), "se_type": se_type, "k": k},
    )


def train_test_split_by_date(
    df: pd.DataFrame, train_frac: float, ts_col: str = "ts", min_test_days: int = 0
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, list[str]]]:
    """Split on whole UTC dates: earliest ``train_frac`` of dates -> train, rest -> test.

    At least one date always goes to train; when there are two or more dates at least
    ``max(1, min_test_days)`` go to test (if that leaves one for train). The third item is
    ``{"train": [start, end], "test": [start, end]}`` as ISO dates (empty list if a side is empty).
    """
    day_ns = pd.Timedelta(days=1).value
    days = ts_ns(pd.to_datetime(df[ts_col], utc=True)) // day_ns
    uniq = np.unique(days)
    n_days = len(uniq)
    if n_days == 0:
        return df.iloc[0:0], df.iloc[0:0], {"train": [], "test": []}
    n_train = max(1, int(math.floor(train_frac * n_days)))
    if n_days >= 2:
        n_train = min(n_train, max(1, n_days - max(1, min_test_days)))
    is_train = days <= uniq[n_train - 1]
    train, test = df.loc[is_train], df.loc[~is_train]

    def _span(d: np.ndarray) -> list[str]:
        if not len(d):
            return []
        return [pd.Timestamp(int(x) * day_ns, tz="UTC").date().isoformat() for x in (d[0], d[-1])]

    return train, test, {"train": _span(uniq[:n_train]), "test": _span(uniq[n_train:])}
