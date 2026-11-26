"""Resilience: post-trade reversion, spread/depth recovery, Obizhaeva-Wang ρ, propagator kernel.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.

Conventions
-----------
``event_study`` measures, for each large trade with sign ε at t0, the signed log-mid change
ε·log(mid(t0+h)/mid(t0-)) from the last quote strictly before the trade. Its mean over events,
the column ``price_reversion``, is the impact still present at lag h; it decays from the peak
toward the permanent level as liquidity refills. The exponential model is
I(h) = I_inf + (I_0 - I_inf)·exp(-ρ h) (Obizhaeva-Wang), the power-law model
I(h) = I_inf + A·h^(-β). Both have three parameters, so AIC compares them fairly.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
from scipy.linalg import solve_toeplitz

from flow_model.core.metrics import FitResult, ols_fit, ts_ns


def large_trade_events(trades: pd.DataFrame, quantile: float) -> pd.DataFrame:
    """Trades whose size is above the ``quantile`` of the size distribution.

    Returns ``ts, price, size, side`` (only signed trades, side != 0), plus ``size_threshold`` in
    ``.attrs``.
    """
    thr = float(trades["size"].quantile(quantile)) if len(trades) else math.nan
    ev = trades.loc[(trades["size"] > thr) & (trades["side"] != 0), ["ts", "price", "size", "side"]]
    ev = ev.reset_index(drop=True)
    ev.attrs["size_threshold"] = thr
    return ev


def _quote_at(q_ns: np.ndarray, t_ns: np.ndarray, strict: bool) -> np.ndarray:
    """Index of the last quote with ts < t (strict) or <= t; -1 if none."""
    return np.searchsorted(q_ns, t_ns, side="left" if strict else "right") - 1


def event_study(
    events: pd.DataFrame, trades: pd.DataFrame, quotes: pd.DataFrame, horizons: list[pd.Timedelta]
) -> pd.DataFrame:
    """Average path after events: ``lag_s, price_reversion, spread, depth, n``.

    Lag 0 is the pre-trade state (``price_reversion`` = 0 by definition, spread/depth before the
    trade); then one row per horizon. ``spread`` is the mean relative spread (ask-bid)/mid and
    ``depth`` the mean of (bid_sz + ask_sz)/2 at that lag. Extra column ``price_reversion_se``.
    ``trades`` is accepted for interface symmetry (events already carry their sign).
    """
    cols = ["lag_s", "price_reversion", "spread", "depth", "n", "price_reversion_se"]
    if events.empty or quotes.empty:
        return pd.DataFrame(columns=cols)
    q_ns = ts_ns(quotes["ts"])
    bid = quotes["bid_px"].to_numpy(dtype=float)
    ask = quotes["ask_px"].to_numpy(dtype=float)
    mid = 0.5 * (bid + ask)
    rel_spread = (ask - bid) / mid
    depth = 0.5 * (quotes["bid_sz"].to_numpy(dtype=float) + quotes["ask_sz"].to_numpy(dtype=float))
    t0 = ts_ns(events["ts"])
    sign = events["side"].to_numpy(dtype=float)
    i0 = _quote_at(q_ns, t0, strict=True)
    ok0 = i0 >= 0
    lm0 = np.where(ok0, np.log(mid[np.maximum(i0, 0)]), np.nan)
    last_ts = q_ns[-1]
    rows = [{"lag_s": 0.0, "price_reversion": 0.0, "price_reversion_se": 0.0,
             "spread": float(np.nanmean(rel_spread[i0[ok0]])) if ok0.any() else np.nan,
             "depth": float(np.nanmean(depth[i0[ok0]])) if ok0.any() else np.nan, "n": int(ok0.sum())}]
    for h in horizons:
        h_ns = pd.Timedelta(h).value
        th = t0 + h_ns
        ih = _quote_at(q_ns, th, strict=False)
        ok = ok0 & (ih >= 0) & (th <= last_ts)
        path = sign[ok] * (np.log(mid[ih[ok]]) - lm0[ok])
        n = int(ok.sum())
        rows.append({
            "lag_s": h_ns / 1e9,
            "price_reversion": float(path.mean()) if n else np.nan,
            "price_reversion_se": float(path.std(ddof=1) / math.sqrt(n)) if n > 1 else np.nan,
            "spread": float(np.nanmean(rel_spread[ih[ok]])) if n else np.nan,
            "depth": float(np.nanmean(depth[ih[ok]])) if n else np.nan,
            "n": n,
        })
    return pd.DataFrame(rows, columns=cols)


def _decay_arrays(decay: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    d = decay[decay["lag_s"] > 0].dropna(subset=["price_reversion"])
    t = d["lag_s"].to_numpy(dtype=float)
    y = d["price_reversion"].to_numpy(dtype=float)
    if "price_reversion_se" in d and d["price_reversion_se"].notna().all() and (d["price_reversion_se"] > 0).all():
        s = d["price_reversion_se"].to_numpy(dtype=float)
    else:
        s = 1.0 / np.sqrt(np.maximum(d["n"].to_numpy(dtype=float), 1.0))
    return t, y, s


def _aic(resid_w: np.ndarray, k: int) -> float:
    """Gaussian AIC on standardised residuals (known-variance form): χ² + 2k."""
    return float(resid_w @ resid_w + 2 * k)


def _exp_model(t: np.ndarray, perm: float, trans: float, rho: float) -> np.ndarray:
    return perm + trans * np.exp(-rho * t)


def _pl_model(t: np.ndarray, perm: float, amp: float, beta: float) -> np.ndarray:
    return perm + amp * t ** (-beta)


def fit_exponential(decay: pd.DataFrame) -> FitResult:
    """Fit exp(-ρ t) recovery; param ``rho`` (1/s), ``half_life_s`` and ``aic`` in extra.

    Model I(h) = perm + trans·exp(-ρ h), weighted by the per-lag standard errors. Also returns
    params ``perm`` and ``trans`` and ``extra['permanent_fraction'] = perm/(perm + trans)``.
    """
    t, y, s = _decay_arrays(decay)
    if len(t) < 4:
        return FitResult.failed("resilience_exponential", f"need >=4 lags, got {len(t)}", n=len(t))
    peak = float(y[0]) if y[0] != 0 else float(np.max(np.abs(y)))
    p0 = [float(y[-1]), peak - float(y[-1]), math.log(2.0) / float(np.median(t))]
    try:
        popt, pcov = curve_fit(_exp_model, t, y, p0=p0, sigma=s, absolute_sigma=True,
                               bounds=([-np.inf, -np.inf, 0.0], [np.inf, np.inf, np.inf]), maxfev=20000)
    except (RuntimeError, ValueError) as exc:
        return FitResult.failed("resilience_exponential", str(exc), n=len(t))
    perr = np.sqrt(np.clip(np.diag(pcov), 0.0, None))
    rw = (y - _exp_model(t, *popt)) / s
    perm, trans, rho = (float(v) for v in popt)
    total = perm + trans
    return FitResult(
        name="resilience_exponential",
        params={"rho": rho, "perm": perm, "trans": trans},
        se={"rho": float(perr[2]), "perm": float(perr[0]), "trans": float(perr[1])},
        r2=_weighted_r2(y, _exp_model(t, *popt), s),
        n=len(t),
        extra={"half_life_s": math.log(2.0) / rho if rho > 0 else None, "aic": _aic(rw, 3),
               "permanent_fraction": perm / total if total != 0 else None},
    )


def _weighted_r2(y: np.ndarray, fitted: np.ndarray, s: np.ndarray) -> float | None:
    w = 1.0 / s**2
    ybar = float((w * y).sum() / w.sum())
    sst = float((w * (y - ybar) ** 2).sum())
    return 1.0 - float((w * (y - fitted) ** 2).sum()) / sst if sst > 0 else None


def fit_power_law(decay: pd.DataFrame) -> FitResult:
    """Fit t^-β recovery; param ``beta``, ``aic`` in extra.

    Model I(h) = perm + amp·h^(-β), β >= 0, weighted like ``fit_exponential``.
    """
    t, y, s = _decay_arrays(decay)
    if len(t) < 4:
        return FitResult.failed("resilience_power_law", f"need >=4 lags, got {len(t)}", n=len(t))
    p0 = [float(y[-1]), float(y[0] - y[-1]) * float(t[0]) ** 0.5, 0.5]
    try:
        popt, pcov = curve_fit(_pl_model, t, y, p0=p0, sigma=s, absolute_sigma=True,
                               bounds=([-np.inf, -np.inf, 0.0], [np.inf, np.inf, np.inf]), maxfev=20000)
    except (RuntimeError, ValueError) as exc:
        return FitResult.failed("resilience_power_law", str(exc), n=len(t))
    perr = np.sqrt(np.clip(np.diag(pcov), 0.0, None))
    rw = (y - _pl_model(t, *popt)) / s
    return FitResult(
        name="resilience_power_law",
        params={"beta": float(popt[2]), "perm": float(popt[0]), "amp": float(popt[1])},
        se={"beta": float(perr[2]), "perm": float(perr[0]), "amp": float(perr[1])},
        r2=_weighted_r2(y, _pl_model(t, *popt), s),
        n=len(t),
        extra={"aic": _aic(rw, 3)},
    )


def fitted_curves(decay: pd.DataFrame, exp_fit: FitResult, pl_fit: FitResult) -> pd.DataFrame:
    """``decay`` plus ``exp_fit`` and ``pl_fit`` model values at each lag (for resilience_decay.parquet)."""
    out = decay.copy()
    t = out["lag_s"].to_numpy(dtype=float)
    pe, pp = exp_fit.params, pl_fit.params
    out["exp_fit"] = _exp_model(t, pe["perm"], pe["trans"], pe["rho"]) if pe else np.nan
    with np.errstate(divide="ignore"):
        out["pl_fit"] = np.where(t > 0, _pl_model(np.where(t > 0, t, 1.0), pp["perm"], pp["amp"], pp["beta"]),
                                 np.nan) if pp else np.nan
    return out


def _autocov_fft(x: np.ndarray, max_lag: int) -> np.ndarray:
    """Mean of x_t x_{t+l} for l = 0..max_lag (FFT)."""
    n = len(x)
    size = 1 << (2 * n - 1).bit_length()
    f = np.fft.rfft(x, size)
    ac = np.fft.irfft(f * np.conj(f), size)[: max_lag + 1]
    return ac / (n - np.arange(max_lag + 1))


def _crosscov_fft(y: np.ndarray, x: np.ndarray, max_lag: int) -> np.ndarray:
    """Mean of y_t x_{t-l} for l = 0..max_lag (FFT)."""
    n = len(x)
    size = 1 << (2 * n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(y, size) * np.conj(np.fft.rfft(x, size)), size)[: max_lag + 1]
    return c / (n - np.arange(max_lag + 1))


def fit_propagator(trades: pd.DataFrame, max_lag: int, quotes: pd.DataFrame | None = None) -> FitResult:
    """Trade-time propagator G(ℓ) ∝ ℓ^-β from the sign-response function.

    Transient-impact model in trade time: r_n = Σ_{l=0}^{L} g_l ε_{n-l} + noise, with r_n the log
    price change from trade n to trade n+1 (trade prices, or the mid just before each trade if
    ``quotes`` given) and G(ℓ) = Σ_{l<ℓ} g_l the cumulative kernel. The normal equations use
    the Toeplitz sign autocovariance (FFT), so memory is O(n). G(ℓ) = A·ℓ^-β is then fitted by
    log-log OLS on lags with G > 0. ``extra`` holds ``G`` (list), ``response`` R(ℓ) and
    ``sign_acf``, ``G_inf`` (= G(max_lag)).
    """
    eps = trades["side"].to_numpy(dtype=float)
    n = len(eps)
    if n <= 2 * max_lag:
        return FitResult.failed("propagator", f"need > {2 * max_lag} trades, got {n}", n=n)
    if quotes is not None and len(quotes):
        q_ns = ts_ns(quotes["ts"])
        idx = _quote_at(q_ns, ts_ns(trades["ts"]), strict=True)
        m = 0.5 * (quotes["bid_px"].to_numpy(dtype=float) + quotes["ask_px"].to_numpy(dtype=float))
        lp = np.log(np.where(idx >= 0, m[np.maximum(idx, 0)], np.nan))
        lp = pd.Series(lp).bfill().to_numpy()
    else:
        lp = np.log(trades["price"].to_numpy(dtype=float))
    r = np.diff(lp)
    e = eps[:-1]
    e_c = e - e.mean()
    acov = _autocov_fft(e_c, max_lag)
    xcov = _crosscov_fft(r - r.mean(), e_c, max_lag)
    g = solve_toeplitz(acov, xcov)
    G = np.cumsum(g)
    lags = np.arange(1, max_lag + 2)
    acf = acov / acov[0]
    resp = np.array([np.mean(e[: len(e) - l + 1] * (lp[l:] - lp[:-l])[: len(e) - l + 1]) if l else 0.0
                     for l in range(0, min(max_lag, len(e) - 1) + 1)])
    pos = G > 0
    extra = {"G": G.tolist(), "response": resp.tolist(), "sign_acf": acf.tolist(), "G_inf": float(G[-1]),
             "price": "mid" if quotes is not None and len(quotes) else "trade"}
    if pos.sum() < 3:
        fit = FitResult.failed("propagator", "kernel not positive on enough lags", n=n)
        fit.extra.update(extra)
        return fit
    ll = ols_fit(np.log(G[pos]), np.log(lags[pos]), add_constant=True, name="propagator_loglog")
    beta = -ll.params["x0"]
    return FitResult(name="propagator", params={"beta": beta, "G1": float(G[0]), "A": float(np.exp(ll.params["const"]))},
                     se={"beta": ll.se["x0"]}, r2=ll.r2, n=n, extra=extra)


def compare_resilience(exp_fit: FitResult, pl_fit: FitResult) -> dict[str, object]:
    """``preferred`` ("exponential"|"power_law") and ``delta_aic`` (= AIC_power_law - AIC_exponential)."""
    a_e, a_p = exp_fit.extra.get("aic"), pl_fit.extra.get("aic")
    if a_e is None and a_p is None:
        return {"preferred": None, "delta_aic": None}
    if a_p is None:
        return {"preferred": "exponential", "delta_aic": None}
    if a_e is None:
        return {"preferred": "power_law", "delta_aic": None}
    d = float(a_p - a_e)
    return {"preferred": "exponential" if d > 0 else "power_law", "delta_aic": d}
