"""Order-flow imbalance (Cont, Kukanov & Stoikov 2014) and ΔP-on-OFI regressions.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from flow_model.core.metrics import FitResult, floor_ns, ns_to_utc, ols_fit, ts_ns


def event_ofi(quotes: pd.DataFrame) -> pd.Series:
    """Per-update OFI contribution e_n from best bid/ask price and size changes.

    e_n = 1{Pb_n >= Pb_n-1} qb_n - 1{Pb_n <= Pb_n-1} qb_n-1 - 1{Pa_n <= Pa_n-1} qa_n + 1{Pa_n >= Pa_n-1} qa_n-1.
    The first row is 0. Rows with missing sizes give NaN (OFI needs sizes).
    """
    pb = quotes["bid_px"].to_numpy(dtype=float)
    qb = quotes["bid_sz"].to_numpy(dtype=float)
    pa = quotes["ask_px"].to_numpy(dtype=float)
    qa = quotes["ask_sz"].to_numpy(dtype=float)
    e = np.zeros(len(pb))
    if len(pb) > 1:
        dpb, dpa = pb[1:] - pb[:-1], pa[1:] - pa[:-1]
        e[1:] = (
            np.where(dpb >= 0, qb[1:], 0.0)
            - np.where(dpb <= 0, qb[:-1], 0.0)
            - np.where(dpa <= 0, qa[1:], 0.0)
            + np.where(dpa >= 0, qa[:-1], 0.0)
        )
        bad = ~(np.isfinite(qb[1:]) & np.isfinite(qb[:-1]) & np.isfinite(qa[1:]) & np.isfinite(qa[:-1]))
        e[1:][bad] = np.nan
    return pd.Series(e, index=quotes.index, name="ofi")


def aggregate_ofi(quotes: pd.DataFrame, window: str, max_gap: str | pd.Timedelta | None = None) -> pd.DataFrame:
    """Sum OFI per ``window``; columns ``ofi, dmid, depth, n_events`` (index = window start).

    ``dmid`` is the change of the last mid in this window vs the last mid of the previous
    non-empty window (the OFI of the window's first update is measured against that same quote).
    ``depth`` is the time-weighted mean of (bid_sz + ask_sz)/2. Windows with no update are dropped.
    If ``max_gap`` is set, windows more than ``max_gap`` after the previous non-empty one get
    ``dmid = NaN`` (e.g. across an overnight close).
    """
    e = event_ofi(quotes).to_numpy()
    mid = 0.5 * (quotes["bid_px"].to_numpy(dtype=float) + quotes["ask_px"].to_numpy(dtype=float))
    dep = 0.5 * (quotes["bid_sz"].to_numpy(dtype=float) + quotes["ask_sz"].to_numpy(dtype=float))
    t_ns = ts_ns(quotes["ts"])
    dur = np.diff(t_ns, append=t_ns[-1] if len(t_ns) else 0).astype(float)
    frame = pd.DataFrame({"b": floor_ns(quotes["ts"], window), "e": e, "mid": mid, "dw": dep * dur, "dur": dur, "dep": dep})
    g = frame.groupby("b", sort=True)
    out = pd.DataFrame(
        {
            "ofi": g["e"].sum(min_count=1),
            "mid": g["mid"].last(),
            "dw": g["dw"].sum(),
            "dur": g["dur"].sum(),
            "dep_mean": g["dep"].mean(),
            "n_events": g["e"].size(),
        }
    )
    out.index = ns_to_utc(out.index.to_numpy())
    out["depth"] = np.where(out["dur"] > 0, out["dw"] / out["dur"].where(out["dur"] > 0), out["dep_mean"])
    out["dmid"] = out["mid"].diff()
    if max_gap is not None and len(out):
        gaps = out.index.to_series().diff() > pd.Timedelta(window) + pd.Timedelta(max_gap)
        out.loc[gaps.to_numpy(), "dmid"] = np.nan
    out.index.name = "ts"
    return out[["ofi", "dmid", "depth", "n_events", "mid"]]


def _ofi_regressor(agg: pd.DataFrame, depth_norm: str) -> tuple[np.ndarray, float]:
    if depth_norm == "sample":
        norm = float(np.nanmean(agg["depth"].to_numpy(dtype=float)))
        return agg["ofi"].to_numpy(dtype=float) / norm, norm
    if depth_norm == "window":
        return (agg["ofi"] / agg["depth"]).to_numpy(dtype=float), float("nan")
    raise ValueError(f"unknown depth_norm {depth_norm!r}")


def fit_ofi(agg: pd.DataFrame, hac_lags: int | None = None, depth_norm: str = "sample") -> FitResult:
    """Regress ΔP on OFI/depth; params ``beta`` and ``alpha``.

    ``depth_norm="sample"`` (default) divides by the mean depth over the fit sample, as CKS do
    with the average depth of each estimation interval; ``"window"`` uses each window's depth.
    ΔP is in price units, so ``beta`` is the mid move (price units) per mean-depth of OFI.
    """
    x, norm = _ofi_regressor(agg, depth_norm)
    fit = ols_fit(agg["dmid"].to_numpy(dtype=float), x, add_constant=True, hac_lags=hac_lags, name="ofi")
    if "error" in fit.extra:
        return fit
    ren = {"const": "alpha", "x0": "beta"}
    fit.params = {ren[k]: v for k, v in fit.params.items()}
    fit.se = {ren[k]: v for k, v in fit.se.items()}
    fit.extra.update({"depth_norm": depth_norm, "mean_depth": norm})
    return fit


def ofi_by_window(quotes: pd.DataFrame, windows: list[str], **kwargs: object) -> list[FitResult]:
    """``fit_ofi`` at each window (e.g. 1s, 10s, 1min) to show how β and R² change with horizon.

    kwargs go to ``fit_ofi`` (``hac_lags``, ``depth_norm``) and ``aggregate_ofi`` (``max_gap``).
    """
    max_gap = kwargs.pop("max_gap", None)
    fits = []
    for w in windows:
        fit = fit_ofi(aggregate_ofi(quotes, w, max_gap=max_gap), **kwargs)  # type: ignore[arg-type]
        fit.name = f"ofi_{w}"
        fit.extra["window"] = w
        fits.append(fit)
    return fits


def ofi_bins(agg: pd.DataFrame, n_bins: int, window: str, depth_norm: str = "sample") -> pd.DataFrame:
    """Quantile-binned OFI/depth vs mean ΔP for charts: ``window, ofi_norm_bin, dmid_mean, dmid_se, n``."""
    x, _ = _ofi_regressor(agg, depth_norm)
    df = pd.DataFrame({"x": x, "y": agg["dmid"].to_numpy(dtype=float)}).dropna()
    if df.empty:
        return pd.DataFrame(columns=["window", "ofi_norm_bin", "dmid_mean", "dmid_se", "n"])
    df["bin"] = pd.qcut(df["x"], q=min(n_bins, df["x"].nunique()), duplicates="drop")
    g = df.groupby("bin", observed=True)
    out = pd.DataFrame(
        {
            "ofi_norm_bin": g["x"].mean(),
            "dmid_mean": g["y"].mean(),
            "dmid_se": g["y"].std() / np.sqrt(g["y"].count()),
            "n": g["y"].count(),
        }
    ).reset_index(drop=True)
    out.insert(0, "window", window)
    return out
