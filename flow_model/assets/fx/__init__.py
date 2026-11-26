"""Fx asset module (owned by the Fx agent). Exposes run() per docs/CONTRACTS.md §4."""

from __future__ import annotations

from pathlib import Path


def run(symbols: list[str] | None = None, results_root: Path | None = None) -> list[Path]:
    """Load stored data, run every core method the tier allows, write results; return result dirs."""
    raise NotImplementedError("Fx agent: Phase 4")
