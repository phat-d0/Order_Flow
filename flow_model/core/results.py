"""Writer and validator for the output contract (docs/CONTRACTS.md §3).

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


def write_results(out_dir: Path, fits: dict[str, Any], tables: dict[str, pd.DataFrame]) -> Path:
    """Write ``fits.json`` and each ``<name>.parquet`` into ``out_dir``; return ``out_dir``."""
    raise NotImplementedError("Quant agent: Phase 3")


def validate_results(out_dir: Path) -> list[str]:
    """Problems with ``out_dir`` vs the output contract (empty list = valid)."""
    raise NotImplementedError("Quant agent: Phase 3")
