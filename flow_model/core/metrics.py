"""Shared fit container, OLS helper and date-based train/test split.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


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
        raise NotImplementedError("Quant agent: Phase 3")


def ols_fit(
    y: pd.Series | np.ndarray,
    X: pd.DataFrame | np.ndarray,
    add_constant: bool = True,
    hac_lags: int | None = None,
    name: str = "ols",
) -> FitResult:
    """OLS of ``y`` on ``X`` (optionally Newey-West HAC standard errors)."""
    raise NotImplementedError("Quant agent: Phase 3")


def train_test_split_by_date(
    df: pd.DataFrame, train_frac: float, ts_col: str = "ts"
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, list[str]]]:
    """Split on whole UTC dates: earliest ``train_frac`` of dates -> train, rest -> test."""
    raise NotImplementedError("Quant agent: Phase 3")
