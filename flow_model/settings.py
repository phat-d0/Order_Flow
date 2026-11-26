"""Config loading: every parameter used by the package comes from ``flow_model/config/*.yaml``."""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
CONFIG_DIR = PACKAGE_ROOT / "config"


def deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict with ``override`` recursively merged on top of ``base``."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@lru_cache(maxsize=None)
def _read_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(name: str, config_dir: str | Path | None = None) -> dict[str, Any]:
    """Load ``<config_dir>/<name>.yaml`` as a fresh (mutable) dict."""
    directory = Path(config_dir) if config_dir else CONFIG_DIR
    return copy.deepcopy(_read_yaml(str(directory / f"{name}.yaml")))


def list_instruments(asset_class: str | None = None, tier: str | None = None) -> list[str]:
    """Symbols in ``instruments.yaml``, optionally filtered by asset class and/or tier."""
    registry = load_config("instruments")["instruments"]
    return [
        sym
        for sym, meta in registry.items()
        if (asset_class is None or meta["asset_class"] == asset_class)
        and (tier is None or meta["tier"] == tier)
    ]


def instrument(symbol: str) -> dict[str, Any]:
    """Registry entry for ``symbol`` plus ``symbol``, allowed ``methods`` and tier ``caveats``."""
    cfg = load_config("instruments")
    meta = dict(cfg["instruments"][symbol])
    meta["symbol"] = symbol
    meta["methods"] = list(cfg["tier_methods"][meta["tier"]])
    meta["caveats"] = list(cfg["caveats"][meta["tier"]])
    return meta


def core_config(symbol: str | None = None) -> dict[str, Any]:
    """``core.yaml`` with the instrument's ``overrides`` (if any) deep-merged on top."""
    cfg = load_config("core")
    if symbol is not None:
        cfg = deep_merge(cfg, instrument(symbol).get("overrides", {}) or {})
    return cfg


def storage_root() -> Path:
    """Absolute path of the raw parquet store (``sources.yaml`` -> storage.root, relative to repo)."""
    root = Path(load_config("sources")["storage"]["root"])
    return root if root.is_absolute() else REPO_ROOT / root
