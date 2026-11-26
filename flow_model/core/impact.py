"""Price impact: square-root law on proxy metaorders, Kyle's lambda, Amihud illiquidity.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import pandas as pd

from flow_model.core.metrics import FitResult


def build_metaorders(trades: pd.DataFrame, max_gap: pd.Timedelta, min_trades: int) -> pd.DataFrame:
    """Runs of same-sign trades as proxy metaorders.

    Columns ``start, end, sign, Q, n_trades, p_start, p_end, impact`` (impact = sign·log(p_end/p_start)).
    """
    raise NotImplementedError("Quant agent: Phase 3")


def fit_sqrt_law(metaorders: pd.DataFrame, sigma_daily: float, volume_daily: float) -> FitResult:
    """Fit impact ≈ Y·σ·√(Q/V); param ``Y`` (δ=0.5), free exponent ``delta`` in ``extra``."""
    raise NotImplementedError("Quant agent: Phase 3")


def kyle_lambda(returns: pd.Series, signed_volume: pd.Series) -> FitResult:
    """Regress returns on signed volume; param ``lambda``."""
    raise NotImplementedError("Quant agent: Phase 3")


def amihud(returns: pd.Series, dollar_volume: pd.Series, freq: str) -> FitResult:
    """Mean of |r| / dollar volume per ``freq`` bucket; param ``illiq``."""
    raise NotImplementedError("Quant agent: Phase 3")
