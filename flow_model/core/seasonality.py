"""Intraday volume/volatility/spread profiles and deseasonalisation.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import pandas as pd


def intraday_profile(
    df: pd.DataFrame,
    value_col: str,
    bucket: str,
    ts_col: str = "ts",
    tz: str | None = None,
    session: tuple[str, str] | None = None,
) -> pd.DataFrame:
    """Average of ``value_col`` per time-of-day bucket; columns ``bucket, mean, median, n``."""
    raise NotImplementedError("Quant agent: Phase 3")


def deseasonalize(df: pd.DataFrame, profile: pd.DataFrame, value_col: str, bucket: str) -> pd.Series:
    """``value_col`` divided by its bucket's profile mean."""
    raise NotImplementedError("Quant agent: Phase 3")
