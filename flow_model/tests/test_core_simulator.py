"""Simulator: schema-valid output, determinism, and the exact identities the truth values rely on."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from flow_model.core import ofi
from flow_model.core.metrics import ts_ns
from flow_model.core.simulator import SimParams, simulate, truth_values
from flow_model.data import schema
from flow_model.settings import load_config

CFG = load_config("core")
SHORT_S = 1800.0


@pytest.fixture(scope="module")
def sim():
    return simulate(SimParams.from_config(CFG, duration_s=SHORT_S))


def test_outputs_match_schemas(sim):
    assert schema.validate(sim.trades, "trades") == []
    assert schema.validate(sim.quotes, "quotes") == []
    assert sim.l2 is None
    assert {"kyle_lambda", "sqrt_Y", "rho", "ofi_beta", "permanent_fraction"} <= set(sim.truth)


def test_deterministic_given_seed(sim):
    again = simulate(SimParams.from_config(CFG, duration_s=SHORT_S))
    pd.testing.assert_frame_equal(sim.trades, again.trades)
    other = simulate(SimParams.from_config(CFG, duration_s=SHORT_S, seed=CFG["simulator"]["seed"] + 1))
    assert not sim.trades["price"].equals(other.trades["price"])


def test_trades_print_at_the_touch(sim):
    q = sim.quotes
    t = sim.trades
    pos = np.searchsorted(q["ts"].to_numpy(), t["ts"].to_numpy(), side="left") - 1
    ask, bid = q["ask_px"].to_numpy()[pos], q["bid_px"].to_numpy()[pos]
    buy = t["side"].to_numpy() > 0
    assert np.allclose(t["price"].to_numpy()[buy], ask[buy])
    assert np.allclose(t["price"].to_numpy()[~buy], bid[~buy])


def test_ofi_identity_is_exact(sim):
    """Boundary rows make the CKS OFI telescope: Δmid(displayed) − tick·Δfill = tick/(2·depth)·OFI."""
    p = SimParams.from_config(CFG)
    q = sim.quotes
    e = ofi.event_ofi(q).to_numpy()[1:]
    k_b = np.rint(q["bid_px"].to_numpy() / p.tick_size)
    u_b = k_b + q["bid_sz"].to_numpy() / p.depth  # latent bid edge in ticks
    du = np.diff(u_b)
    assert np.allclose(e, 2 * p.depth * du, atol=1e-6 * p.depth)


def test_depth_and_spread_conventions(sim):
    q = sim.quotes
    p = SimParams.from_config(CFG)
    total = (q["bid_sz"] + q["ask_sz"]).to_numpy()
    boundary = np.isclose(total, 2 * p.depth)  # zero-duration rows at a crossed tick: both levels full
    assert np.allclose(total[~boundary], p.depth)
    dur = np.diff(ts_ns(q["ts"]), append=0)
    assert (dur[boundary & (np.arange(len(q)) < len(q) - 1)] == 0).all()
    spread_ticks = np.rint((q["ask_px"] - q["bid_px"]) / p.tick_size)
    assert set(np.unique(spread_ticks)) <= {p.spread_ticks + 1, p.spread_ticks + 2}


def test_metaorders_table(sim):
    m = sim.metaorders
    p = SimParams.from_config(CFG)
    assert m is not None and len(m) == sim.truth["n_metaorders"]
    assert (m["Q"] == m["n_trades"] * p.size_mean).all()
    assert np.allclose(m["impact"], m["sign"] * np.log(m["p_end"] / m["p_start"]))


def test_truth_closed_forms():
    p = SimParams.from_config(CFG)
    t = truth_values(p)
    a = math.exp(-p.resilience_rho * p.dt_s)
    n = round(p.kyle_bar_s / p.dt_s)
    f = (1 - a**n) / (n * (1 - a))
    assert t["kyle_lambda"] == pytest.approx(p.impact_coef * (p.permanent_fraction + (1 - p.permanent_fraction) * f))
    assert t["ofi_beta"] == pytest.approx(p.tick_size / 4)
    assert t["sqrt_Y"] > 0 and t["volume_daily"] > p.trade_rate * p.size_mean * p.duration_s
    no_meta = truth_values(SimParams.from_config(CFG, metaorder_rate=0.0))
    assert math.isnan(no_meta["sqrt_Y"]) and no_meta["kyle_lambda_exact"]


def test_validation_rejects_bad_params():
    with pytest.raises(ValueError):
        simulate(SimParams.from_config(CFG, spread_ticks=1.5))
    with pytest.raises(ValueError):
        SimParams.from_config(CFG, not_a_key=1)
