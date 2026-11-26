"""Writer and validator for the output contract (docs/CONTRACTS.md §3).

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from flow_model.core.metrics import json_safe

METHODS: tuple[str, ...] = (
    "signing", "ofi", "sqrt_law", "kyle_lambda", "amihud", "resilience", "execution", "seasonality",
)
STATUSES: frozenset[str] = frozenset({"ok", "not_applicable", "insufficient_data", "failed"})
TIERS: frozenset[str] = frozenset({"A", "B", "C"})
TOP_KEYS: tuple[str, ...] = (
    "symbol", "asset_class", "tier", "sources", "caveats", "data", "split", "methods", "generated_at", "config",
)
DATA_KEYS: tuple[str, ...] = ("start", "end", "n_trades", "n_quotes", "n_bars", "gaps")
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "impact_curve": ("x_sqrt_q_over_v", "impact_norm", "n", "sample", "fitted"),
    "ofi_bins": ("window", "ofi_norm_bin", "dmid_mean", "dmid_se", "n", "sample"),
    "resilience_decay": ("lag_s", "price_reversion", "spread", "depth", "n", "exp_fit", "pl_fit", "sample"),
    "intraday_profile": ("bucket", "volume", "spread", "volatility", "n"),
    "execution": ("order_size_pct_adv", "schedule", "step", "time_frac", "trade_frac", "cost_bps",
                  "savings_vs_twap_bps"),
}
RESILIENCE_KEYS: tuple[str, ...] = ("exponential", "power_law", "propagator", "comparison")
SAMPLES: frozenset[str] = frozenset({"IS", "OOS"})


def method_entry(
    status: str,
    reason: str | None = None,
    in_sample: Any = None,
    out_of_sample: Any = None,
) -> dict[str, Any]:
    """One ``methods.<method>`` entry; FitResults (or lists/dicts of them) are converted to dicts."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}")
    return {
        "status": status,
        "reason": reason,
        "in_sample": json_safe(in_sample) if in_sample is not None else {},
        "out_of_sample": json_safe(out_of_sample) if out_of_sample is not None else {},
    }


def write_results(out_dir: Path, fits: dict[str, Any], tables: dict[str, pd.DataFrame]) -> Path:
    """Write ``fits.json`` and each ``<name>.parquet`` into ``out_dir``; return ``out_dir``.

    ``generated_at`` is filled with the current UTC time if missing. Values are made JSON-safe
    (NaN/inf -> null). Table names are the file stems of §3 (e.g. ``"impact_curve"``).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = dict(fits)
    doc.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
    with open(out_dir / "fits.json", "w", encoding="utf-8") as fh:
        json.dump(json_safe(doc), fh, indent=2, sort_keys=False, allow_nan=False)
    for name, df in tables.items():
        stem = name[:-8] if name.endswith(".parquet") else name
        df.reset_index(drop=True).to_parquet(out_dir / f"{stem}.parquet", index=False)
    return out_dir


def _check_method(name: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return [f"methods.{name} is not an object"]
    probs = []
    status = entry.get("status")
    if status not in STATUSES:
        probs.append(f"methods.{name}.status {status!r} not in {sorted(STATUSES)}")
    if "reason" not in entry:
        probs.append(f"methods.{name}.reason missing")
    elif status != "ok" and not entry.get("reason"):
        probs.append(f"methods.{name}: status {status!r} needs a reason")
    for key in ("in_sample", "out_of_sample"):
        if key not in entry:
            probs.append(f"methods.{name}.{key} missing")
        elif not isinstance(entry[key], (dict, list)):
            probs.append(f"methods.{name}.{key} must be an object or list")
    if status == "ok" and not entry.get("in_sample"):
        probs.append(f"methods.{name}: status ok but in_sample is empty")
    if status == "ok" and name == "resilience" and isinstance(entry.get("in_sample"), dict):
        missing = [k for k in RESILIENCE_KEYS if k not in entry["in_sample"]]
        if missing:
            probs.append(f"methods.resilience.in_sample missing {missing}")
    if status == "ok" and name == "execution" and isinstance(entry.get("in_sample"), dict):
        if "rows" not in entry["in_sample"]:
            probs.append("methods.execution.in_sample missing 'rows'")
    return probs


def _check_fits(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["fits.json is not an object"]
    probs = [f"missing top-level key {k!r}" for k in TOP_KEYS if k not in doc]
    if doc.get("tier") not in TIERS and "tier" in doc:
        probs.append(f"tier {doc.get('tier')!r} not in {sorted(TIERS)}")
    if "sources" in doc and not isinstance(doc["sources"], list):
        probs.append("sources must be a list")
    if "caveats" in doc and not isinstance(doc["caveats"], list):
        probs.append("caveats must be a list")
    data = doc.get("data")
    if isinstance(data, dict):
        probs += [f"data.{k} missing" for k in DATA_KEYS if k not in data]
        if "gaps" in data and not isinstance(data["gaps"], list):
            probs.append("data.gaps must be a list")
    elif "data" in doc:
        probs.append("data must be an object")
    split = doc.get("split")
    if isinstance(split, dict):
        probs += [f"split.{k} missing" for k in ("train", "test") if k not in split]
    elif "split" in doc:
        probs.append("split must be an object")
    methods = doc.get("methods")
    if isinstance(methods, dict):
        probs += [f"methods.{m} missing" for m in METHODS if m not in methods]
        probs += [f"unknown method {m!r}" for m in methods if m not in METHODS]
        for m in METHODS:
            if m in methods:
                probs += _check_method(m, methods[m])
    elif "methods" in doc:
        probs.append("methods must be an object")
    if "generated_at" in doc:
        try:
            datetime.fromisoformat(str(doc["generated_at"]).replace("Z", "+00:00"))
        except ValueError:
            probs.append("generated_at is not ISO-8601")
    return probs


def _check_table(path: Path) -> list[str]:
    stem = path.stem
    if stem not in TABLE_COLUMNS:
        return [f"unexpected table {path.name}"]
    try:
        df = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - report any read failure as a problem
        return [f"{path.name}: unreadable ({exc})"]
    probs = [f"{path.name}: missing column {c!r}" for c in TABLE_COLUMNS[stem] if c not in df.columns]
    if "sample" in TABLE_COLUMNS[stem] and "sample" in df.columns:
        bad = set(df["sample"].dropna().astype(str)) - SAMPLES
        if bad:
            probs.append(f"{path.name}: sample values {sorted(bad)} not in {sorted(SAMPLES)}")
    return probs


def validate_results(out_dir: Path) -> list[str]:
    """Problems with ``out_dir`` vs the output contract (empty list = valid)."""
    out_dir = Path(out_dir)
    fpath = out_dir / "fits.json"
    if not fpath.exists():
        return [f"{fpath} missing"]
    try:
        with open(fpath, encoding="utf-8") as fh:
            doc = json.load(fh, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError as exc:
        return [f"fits.json is not strict JSON: {exc}"]
    probs = _check_fits(doc)
    for p in sorted(out_dir.glob("*.parquet")):
        probs += _check_table(p)
    return probs
