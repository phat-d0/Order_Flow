"""Order-flow imbalance (Cont, Kukanov & Stoikov 2014) and ΔP-on-OFI regressions.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import pandas as pd

from flow_model.core.metrics import FitResult


def event_ofi(quotes: pd.DataFrame) -> pd.Series:
    """Per-update OFI contribution e_n from best bid/ask price and size changes."""
    raise NotImplementedError("Quant agent: Phase 3")


def aggregate_ofi(quotes: pd.DataFrame, window: str) -> pd.DataFrame:
    """Sum OFI per ``window``; columns ``ofi, dmid, depth, n_events``."""
    raise NotImplementedError("Quant agent: Phase 3")


def fit_ofi(agg: pd.DataFrame) -> FitResult:
    """Regress ΔP on OFI/depth; params ``beta`` and ``alpha``."""
    raise NotImplementedError("Quant agent: Phase 3")


def ofi_by_window(quotes: pd.DataFrame, windows: list[str]) -> list[FitResult]:
    """``fit_ofi`` at each window (e.g. 1s, 10s, 1min) to show how β and R² change with horizon."""
    raise NotImplementedError("Quant agent: Phase 3")
