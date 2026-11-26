"""Resilience: post-trade reversion, spread/depth recovery, Obizhaeva-Wang ρ, propagator kernel.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import pandas as pd

from flow_model.core.metrics import FitResult


def large_trade_events(trades: pd.DataFrame, quantile: float) -> pd.DataFrame:
    """Trades whose size is above the ``quantile`` of the size distribution."""
    raise NotImplementedError("Quant agent: Phase 3")


def event_study(
    events: pd.DataFrame, trades: pd.DataFrame, quotes: pd.DataFrame, horizons: list[pd.Timedelta]
) -> pd.DataFrame:
    """Average path after events: ``lag_s, price_reversion, spread, depth, n``."""
    raise NotImplementedError("Quant agent: Phase 3")


def fit_exponential(decay: pd.DataFrame) -> FitResult:
    """Fit exp(-ρ t) recovery; param ``rho`` (1/s), ``half_life_s`` and ``aic`` in extra."""
    raise NotImplementedError("Quant agent: Phase 3")


def fit_power_law(decay: pd.DataFrame) -> FitResult:
    """Fit t^-β recovery; param ``beta``, ``aic`` in extra."""
    raise NotImplementedError("Quant agent: Phase 3")


def fit_propagator(trades: pd.DataFrame, max_lag: int) -> FitResult:
    """Trade-time propagator G(ℓ) ∝ ℓ^-β from the sign-response function."""
    raise NotImplementedError("Quant agent: Phase 3")


def compare_resilience(exp_fit: FitResult, pl_fit: FitResult) -> dict[str, object]:
    """``preferred`` ("exponential"|"power_law") and ``delta_aic``."""
    raise NotImplementedError("Quant agent: Phase 3")
