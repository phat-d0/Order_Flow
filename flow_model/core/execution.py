"""Optimal execution: Obizhaeva-Wang with Almgren-Chriss baseline, vs TWAP and VWAP.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.

Cost model (discrete linear propagator, all in price units)
---------------------------------------------------------
Buying x_i shares at times t_i (Σ x_i = Q). A trade of size x moves the price by
(lambda_perm + eta_temp)·x; the eta_temp part decays as exp(-rho·Δt) (rho=None: it is gone by
the next step). Each trade pays half the spread plus the displacement left by earlier trades
plus half its own impact (it walks a linear book), so

    expected cost = spread/2·|Q| + ½ xᵀ G x,   G_ij = lambda_perm + eta_temp·exp(-rho|t_i - t_j|)

(rho=None -> G = lambda_perm·11ᵀ + eta_temp·I). The permanent part costs lambda_perm·Q²/2 for
every schedule, so only the transient part shapes the optimum. Obizhaeva-Wang minimises this
cost exactly: x* ∝ G⁻¹1, which puts blocks at both ends and a flat rate in between.
Almgren-Chriss minimises ½ηΣx² + λ_ra·σ²·Σ τ·y_k² (no resilience memory; risk aversion λ_ra);
its schedule is then costed under the same model as the others.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class ImpactModel:
    """Fitted impact parameters used to cost a schedule.

    ``lambda_perm``/``eta_temp``: price units per share; ``rho``: 1/s (None = no transient memory,
    Tier C λ-only model); ``sigma``: price units per √s; ``spread``: price units; ``price``:
    reference price for bps conversion (optional addition).
    """

    lambda_perm: float
    eta_temp: float
    rho: float | None
    depth: float | None
    sigma: float
    spread: float
    price: float | None = None

    @classmethod
    def from_fits(
        cls,
        fits: dict[str, Any],
        cfg: dict[str, Any],
        *,
        price: float | None = None,
        sigma: float | None = None,
        spread: float | None = None,
        depth: float | None = None,
    ) -> "ImpactModel":
        """Build from a ``fits.json``-style dict; Tier C (no ρ) falls back to a λ-only model.

        Reads ``methods.kyle_lambda.in_sample.params.lambda`` (log-return per share at bar
        ``cfg['impact']['kyle_bar']``) and, if ``status == "ok"``,
        ``methods.resilience.in_sample.exponential`` (``rho``, ``extra.permanent_fraction``).
        Kyle λ over a bar Δ equals κ·(φ + (1-φ)·(1-e^{-ρΔ})/(ρΔ)) under the propagator model, which
        is inverted for the instantaneous κ; then lambda_perm = φκ·price, eta_temp = (1-φ)κ·price.
        Without ρ: lambda_perm = eta_temp = λ·price (each child's instantaneous impact equals the
        bar λ and does not persist past one step) — an assumption, flagged in the caveats.
        ``price``/``sigma`` (price units per √s)/``spread``/``depth`` come from kwargs or from
        ``fits['market']`` if present.
        """
        methods = fits.get("methods", fits)
        market = fits.get("market", {}) or {}
        price = price if price is not None else market.get("price")
        sigma = sigma if sigma is not None else market.get("sigma", 0.0)
        spread = spread if spread is not None else market.get("spread", 0.0)
        depth = depth if depth is not None else market.get("depth")
        if price is None:
            raise ValueError("from_fits needs a reference price (kwarg or fits['market']['price'])")
        kl = methods.get("kyle_lambda", {})
        if kl.get("status", "ok") != "ok":
            raise ValueError(f"kyle_lambda not available: {kl.get('reason')}")
        lam = float(kl["in_sample"]["params"]["lambda"])
        res = methods.get("resilience", {})
        rho = phi = None
        if res.get("status") == "ok":
            ex = res["in_sample"].get("exponential", {})
            rho = ex.get("params", {}).get("rho")
            phi = ex.get("extra", {}).get("permanent_fraction")
        if rho is None or phi is None or not (rho > 0) or not (0.0 <= phi <= 1.0):
            return cls(lambda_perm=lam * price, eta_temp=lam * price, rho=None, depth=depth,
                       sigma=float(sigma), spread=float(spread), price=float(price))
        bar = pd.Timedelta(cfg["impact"]["kyle_bar"]).total_seconds()
        f = (1.0 - math.exp(-rho * bar)) / (rho * bar)
        kappa = lam / (phi + (1.0 - phi) * f)
        return cls(lambda_perm=phi * kappa * price, eta_temp=(1.0 - phi) * kappa * price, rho=float(rho),
                   depth=depth, sigma=float(sigma), spread=float(spread), price=float(price))


@dataclass
class Schedule:
    """Trades per step (sum = Q) at the given times (seconds from start)."""

    times: np.ndarray
    trades: np.ndarray
    name: str

    def to_frame(self) -> pd.DataFrame:
        """``step, time_s, time_frac, trade, trade_frac``."""
        T = float(self.times[-1]) if len(self.times) else 0.0
        Q = float(self.trades.sum())
        return pd.DataFrame({
            "step": np.arange(len(self.trades)),
            "time_s": self.times,
            "time_frac": self.times / T if T > 0 else np.zeros(len(self.times)),
            "trade": self.trades,
            "trade_frac": self.trades / Q if Q != 0 else np.zeros(len(self.trades)),
        })


def _times(T: float, n: int) -> np.ndarray:
    """n trade times: 0, T/(n-1), ..., T (both ends included, as in Obizhaeva-Wang)."""
    return np.linspace(0.0, T, n) if n > 1 else np.zeros(1)


def twap(Q: float, T: float, n: int) -> Schedule:
    """Equal slices."""
    return Schedule(_times(T, n), np.full(n, Q / n), "twap")


def vwap(Q: float, T: float, n: int, volume_profile: np.ndarray) -> Schedule:
    """Slices proportional to the expected volume profile (resampled to ``n`` points if needed)."""
    prof = np.asarray(volume_profile, dtype=float)
    if len(prof) != n:
        prof = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(prof)), prof)
    prof = np.clip(prof, 0.0, None)
    w = prof / prof.sum() if prof.sum() > 0 else np.full(n, 1.0 / n)
    return Schedule(_times(T, n), Q * w, "vwap")


def kernel_matrix(times: np.ndarray, model: ImpactModel) -> np.ndarray:
    """G_ij = lambda_perm + eta_temp·exp(-rho|t_i - t_j|) (identity transient part if rho is None)."""
    dt = np.abs(times[:, None] - times[None, :])
    if model.rho is None or not math.isfinite(model.rho):
        trans = np.eye(len(times))
    else:
        trans = np.exp(-model.rho * dt)
    return model.lambda_perm + model.eta_temp * trans


def _min_quadratic(H: np.ndarray, c: np.ndarray, Q: float) -> np.ndarray:
    """argmin ½xᵀHx + cᵀx subject to Σx = Q (KKT system, least squares if singular)."""
    n = len(c)
    K = np.zeros((n + 1, n + 1))
    K[:n, :n] = H
    K[:n, n] = 1.0
    K[n, :n] = 1.0
    rhs = np.concatenate([-c, [Q]])
    sol, *_ = np.linalg.lstsq(K, rhs, rcond=None)
    return sol[:n]


def almgren_chriss(Q: float, T: float, n: int, model: ImpactModel, risk_aversion: float) -> Schedule:
    """Almgren-Chriss mean-variance optimal schedule (no resilience).

    Minimises ½·eta·Σx_k² + risk_aversion·σ²·τ·Σ_k y_k², y_k = Q - Σ_{i<=k} x_i (holdings after
    trade k, k < n-1), τ = T/(n-1). risk_aversion = 0 or eta = 0 -> TWAP.
    """
    t = _times(T, n)
    if n == 1:
        return Schedule(t, np.array([Q]), "almgren_chriss")
    tau = T / (n - 1)
    eta = model.eta_temp
    if eta <= 0 or risk_aversion <= 0:
        return Schedule(t, np.full(n, Q / n), "almgren_chriss")
    L = np.tril(np.ones((n - 1, n)))  # cumulative sums of x for k = 0..n-2
    a = 2.0 * risk_aversion * model.sigma**2 * tau
    H = eta * np.eye(n) + a * (L.T @ L)
    c = -a * Q * L.sum(axis=0)
    return Schedule(t, _min_quadratic(H, c, Q), "almgren_chriss")


def obizhaeva_wang(Q: float, T: float, n: int, model: ImpactModel) -> Schedule:
    """Obizhaeva-Wang optimal schedule (block trades at ends, continuous in between).

    Exact minimiser of ``expected_cost`` over n equally spaced trades: x ∝ G⁻¹1 with G the
    transient kernel (the permanent part adds the same cost to every schedule). Falls back to
    TWAP when there is no transient impact (eta_temp = 0).
    """
    t = _times(T, n)
    if model.eta_temp <= 0:
        return Schedule(t, np.full(n, Q / n), "obizhaeva_wang")
    G = kernel_matrix(t, ImpactModel(0.0, model.eta_temp, model.rho, model.depth, model.sigma, model.spread))
    w = np.linalg.solve(G, np.ones(n))
    return Schedule(t, Q * w / w.sum(), "obizhaeva_wang")


def expected_cost(schedule: Schedule, model: ImpactModel) -> float:
    """Expected implementation shortfall of ``schedule`` under ``model`` (price units × shares)."""
    x = np.asarray(schedule.trades, dtype=float)
    G = kernel_matrix(np.asarray(schedule.times, dtype=float), model)
    return float(0.5 * model.spread * np.abs(x).sum() + 0.5 * x @ G @ x)


def cost_bps(cost: float, Q: float, price: float | None) -> float:
    """Cost per unit notional in basis points (NaN without a price)."""
    if price is None or price <= 0 or Q == 0:
        return float("nan")
    return 1e4 * cost / (abs(Q) * price)


def compare_schedules(
    Q: float,
    T: float,
    n: int,
    model: ImpactModel,
    volume_profile: np.ndarray | None = None,
    risk_aversion: float = 0.0,
    return_schedules: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, list[Schedule]]:
    """``schedule, cost, cost_bps, savings_vs_twap_bps`` for TWAP, VWAP, AC and OW.

    VWAP uses ``volume_profile`` (row omitted if None). ``risk_aversion`` feeds Almgren-Chriss
    (config ``execution.risk_aversion``).
    """
    scheds = [twap(Q, T, n)]
    if volume_profile is not None:
        scheds.append(vwap(Q, T, n, volume_profile))
    scheds += [almgren_chriss(Q, T, n, model, risk_aversion), obizhaeva_wang(Q, T, n, model)]
    costs = [expected_cost(s, model) for s in scheds]
    df = pd.DataFrame({"schedule": [s.name for s in scheds], "cost": costs})
    df["cost_bps"] = [cost_bps(c, Q, model.price) for c in costs]
    df["savings_vs_twap_bps"] = df["cost_bps"].iloc[0] - df["cost_bps"]
    return (df, scheds) if return_schedules else df


def execution_table(
    model: ImpactModel,
    adv: float,
    cfg: dict[str, Any],
    volume_profile: np.ndarray | None = None,
) -> pd.DataFrame:
    """Rows for ``execution.parquet``: each order size in ``cfg['execution']['order_sizes_pct_adv']``
    (% of ``adv`` shares) × schedule × step: ``order_size_pct_adv, schedule, step, time_frac,
    trade_frac, cost_bps, savings_vs_twap_bps``."""
    ex = cfg["execution"]
    frames = []
    for pct in ex["order_sizes_pct_adv"]:
        Q = adv * float(pct) / 100.0
        table, scheds = compare_schedules(Q, float(ex["horizon_s"]), int(ex["n_steps"]), model,
                                          volume_profile, float(ex["risk_aversion"]), return_schedules=True)
        for s, (_, row) in zip(scheds, table.iterrows()):
            f = s.to_frame()[["step", "time_frac", "trade_frac"]]
            f.insert(0, "schedule", s.name)
            f.insert(0, "order_size_pct_adv", float(pct))
            f["cost_bps"] = row["cost_bps"]
            f["savings_vs_twap_bps"] = row["savings_vs_twap_bps"]
            frames.append(f)
    return pd.concat(frames, ignore_index=True)
