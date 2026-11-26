"""Price impact: square-root law on proxy metaorders, Kyle's lambda, Amihud illiquidity.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit

from flow_model.core.metrics import FitResult, floor_ns, ns_to_utc, ols_fit, ts_ns

METAORDER_COLUMNS = ["start", "end", "sign", "Q", "n_trades", "p_start", "p_end", "impact"]


def build_metaorders(trades: pd.DataFrame, max_gap: pd.Timedelta, min_trades: int) -> pd.DataFrame:
    """Runs of same-sign trades as proxy metaorders.

    A run breaks when the sign changes, the sign is 0, or two trades are more than ``max_gap``
    apart. Runs with fewer than ``min_trades`` trades are dropped.
    Columns ``start, end, sign, Q, n_trades, p_start, p_end, impact`` (impact = sign·log(p_end/p_start)).
    """
    if trades.empty:
        return pd.DataFrame(columns=METAORDER_COLUMNS)
    side = trades["side"].to_numpy(dtype=np.int64)
    t = ts_ns(trades["ts"])
    gap = np.diff(t, prepend=t[0]) > pd.Timedelta(max_gap).value
    brk = np.ones(len(side), dtype=bool)
    brk[1:] = (side[1:] != side[:-1]) | gap[1:] | (side[1:] == 0)
    run = np.cumsum(brk)
    df = pd.DataFrame(
        {"run": run, "ts": t, "side": side,
         "size": trades["size"].to_numpy(dtype=float), "price": trades["price"].to_numpy(dtype=float)}
    )
    g = df.groupby("run", sort=True)
    out = pd.DataFrame(
        {
            "start": g["ts"].first(),
            "end": g["ts"].last(),
            "sign": g["side"].first().astype(np.int8),
            "Q": g["size"].sum(),
            "n_trades": g["size"].size(),
            "p_start": g["price"].first(),
            "p_end": g["price"].last(),
        }
    )
    out = out[(out["n_trades"] >= min_trades) & (out["sign"] != 0)].reset_index(drop=True)
    out["impact"] = out["sign"] * np.log(out["p_end"] / out["p_start"])
    out["start"] = ns_to_utc(out["start"].to_numpy())
    out["end"] = ns_to_utc(out["end"].to_numpy())
    return out[METAORDER_COLUMNS]


def _power(x: np.ndarray, Y: float, delta: float) -> np.ndarray:
    return Y * x**delta


def fit_sqrt_law(metaorders: pd.DataFrame, sigma_daily: float, volume_daily: float) -> FitResult:
    """Fit impact ≈ Y·σ·√(Q/V); param ``Y`` (δ=0.5), free exponent ``delta`` in ``extra``.

    ``Y`` is weighted least squares with weights 1/x², x = σ·√(Q/V), i.e. Y = mean(impact/x):
    at constant participation the price noise over a metaorder has variance ∝ duration ∝ Q ∝ x²,
    so this is the efficient weighting (plain OLS through the origin is in ``extra['Y_ols']``).
    The free fit impact = Y_free·σ·(Q/V)^δ is non-linear least squares with the same weights;
    ``extra`` holds ``delta``, ``delta_se``, ``Y_free``. ``r2`` is the uncentred R² of the δ=0.5 fit.
    """
    df = metaorders[["Q", "impact"]].astype(float).dropna()
    df = df[df["Q"] > 0]
    n = len(df)
    if n < 3 or not (sigma_daily > 0 and volume_daily > 0):
        return FitResult.failed("sqrt_law", f"need >=3 metaorders and positive sigma/volume (n={n})", n=n)
    qv = df["Q"].to_numpy() / volume_daily
    x = sigma_daily * np.sqrt(qv)
    y = df["impact"].to_numpy()
    ratio = y / x
    Y = float(ratio.mean())
    ols = ols_fit(y, x, add_constant=False, name="sqrt_law_ols")
    resid = y - Y * x
    extra: dict[str, object] = {"sigma_daily": sigma_daily, "volume_daily": volume_daily,
                                "q_over_v_min": float(qv.min()), "q_over_v_max": float(qv.max()),
                                "weighting": "1/x^2 (Y = mean(impact/x))", "Y_ols": ols.params.get("x0"),
                                "Y_ols_se": ols.se.get("x0")}
    try:
        popt, pcov = curve_fit(lambda q, a, d: _power(q, a, d) * sigma_daily, qv, y, p0=[Y, 0.5],
                               sigma=x, maxfev=10000)
        perr = np.sqrt(np.clip(np.diag(pcov), 0.0, None))
        extra.update({"delta": float(popt[1]), "delta_se": float(perr[1]), "Y_free": float(popt[0]),
                      "Y_free_se": float(perr[0])})
    except (RuntimeError, ValueError) as exc:
        extra.update({"delta": None, "delta_error": str(exc)})
    return FitResult(name="sqrt_law", params={"Y": Y}, se={"Y": float(ratio.std(ddof=1) / np.sqrt(n))},
                     r2=float(1.0 - (resid @ resid) / (y @ y)) if (y @ y) > 0 else None, n=n, extra=extra)


def impact_curve(
    metaorders: pd.DataFrame, sigma_daily: float, volume_daily: float, n_bins: int, Y: float | None = None
) -> pd.DataFrame:
    """Binned impact curve for charts: ``x_sqrt_q_over_v, impact_norm, n, fitted``.

    x = √(Q/V); impact_norm = impact/σ; ``fitted`` = Y·x if ``Y`` given. Bins are quantiles of Q/V.
    """
    df = metaorders[["Q", "impact"]].astype(float).dropna()
    df = df[df["Q"] > 0]
    cols = ["x_sqrt_q_over_v", "impact_norm", "n", "fitted"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["x"] = np.sqrt(df["Q"] / volume_daily)
    df["y"] = df["impact"] / sigma_daily
    df["bin"] = pd.qcut(df["x"], q=min(n_bins, df["x"].nunique()), duplicates="drop")
    g = df.groupby("bin", observed=True)
    out = pd.DataFrame({"x_sqrt_q_over_v": g["x"].mean(), "impact_norm": g["y"].mean(), "n": g["y"].size()})
    out = out.reset_index(drop=True)
    out["fitted"] = Y * out["x_sqrt_q_over_v"] if Y is not None else np.nan
    return out[cols]


def bar_flow(trades: pd.DataFrame, bar: str, quotes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per-bar ``ret`` (log change of the last mid, or last trade price without quotes),
    ``signed_volume``, ``volume``, ``dollar_volume``, ``n_trades`` (index = bar start).

    Bars without trades keep zero flow; the return of a bar is measured from the previous bar's
    last price, so every bar in the covered range is present.
    """
    tb = floor_ns(trades["ts"], bar)
    size = trades["size"].to_numpy(dtype=float)
    px = trades["price"].to_numpy(dtype=float)
    flow = pd.DataFrame({"b": tb, "sv": trades["side"].to_numpy(dtype=float) * size,
                         "v": size, "dv": size * px})
    g = flow.groupby("b")
    out = pd.DataFrame({"signed_volume": g["sv"].sum(), "volume": g["v"].sum(),
                        "dollar_volume": g["dv"].sum(), "n_trades": g["v"].size()})
    if quotes is not None and len(quotes):
        mid = 0.5 * (quotes["bid_px"].to_numpy(dtype=float) + quotes["ask_px"].to_numpy(dtype=float))
        last = pd.Series(mid).groupby(floor_ns(quotes["ts"], bar)).last()
    else:
        last = pd.Series(px).groupby(tb).last()
    idx = out.index.union(last.index)
    step = pd.Timedelta(bar).value
    full = np.arange(idx.min(), idx.max() + step, step, dtype=np.int64)
    out = out.reindex(full).fillna({"signed_volume": 0.0, "volume": 0.0, "dollar_volume": 0.0, "n_trades": 0})
    lp = np.log(last.reindex(full).ffill())
    out["ret"] = lp.diff()
    out.index = ns_to_utc(full)
    out.index.name = "ts"
    return out


