"""Trade signing: exchange flag (Tier A), Lee-Ready (B), tick rule and bulk volume classification (C).

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import pandas as pd


def exchange_side(trades: pd.DataFrame) -> pd.Series:
    """The exchange-reported aggressor side (int8, +1/-1/0)."""
    raise NotImplementedError("Quant agent: Phase 3")


def tick_rule(trades: pd.DataFrame) -> pd.Series:
    """+1 on upticks, -1 on downticks, zero-ticks inherit the last non-zero sign."""
    raise NotImplementedError("Quant agent: Phase 3")


def lee_ready(trades: pd.DataFrame, quotes: pd.DataFrame, quote_lag: pd.Timedelta) -> pd.Series:
    """Quote rule vs the prevailing midpoint (quotes lagged by ``quote_lag``); tick rule at the mid."""
    raise NotImplementedError("Quant agent: Phase 3")


def bulk_volume_classification(
    bars: pd.DataFrame, window: int, dist: str = "t", dof: float | None = None
) -> pd.DataFrame:
    """Split bar volume into buy/sell via the CDF of standardised price changes.

    Returns ``buy_volume, sell_volume, signed_volume`` indexed like ``bars``.
    """
    raise NotImplementedError("Quant agent: Phase 3")


def sign_trades(
    trades: pd.DataFrame, quotes: pd.DataFrame | None = None, method: str = "auto", **kwargs: object
) -> pd.Series:
    """Dispatch: ``auto`` = exchange flag if present, else Lee-Ready with quotes, else tick rule."""
    raise NotImplementedError("Quant agent: Phase 3")


def signing_accuracy(true_side: pd.Series, inferred_side: pd.Series) -> dict[str, object]:
    """``accuracy``, ``accuracy_volume_weighted`` (if sizes given via ``.attrs``), ``n``, ``confusion``."""
    raise NotImplementedError("Quant agent: Phase 3")
