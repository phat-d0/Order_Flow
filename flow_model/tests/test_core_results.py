"""Gate item 6: write_results output passes validate_results (end-to-end synthetic demo), plus negatives."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from flow_model.core.demo import run_demo
from flow_model.core.results import METHODS, method_entry, validate_results, write_results
from flow_model.settings import load_config

CFG = load_config("core")
DEMO_DURATION_S = 3 * 86_400  # spans 4 UTC dates -> 2 train + 2 test


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    out = tmp_path_factory.mktemp("results") / "synthetic" / "SIM"
    fits, tables, tvf, sim = run_demo(CFG, out_dir=out, duration_s=DEMO_DURATION_S)
    return out, fits, tables, tvf, sim


def test_gate_write_results_passes_validate(demo):
    out = demo[0]
    assert validate_results(out) == []
    doc = json.loads((out / "fits.json").read_text())
    assert set(doc["methods"]) == set(METHODS)
    assert all(doc["methods"][m]["status"] == "ok" for m in METHODS)
    assert doc["split"]["train"] and doc["split"]["test"]
    for name in ("impact_curve", "ofi_bins", "resilience_decay", "intraday_profile", "execution"):
        assert (out / f"{name}.parquet").exists()
    assert set(pd.read_parquet(out / "resilience_decay.parquet")["sample"]) == {"IS", "OOS"}


def test_demo_out_of_sample_close_to_truth(demo):
    tvf = demo[3].set_index("param")
    for p in ("rho", "ofi_beta_1min", "sqrt_Y"):
        assert abs(tvf.loc[p, "out_of_sample"] / tvf.loc[p, "true"] - 1) < 0.5


def test_validator_flags_problems(tmp_path, demo):
    fits = json.loads((demo[0] / "fits.json").read_text())
    del fits["methods"]["amihud"]
    fits["methods"]["ofi"] = method_entry("failed", None)
    fits["tier"] = "Z"
    bad = write_results(tmp_path, fits, {"impact_curve": pd.DataFrame({"x_sqrt_q_over_v": [1.0]}),
                                         "mystery": pd.DataFrame({"a": [1]})})
    probs = validate_results(bad)
    joined = "\n".join(probs)
    assert "methods.amihud missing" in joined
    assert "needs a reason" in joined
    assert "tier" in joined
    assert "impact_curve.parquet: missing column" in joined
    assert "unexpected table mystery.parquet" in joined
    assert validate_results(tmp_path / "nope") != []


def test_not_applicable_entries_are_valid(tmp_path, demo):
    fits = json.loads((demo[0] / "fits.json").read_text())
    for m in ("ofi", "sqrt_law", "resilience"):
        fits["methods"][m] = method_entry("not_applicable", "tier C: bars only")
    assert validate_results(write_results(tmp_path, fits, {})) == []
