"""Trade signing: exchange flag (Tier A), Lee-Ready (B), tick rule and bulk volume classification (C).

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.

All inputs are assumed to hold a single symbol and be sorted by ``ts`` (schema guarantees).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from flow_model.core.metrics import ts_ns


def exchange_side(trades: pd.DataFrame) -> pd.Series:
    """The exchange-reported aggressor side (int8, +1/-1/0)."""
    return trades["side"].astype("int8").rename("side")


def _ffill_nonzero(sign: np.ndarray) -> np.ndarray:
    """Replace zeros by the last non-zero value (leading zeros stay 0)."""
    s = pd.Series(np.where(sign == 0, np.nan, sign))
    return s.ffill().fillna(0.0).to_numpy()


def tick_rule(trades: pd.DataFrame) -> pd.Series:
    """+1 on upticks, -1 on downticks, zero-ticks inherit the last non-zero sign."""
    dp = np.sign(np.diff(trades["price"].to_numpy(dtype=float), prepend=np.nan))
    dp = np.nan_to_num(dp, nan=0.0)
    return pd.Series(_ffill_nonzero(dp).astype("int8"), index=trades.index, name="side")


def prevailing_quotes(
    trades: pd.DataFrame, quotes: pd.DataFrame, quote_lag: pd.Timedelta, allow_exact: bool = False
) -> pd.DataFrame:
    """For each trade, the last quote with ``ts < trade_ts - quote_lag`` (``<=`` if ``allow_exact``).

    Returns ``bid_px, ask_px, bid_sz, ask_sz`` aligned to ``trades.index`` (NaN if none).
    """
    t = ts_ns(trades["ts"]) - pd.Timedelta(quote_lag).value
    q = ts_ns(quotes["ts"])
    pos = np.searchsorted(q, t, side="right" if allow_exact else "left") - 1
    ok = pos >= 0
    out = {}
    for col in ("bid_px", "ask_px", "bid_sz", "ask_sz"):
        vals = quotes[col].to_numpy(dtype=float)
        arr = np.full(len(t), np.nan)
        arr[ok] = vals[pos[ok]]
        out[col] = arr
    return pd.DataFrame(out, index=trades.index)


def lee_ready(
    trades: pd.DataFrame, quotes: pd.DataFrame, quote_lag: pd.Timedelta, allow_exact: bool = False
) -> pd.Series:
    """Quote rule vs the prevailing midpoint (quotes lagged by ``quote_lag``); tick rule at the mid.

    The prevailing quote is the last one strictly before ``ts - quote_lag`` by default, because
    feeds often stamp the post-trade quote with the trade's own timestamp.
    """
    pq = prevailing_quotes(trades, quotes, quote_lag, allow_exact)
    mid = 0.5 * (pq["bid_px"].to_numpy() + pq["ask_px"].to_numpy())
    px = trades["price"].to_numpy(dtype=float)
    quote_sign = np.sign(px - mid)
    quote_sign = np.nan_to_num(quote_sign, nan=0.0)
    tick = tick_rule(trades).to_numpy()
    out = np.where(quote_sign != 0, quote_sign, tick)
    return pd.Series(out.astype("int8"), index=trades.index, name="side")


def bulk_volume_classification(
    bars: pd.DataFrame, window: int, dist: str = "t", dof: float | None = None
) -> pd.DataFrame:
    """Split bar volume into buy/sell via the CDF of standardised price changes.

    z_t = Δclose_t / σ_t with σ_t the rolling std of Δclose over ``window`` bars (expanding until
    ``window`` bars exist; the first bar gets a 50/50 split). buy share = F(z), F = Student-t(dof)
    or standard normal CDF (Easley, López de Prado & O'Hara 2012).
    Returns ``buy_volume, sell_volume, signed_volume`` indexed like ``bars``.
    """
    close = bars["close"].to_numpy(dtype=float)
    vol = bars["volume"].to_numpy(dtype=float)
    dp = pd.Series(np.diff(close, prepend=np.nan))
    sigma = dp.rolling(window, min_periods=2).std().shift(1)
    sigma = sigma.fillna(dp.expanding(min_periods=2).std()).to_numpy()
    z = dp.to_numpy() / sigma
    if dist == "t":
        if dof is None:
            raise ValueError("dist='t' needs dof")
        frac = stats.t.cdf(z, dof)
    elif dist == "normal":
        frac = stats.norm.cdf(z)
    else:
        raise ValueError(f"unknown dist {dist!r}")
    frac = np.where(np.isfinite(z), frac, 0.5)
    frac = np.where(np.isfinite(dp.to_numpy()) & (sigma == 0), 0.5 + 0.5 * np.sign(dp.to_numpy()), frac)
    buy = vol * frac
    return pd.DataFrame(
        {"buy_volume": buy, "sell_volume": vol - buy, "signed_volume": 2.0 * buy - vol}, index=bars.index
    )


def sign_trades(
    trades: pd.DataFrame, quotes: pd.DataFrame | None = None, method: str = "auto", **kwargs: object
) -> pd.Series:
    """Dispatch: ``auto`` = exchange flag if present, else Lee-Ready with quotes, else tick rule.

    ``auto`` works row by row: rows with a non-zero exchange flag keep it, the rest are filled
    by Lee-Ready (if ``quotes``) or the tick rule. Other methods: ``exchange``, ``lee_ready``,
    ``tick``. kwargs: ``quote_lag`` (Timedelta, default 0), ``allow_exact`` (bool).
    """
    lag = pd.Timedelta(kwargs.get("quote_lag", pd.Timedelta(0)))
    allow_exact = bool(kwargs.get("allow_exact", False))
    if method == "exchange":
        return exchange_side(trades)
    if method == "tick":
        return tick_rule(trades)
    if method == "lee_ready":
        if quotes is None:
            raise ValueError("lee_ready needs quotes")
        return lee_ready(trades, quotes, lag, allow_exact)
    if method != "auto":
        raise ValueError(f"unknown method {method!r}")
    fallback = lee_ready(trades, quotes, lag, allow_exact) if quotes is not None and len(quotes) else tick_rule(trades)
    side = exchange_side(trades) if "side" in trades else pd.Series(0, index=trades.index, dtype="int8")
    return side.where(side != 0, fallback).astype("int8").rename("side")


def signing_accuracy(
    true_side: pd.Series, inferred_side: pd.Series, sizes: pd.Series | None = None
) -> dict[str, object]:
    """``accuracy``, ``accuracy_volume_weighted`` (if sizes given via ``.attrs``), ``n``, ``confusion``.

    Rows whose true side is 0 (unknown) are excluded; an inferred 0 counts as wrong.
    Sizes come from ``sizes`` or ``true_side.attrs["size"]``.
    """
    t = np.asarray(true_side, dtype=float)
    i = np.asarray(inferred_side, dtype=float)
    if sizes is None:
        sizes = true_side.attrs.get("size") if hasattr(true_side, "attrs") else None
    keep = t != 0
    correct = (t == i)[keep]
    n = int(keep.sum())
    acc = float(correct.mean()) if n else float("nan")
    acc_vw = None
    if sizes is not None and n:
        w = np.asarray(sizes, dtype=float)[keep]
        acc_vw = float((w * correct).sum() / w.sum()) if w.sum() > 0 else None
    conf = pd.crosstab(pd.Series(t[keep].astype(int), name="true"), pd.Series(i[keep].astype(int), name="inferred"))
    confusion = {f"{a},{b}": int(conf.loc[a, b]) for a in conf.index for b in conf.columns}
    return {"accuracy": acc, "accuracy_volume_weighted": acc_vw, "n": n, "confusion": confusion}
