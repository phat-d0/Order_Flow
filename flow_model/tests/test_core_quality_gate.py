"""Phase 3 quality gate: every core estimator recovers the simulator's closed-form truth.

Seeds and path lengths are fixed below; tolerances come from config/core.yaml -> quality_gate.
Gate items (see .claude/agents/quant.md):
  1. impact coefficient: Kyle λ (no-metaorder path, 24h, seed 11) and sqrt-law Y (4 seeds × 60h pooled)
  2. resilience ρ (6 seeds × 24h, event-study tables pooled)
  3. OFI β at 1s / 10s / 1min (default config path, seed from config)
  4. signing: exchange flag and Lee-Ready ≥ min_sign_accuracy on the default path; tick rule ≥ min accuracy
     when mid diffusion between trades ≪ spread (noise_sigma = 2e-5) and below it at the default noise
  5. Obizhaeva-Wang beats TWAP with finite resilience; equals TWAP without transient memory
  6. write_results output passes validate_results (in test_core_results.py)
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from flow_model.core import execution as ex
from flow_model.core import impact, ofi, resilience, signing
from flow_model.core.simulator import SimParams, simulate
from flow_model.settings import load_config

CFG = load_config("core")
GATE = CFG["quality_gate"]

KYLE_SEED, KYLE_DURATION_S = 11, 86_400
SQRT_SEEDS, SQRT_DURATION_S = (1, 2, 3, 4), 216_000
RHO_SEEDS, RHO_DURATION_S = (1, 2, 3, 4, 5, 6), 86_400
LOW_NOISE_SIGMA = 2e-5


def rel_err(est: float, true: float) -> float:
    return abs(est / true - 1.0)


def pool_decay(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """n-weighted average of event-study tables from independent paths (pooled standard errors)."""
    cat = pd.concat(tables)
    cat["w_mean"] = cat["price_reversion"] * cat["n"]
    cat["w_var"] = (cat["price_reversion_se"] * cat["n"]) ** 2
    g = cat.groupby("lag_s")
    n = g["n"].sum()
    return pd.DataFrame({
        "lag_s": n.index,
        "price_reversion": (g["w_mean"].sum() / n).to_numpy(),
        "price_reversion_se": (np.sqrt(g["w_var"].sum()) / n).to_numpy(),
        "spread": g["spread"].mean().to_numpy(),
        "depth": g["depth"].mean().to_numpy(),
        "n": n.to_numpy(),
    })


@pytest.fixture(scope="module")
def default_sim():
    return simulate(SimParams.from_config(CFG))


# 1 ------------------------------------------------------------------------------------------
def test_gate_kyle_lambda_recovered():
    r = simulate(SimParams.from_config(CFG, metaorder_rate=0.0, duration_s=KYLE_DURATION_S, seed=KYLE_SEED))
    assert r.truth["kyle_lambda_exact"]
    bf = impact.bar_flow(r.trades, CFG["impact"]["kyle_bar"], quotes=r.quotes)
    fit = impact.kyle_lambda(bf["ret"], bf["signed_volume"], hac_lags=CFG["impact"]["hac_lags"])
    assert rel_err(fit.params["lambda"], r.truth["kyle_lambda"]) < GATE["rel_tol_impact"]


def test_gate_sqrt_law_Y_recovered():
    tables, truth = [], None
    for seed in SQRT_SEEDS:
        r = simulate(SimParams.from_config(CFG, duration_s=SQRT_DURATION_S, seed=seed))
        tables.append(r.metaorders)
        truth = r.truth
    fit = impact.fit_sqrt_law(pd.concat(tables), truth["sigma_daily"], truth["volume_daily"])
    assert fit.n > 1000
    assert rel_err(fit.params["Y"], truth["sqrt_Y"]) < GATE["rel_tol_impact"]


# 2 ------------------------------------------------------------------------------------------
def test_gate_resilience_rho_recovered():
    rc = CFG["resilience"]
    horizons = [pd.Timedelta(seconds=h) for h in rc["horizons_s"]]
    tables, truth = [], None
    for seed in RHO_SEEDS:
        r = simulate(SimParams.from_config(CFG, duration_s=RHO_DURATION_S, seed=seed))
        ev = resilience.large_trade_events(r.trades, rc["large_trade_quantile"])
        tables.append(resilience.event_study(ev, r.trades, r.quotes, horizons))
        truth = r.truth
    decay = pool_decay(tables)
    fit = resilience.fit_exponential(decay)
    assert rel_err(fit.params["rho"], truth["rho"]) < GATE["rel_tol_rho"]
    # the true kernel is exponential: AIC must prefer it
    cmp_ = resilience.compare_resilience(fit, resilience.fit_power_law(decay))
    assert cmp_["preferred"] == "exponential"


# 3 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("window", CFG["ofi"]["windows"])
def test_gate_ofi_beta_recovered(default_sim, window):
    fit = ofi.fit_ofi(ofi.aggregate_ofi(default_sim.quotes, window))
    assert rel_err(fit.params["beta"], default_sim.truth["ofi_beta"]) < GATE["rel_tol_ofi_beta"]


# 4 ------------------------------------------------------------------------------------------
def test_gate_signing_exchange_and_lee_ready(default_sim):
    t = default_sim.trades
    lag = pd.Timedelta(milliseconds=CFG["signing"]["lee_ready_quote_lag_ms"])
    acc_ex = signing.signing_accuracy(t["side"], signing.exchange_side(t))["accuracy"]
    acc_lr = signing.signing_accuracy(t["side"], signing.lee_ready(t, default_sim.quotes, lag))["accuracy"]
    assert acc_ex == 1.0
    # quotes are current and trades print at the touch -> the quote rule is right by construction
    assert acc_lr >= GATE["min_sign_accuracy"]


def test_gate_tick_rule_when_theory_applies(default_sim):
    lo = simulate(SimParams.from_config(CFG, noise_sigma=LOW_NOISE_SIGMA))
    acc_lo = signing.signing_accuracy(lo.trades["side"], signing.tick_rule(lo.trades))["accuracy"]
    acc_hi = signing.signing_accuracy(default_sim.trades["side"], signing.tick_rule(default_sim.trades))["accuracy"]
    assert acc_lo >= GATE["min_sign_accuracy"]
    # with ~1.4 ticks of mid diffusion between trades the tick rule degrades, as theory says
    assert acc_hi < acc_lo


# 5 ------------------------------------------------------------------------------------------
def _model_from_truth(truth: dict, rho: float | None) -> ex.ImpactModel:
    p = truth["base_price"]
    k, phi = truth["kappa"], truth["permanent_fraction"]
    return ex.ImpactModel(lambda_perm=phi * k * p, eta_temp=(1 - phi) * k * p, rho=rho, depth=None,
                          sigma=truth["noise_sigma"] * p, spread=0.0, price=p)


def test_gate_ow_beats_twap_with_finite_resilience(default_sim):
    e = CFG["execution"]
    Q, T, n = 10_000.0, float(e["horizon_s"]), int(e["n_steps"])
    # resilience slow relative to the horizon (one e-fold over T) -> transient impact matters
    slow = _model_from_truth(default_sim.truth, rho=1.0 / T)
    tab = ex.compare_schedules(Q, T, n, slow).set_index("schedule")
    assert tab.loc["obizhaeva_wang", "cost"] < tab.loc["twap", "cost"]
    assert tab.loc["obizhaeva_wang", "savings_vs_twap_bps"] > 0
    # the model's own rho: still never worse than TWAP
    own = _model_from_truth(default_sim.truth, rho=default_sim.truth["rho"])
    tab2 = ex.compare_schedules(Q, T, n, own).set_index("schedule")
    assert tab2.loc["obizhaeva_wang", "cost"] <= tab2.loc["twap", "cost"]


@pytest.mark.parametrize("rho", [None, 1e6])
def test_gate_ow_equals_twap_without_transient_memory(default_sim, rho):
    e = CFG["execution"]
    Q, T, n = 10_000.0, float(e["horizon_s"]), int(e["n_steps"])
    m = _model_from_truth(default_sim.truth, rho=rho)
    tab = ex.compare_schedules(Q, T, n, m).set_index("schedule")
    assert math.isclose(tab.loc["obizhaeva_wang", "cost"], tab.loc["twap", "cost"], rel_tol=1e-9)
    no_transient = ex.ImpactModel(m.lambda_perm, 0.0, 0.05, None, m.sigma, 0.0, m.price)
    assert np.allclose(ex.obizhaeva_wang(Q, T, n, no_transient).trades, Q / n)
