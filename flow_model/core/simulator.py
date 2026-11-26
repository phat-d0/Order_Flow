"""Synthetic limit order book with known impact, resilience and noise.

Generates trades and quotes in the common schema so every method runs unchanged on synthetic
and real data. ``truth`` holds the parameters the quality gate must recover.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class SimParams:
    """Simulator settings (see ``simulator`` section of config/core.yaml)."""

    duration_s: float
    dt_s: float
    base_price: float
    tick_size: float
    depth: float
    impact_coef: float
    resilience_rho: float
    permanent_fraction: float
    noise_sigma: float
    trade_rate: float
    size_mean: float
    size_tail: float
    metaorder_rate: float
    metaorder_participation: float
    quote_rate: float
    seed: int
    symbol: str = "SIM"

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "SimParams":
        """Build from the full core config (reads ``cfg['simulator']``)."""
        raise NotImplementedError("Quant agent: Phase 3")


@dataclass
class SimResult:
    """Synthetic data plus the true parameters that generated it."""

    trades: pd.DataFrame
    quotes: pd.DataFrame
    l2: pd.DataFrame | None
    truth: dict[str, float] = field(default_factory=dict)


def simulate(params: SimParams) -> SimResult:
    """Run the simulator. ``truth`` has at least kyle_lambda, sqrt_Y, rho, ofi_beta, permanent_fraction."""
    raise NotImplementedError("Quant agent: Phase 3")
