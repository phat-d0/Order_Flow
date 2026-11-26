"""Optimal execution: Obizhaeva-Wang with Almgren-Chriss baseline, vs TWAP and VWAP.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class ImpactModel:
    """Fitted impact parameters used to cost a schedule."""

    lambda_perm: float
    eta_temp: float
    rho: float | None
    depth: float | None
    sigma: float
    spread: float

    @classmethod
    def from_fits(cls, fits: dict[str, Any], cfg: dict[str, Any]) -> "ImpactModel":
        """Build from a ``fits.json``-style dict; Tier C (no ρ) falls back to a λ-only model."""
        raise NotImplementedError("Quant agent: Phase 3")


@dataclass
class Schedule:
    """Trades per step (sum = Q) at the given times (seconds from start)."""

    times: np.ndarray
    trades: np.ndarray
    name: str


def twap(Q: float, T: float, n: int) -> Schedule:
    """Equal slices."""
    raise NotImplementedError("Quant agent: Phase 3")


def vwap(Q: float, T: float, n: int, volume_profile: np.ndarray) -> Schedule:
    """Slices proportional to the expected volume profile."""
    raise NotImplementedError("Quant agent: Phase 3")


def almgren_chriss(Q: float, T: float, n: int, model: ImpactModel, risk_aversion: float) -> Schedule:
    """Almgren-Chriss mean-variance optimal schedule (no resilience)."""
    raise NotImplementedError("Quant agent: Phase 3")


def obizhaeva_wang(Q: float, T: float, n: int, model: ImpactModel) -> Schedule:
    """Obizhaeva-Wang optimal schedule (block trades at ends, continuous in between)."""
    raise NotImplementedError("Quant agent: Phase 3")


def expected_cost(schedule: Schedule, model: ImpactModel) -> float:
    """Expected implementation shortfall of ``schedule`` under ``model`` (price units × shares)."""
    raise NotImplementedError("Quant agent: Phase 3")


def compare_schedules(
    Q: float, T: float, n: int, model: ImpactModel, volume_profile: np.ndarray | None = None
) -> pd.DataFrame:
    """``schedule, cost, cost_bps, savings_vs_twap_bps`` for TWAP, VWAP, AC and OW."""
    raise NotImplementedError("Quant agent: Phase 3")