def kyle_lambda(returns: pd.Series, signed_volume: pd.Series, hac_lags: int | None = None) -> FitResult:
    """Regress returns on signed volume; param ``lambda`` (return units per unit of volume).

    Intercept is in ``params['alpha']``. ``hac_lags`` -> Newey-West standard errors.
    """
    fit = ols_fit(np.asarray(returns, dtype=float), np.asarray(signed_volume, dtype=float),
                  add_constant=True, hac_lags=hac_lags, name="kyle_lambda")
    if "error" in fit.extra:
        return fit
    ren = {"const": "alpha", "x0": "lambda"}
    fit.params = {ren[k]: v for k, v in fit.params.items()}
    fit.se = {ren[k]: v for k, v in fit.se.items()}
    return fit


def amihud(returns: pd.Series, dollar_volume: pd.Series, freq: str) -> FitResult:
    """Mean of |r| / dollar volume per ``freq`` bucket; param ``illiq``.

    ``returns`` and ``dollar_volume`` must share a DatetimeIndex (e.g. ``bar_flow`` output).
    Bucket return = sum of returns; buckets with zero dollar volume are skipped.
    ``illiq`` is in return units per currency unit (multiply by 1e6 for the usual scaling).
    """
    df = pd.DataFrame({"r": np.asarray(returns, dtype=float), "dv": np.asarray(dollar_volume, dtype=float)},
                      index=returns.index)
    g = df.resample(freq)
    agg = pd.DataFrame({"r": g["r"].sum(min_count=1), "dv": g["dv"].sum()})
    agg = agg[(agg["dv"] > 0) & agg["r"].notna()]
    n = len(agg)
    if n == 0:
        return FitResult.failed("amihud", "no buckets with volume")
    ratio = (agg["r"].abs() / agg["dv"]).to_numpy()
    se = float(ratio.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    return FitResult(name="amihud", params={"illiq": float(ratio.mean())}, se={"illiq": se}, n=n,
                     extra={"freq": freq, "illiq_median": float(np.median(ratio))})
