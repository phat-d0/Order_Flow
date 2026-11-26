"""Synthetic limit order book with known impact, resilience and noise.

Generates trades and quotes in the common schema so every method runs unchanged on synthetic
and real data. ``truth`` holds the parameters the quality gate must recover.

Model (all on a clock of step ``dt``; decay and fundamental noise act at grid times, trades
arrive at continuous times between grid points):

* log mid  x(t) = W(t) + P(t) + T(t)
* W: Brownian fundamental, increment N(0, sigma^2 dt) at each grid time.
* each trade i (sign e_i, size v_i) moves x by kappa*e_i*v_i instantly; a share ``phi`` of that
  goes to the permanent part P, the rest to the transient part T.
* T decays by a = exp(-rho dt) at every grid time (exact discretisation of exp(-rho t)).
  -> propagator G(t) = kappa * (phi + (1 - phi) exp(-rho t))   (Obizhaeva-Wang type).
* displayed book: latent bid edge B = mid - s/2, ask edge A = mid + s/2 (s = spread_ticks*tick).
  The book holds ``depth`` shares per tick beyond each edge, so the best bid is the tick level
  just below B holding depth*fill, fill = fraction of that level inside the edge (in (0, 1]).
  Quote rows are added at every crossed tick boundary, which makes the Cont-Kukanov-Stoikov OFI
  exactly (2*depth/tick) * Δmid (latent) -> ``ofi_beta`` is known in closed form.
* noise trades: Poisson(trade_rate), Pareto sizes (tail ``size_tail``, mean ``size_mean``),
  random sign; they execute at the displayed ask (buy) / bid (sell).
* metaorders: Poisson(metaorder_rate) starts, log-uniform durations, constant child size
  ``size_mean`` every Δc = dt*round((1-p)/(p*trade_rate*dt)) seconds (participation p of volume).

See docs/notes/quant.md for how every ``truth`` value is derived.

Owned by the Quant agent. Signatures are fixed by docs/CONTRACTS.md §2;
optional keyword arguments may be added. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy.signal import lfilter

from flow_model.data import schema

SOURCE = "sim"


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
    spread_ticks: int = 1
    metaorder_min_duration_s: float | None = None
    metaorder_max_duration_s: float | None = None
    start_ts: str | None = None
    kyle_bar_s: float | None = None

    @classmethod
    def from_config(cls, cfg: dict[str, Any], **overrides: Any) -> "SimParams":
        """Build from the full core config (reads ``cfg['simulator']``; ``impact.kyle_bar`` for truth)."""
        sim = dict(cfg["simulator"])
        kyle_bar = cfg.get("impact", {}).get("kyle_bar")
        if kyle_bar is not None:
            sim.setdefault("kyle_bar_s", pd.Timedelta(kyle_bar).total_seconds())
        sim.update(overrides)
        known = set(cls.__dataclass_fields__)
        unknown = set(sim) - known
        if unknown:
            raise ValueError(f"unknown simulator keys: {sorted(unknown)}")
        return cls(**sim)

    def validate(self) -> None:
        """Raise ``ValueError`` on inconsistent settings."""
        if int(self.spread_ticks) != self.spread_ticks or self.spread_ticks < 0:
            raise ValueError("spread_ticks must be a non-negative integer (OFI exactness needs it)")
        if not 0.0 <= self.permanent_fraction <= 1.0:
            raise ValueError("permanent_fraction must be in [0, 1]")
        if self.size_tail <= 1.0:
            raise ValueError("size_tail must exceed 1 for a finite mean size")
        if self.metaorder_rate > 0 and not (0.0 < self.metaorder_participation < 1.0):
            raise ValueError("metaorder_participation must be in (0, 1)")
        if self.metaorder_rate > 0 and (
            self.metaorder_min_duration_s is None or self.metaorder_max_duration_s is None
        ):
            raise ValueError("metaorder_min/max_duration_s are required when metaorder_rate > 0")


@dataclass
class SimResult:
    """Synthetic data plus the true parameters that generated it.

    ``metaorders`` (optional addition) lists the true metaorders with the same columns as
    ``impact.build_metaorders`` (p_start = mid before the first child, p_end = mid after the last).
    """

    trades: pd.DataFrame
    quotes: pd.DataFrame
    l2: pd.DataFrame | None
    truth: dict[str, float] = field(default_factory=dict)
    metaorders: pd.DataFrame | None = None


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def _pareto_sizes(rng: np.random.Generator, n: int, mean: float, tail: float) -> np.ndarray:
    """Pareto(tail) sizes with the given mean."""
    xm = mean * (tail - 1.0) / tail
    return xm * (1.0 - rng.random(n)) ** (-1.0 / tail)


def child_steps(p: SimParams) -> int:
    """Grid steps between metaorder children (participation p of volume with equal sizes)."""
    rate_c = p.trade_rate * p.metaorder_participation / (1.0 - p.metaorder_participation)
    return max(1, int(round(1.0 / (rate_c * p.dt_s))))


def _metaorder_children_pmf(p: SimParams) -> tuple[np.ndarray, np.ndarray]:
    """Child counts n and their probabilities when durations are log-uniform and n = round(τ/Δc)."""
    dc = child_steps(p) * p.dt_s
    lo_t, hi_t = float(p.metaorder_min_duration_s), float(p.metaorder_max_duration_s)
    n = np.arange(max(1, int(round(lo_t / dc))), max(1, int(round(hi_t / dc))) + 1)
    lo = np.clip((n - 0.5) * dc, lo_t, hi_t)
    hi = np.clip((n + 0.5) * dc, lo_t, hi_t)
    lo[0] = lo_t
    hi[-1] = hi_t
    prob = np.log(hi / lo) / math.log(hi_t / lo_t) if hi_t > lo_t else np.ones(1)
    return n, prob / prob.sum()


def _draw_metaorders(rng: np.random.Generator, p: SimParams) -> tuple[np.ndarray, ...]:
    """Child trade times, signs and metaorder ids; plus per-metaorder (n_children, sign)."""
    n_meta = rng.poisson(p.metaorder_rate * p.duration_s) if p.metaorder_rate > 0 else 0
    if n_meta == 0:
        e = np.empty(0)
        return e, e, np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64), e
    lo, hi = float(p.metaorder_min_duration_s), float(p.metaorder_max_duration_s)
    tau = np.exp(rng.uniform(math.log(lo), math.log(hi), n_meta)) if hi > lo else np.full(n_meta, lo)
    dc = child_steps(p) * p.dt_s
    n_child = np.maximum(1, np.rint(tau / dc)).astype(np.int64)
    span = (n_child - 1) * dc
    keep = span < p.duration_s
    n_child, span = n_child[keep], span[keep]
    t0 = rng.uniform(0.0, 1.0, len(n_child)) * (p.duration_s - span)
    sign = rng.choice([-1.0, 1.0], len(n_child))
    meta_id = np.repeat(np.arange(len(n_child)), n_child)
    k = np.arange(meta_id.size) - np.repeat(np.cumsum(n_child) - n_child, n_child)
    times = t0[meta_id] + k * dc
    return times, sign[meta_id], meta_id, n_child, sign


def _book_state(u_bid: np.ndarray, spread_ticks: int, tick: float, depth: float) -> dict[str, np.ndarray]:
    """Displayed best levels for latent edge positions (in ticks)."""
    k = np.ceil(u_bid) - 1.0
    frac_b = u_bid - k
    u_ask = u_bid + spread_ticks
    j = np.floor(u_ask) + 1.0
    frac_a = j - u_ask
    return {
        "bid_px": k * tick,
        "bid_sz": depth * frac_b,
        "ask_px": j * tick,
        "ask_sz": depth * frac_a,
    }


def _insert_boundaries(t: np.ndarray, u: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Insert a row at every integer crossed by ``u`` between consecutive rows (same ts as the later row)."""
    fl = np.floor(u)
    c = np.diff(fl).astype(np.int64)
    counts = np.abs(c)
    total = int(counts.sum())
    if total == 0:
        return t, u
    pair = np.repeat(np.arange(1, len(u)), counts)  # later row of each pair
    start = np.repeat(np.cumsum(counts) - counts, counts)
    idx = np.arange(total) - start
    up = np.repeat(c > 0, counts)
    base = fl[pair - 1]
    vals = np.where(up, base + 1 + idx, base - idx)
    ins_cum = np.concatenate([[0], np.cumsum(counts)])  # inserted rows before row n
    out_n = len(u) + total
    pos_rows = np.arange(len(u)) + ins_cum
    pos_ins = (pair - 1) + ins_cum[pair - 1] + 1 + idx
    t_out = np.empty(out_n)
    u_out = np.empty(out_n)
    t_out[pos_rows], u_out[pos_rows] = t, u
    t_out[pos_ins], u_out[pos_ins] = t[pair], vals
    return t_out, u_out


# --------------------------------------------------------------------------------------------
# truth
# --------------------------------------------------------------------------------------------


def truth_values(p: SimParams) -> dict[str, Any]:
    """Closed-form true parameters of the model (see module docstring and docs/notes/quant.md)."""
    kappa, phi, rho, dt = p.impact_coef, p.permanent_fraction, p.resilience_rho, p.dt_s
    a = math.exp(-rho * dt)
    out: dict[str, Any] = {
        "kappa": kappa,
        "rho": rho,
        "permanent_fraction": phi,
        "half_life_s": math.log(2.0) / rho if rho > 0 else math.inf,
        "ofi_beta": p.tick_size / 4.0,
        "ofi_beta_per_share": p.tick_size / (2.0 * p.depth),
        "mean_depth": p.depth / 2.0,
        "sigma_daily": p.noise_sigma * math.sqrt(p.duration_s),
        "noise_sigma": p.noise_sigma,
        "base_price": p.base_price,
        "units": {
            "kyle_lambda": "log-return per share (bar = kyle_bar_s)",
            "kappa": "log-return per share",
            "rho": "1/s",
            "ofi_beta": "price units per unit of OFI/mean-depth",
            "sqrt_Y": "dimensionless, impact = Y*sigma_daily*sqrt(Q/volume_daily) (log units)",
        },
    }
    if p.kyle_bar_s:
        n_bar = max(1, int(round(p.kyle_bar_s / dt)))
        f = (1.0 - a**n_bar) / (n_bar * (1.0 - a)) if a < 1.0 else 1.0
        out["kyle_lambda"] = kappa * (phi + (1.0 - phi) * f)
        out["kyle_bar_s"] = p.kyle_bar_s
        out["kyle_lambda_exact"] = p.metaorder_rate == 0
    noise_vol = p.trade_rate * p.size_mean * p.duration_s
    if p.metaorder_rate > 0:
        n, prob = _metaorder_children_pmf(p)
        dc = child_steps(p) * dt
        ac = math.exp(-rho * dc)
        geo = (1.0 - ac**n) / (1.0 - ac) if ac < 1.0 else n.astype(float)
        impact_n = kappa * p.size_mean * (phi * n + (1.0 - phi) * geo)
        volume = noise_vol + p.metaorder_rate * p.duration_s * float((prob * n).sum()) * p.size_mean
        x = out["sigma_daily"] * np.sqrt(n * p.size_mean / volume)
        out["sqrt_Y"] = float((prob * impact_n / x).sum())  # matches fit_sqrt_law's 1/x^2 weighting
        out["sqrt_Y_ols"] = float((prob * impact_n * x).sum() / (prob * x * x).sum())
        out["volume_daily"] = volume
        out["metaorder_child_interval_s"] = dc
    else:
        out["sqrt_Y"] = math.nan
        out["volume_daily"] = noise_vol
    return out


# --------------------------------------------------------------------------------------------
# simulate
# --------------------------------------------------------------------------------------------


def simulate(params: SimParams) -> SimResult:
    """Run the simulator. ``truth`` has at least kyle_lambda, sqrt_Y, rho, ofi_beta, permanent_fraction."""
    p = params
    p.validate()
    rng = np.random.default_rng(p.seed)
    n_steps = int(round(p.duration_s / p.dt_s))
    dt = p.dt_s
    start = pd.Timestamp(p.start_ts) if p.start_ts else pd.Timestamp(0)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")

    # ---- order flow ----------------------------------------------------------------------
    n_noise = rng.poisson(p.trade_rate * p.duration_s)
    t_noise = rng.uniform(0.0, p.duration_s, n_noise)
    v_noise = _pareto_sizes(rng, n_noise, p.size_mean, p.size_tail)
    s_noise = rng.choice([-1.0, 1.0], n_noise)
    t_meta, s_meta, meta_id, n_child, meta_sign = _draw_metaorders(rng, p)
    t_tr = np.concatenate([t_noise, t_meta])
    v_tr = np.concatenate([v_noise, np.full(t_meta.size, p.size_mean)])
    s_tr = np.concatenate([s_noise, s_meta])
    mid_tr = np.concatenate([np.full(n_noise, -1, dtype=np.int64), meta_id])
    order = np.argsort(t_tr, kind="stable")
    t_tr, v_tr, s_tr, mid_tr = t_tr[order], v_tr[order], s_tr[order], mid_tr[order]
    step = np.minimum((t_tr / dt).astype(np.int64), n_steps - 1)

    # ---- latent log-mid ------------------------------------------------------------------
    kappa, phi = p.impact_coef, p.permanent_fraction
    jump = kappa * s_tr * v_tr
    jp = np.bincount(step, weights=phi * jump, minlength=n_steps)
    jt = np.bincount(step, weights=(1.0 - phi) * jump, minlength=n_steps)
    a = math.exp(-p.resilience_rho * dt)
    dW = rng.normal(0.0, p.noise_sigma * math.sqrt(dt), n_steps)
    dW[0] = 0.0
    W = np.cumsum(dW)
    P_after = np.cumsum(jp)
    T_after = lfilter([1.0], [1.0, -a], jt)
    P_before = np.concatenate([[0.0], P_after[:-1]])
    T_before = a * np.concatenate([[0.0], T_after[:-1]])
    x0 = W + P_before + T_before  # state at grid time t_j, before trades of step j

    cs = np.cumsum(jump)
    first_in_step = np.searchsorted(step, step, side="left")
    cs_before_step = np.where(first_in_step > 0, cs[np.maximum(first_in_step - 1, 0)], 0.0)
    x_pre = x0[step] + (cs - jump - cs_before_step)
    x_post = x_pre + jump

    # ---- quote rows ----------------------------------------------------------------------
    clock = rng.random(n_steps) < min(1.0, p.quote_rate * dt)
    clock[0] = True
    clock[np.unique(step)] = True
    grid_steps = np.flatnonzero(clock)
    q_t = np.concatenate([grid_steps * dt, t_tr])
    q_x = np.concatenate([x0[grid_steps], x_post])
    q_kind = np.concatenate([np.zeros(grid_steps.size), np.ones(t_tr.size)])
    qo = np.lexsort((q_kind, q_t))
    q_t, q_x = q_t[qo], q_x[qo]
    half = p.spread_ticks / 2.0
    u = p.base_price * np.exp(q_x) / p.tick_size - half
    q_t, u = _insert_boundaries(q_t, u)
    book = _book_state(u, int(p.spread_ticks), p.tick_size, p.depth)
    q_ts = start + pd.to_timedelta(np.rint(q_t * 1e9).astype(np.int64), unit="ns")
    quotes = pd.DataFrame({"ts": q_ts, "symbol": p.symbol, "source": SOURCE, "venue": SOURCE, **book})
    quotes = schema.coerce(quotes, "quotes")

    # ---- trades --------------------------------------------------------------------------
    u_pre = p.base_price * np.exp(x_pre) / p.tick_size - half
    pre = _book_state(u_pre, int(p.spread_ticks), p.tick_size, p.depth)
    price = np.where(s_tr > 0, pre["ask_px"], pre["bid_px"])
    t_ts = start + pd.to_timedelta(np.rint(t_tr * 1e9).astype(np.int64), unit="ns")
    trades = pd.DataFrame(
        {
            "ts": t_ts,
            "symbol": p.symbol,
            "source": SOURCE,
            "venue": SOURCE,
            "price": price,
            "size": v_tr,
            "side": s_tr.astype(np.int8),
            "trade_id": pd.Series(np.arange(t_tr.size)).astype(str).to_numpy(),
        }
    )
    trades = schema.coerce(trades, "trades")

    # ---- true metaorders -----------------------------------------------------------------
    metaorders = None
    if n_child.size:
        is_child = mid_tr >= 0
        ids = mid_tr[is_child]
        idx = np.flatnonzero(is_child)
        first = pd.Series(idx).groupby(ids).min().reindex(range(n_child.size)).to_numpy()
        last = pd.Series(idx).groupby(ids).max().reindex(range(n_child.size)).to_numpy()
        p_start = p.base_price * np.exp(x_pre[first])
        p_end = p.base_price * np.exp(x_post[last])
        metaorders = pd.DataFrame(
            {
                "start": t_ts[first],
                "end": t_ts[last],
                "sign": meta_sign.astype(np.int8),
                "Q": n_child * p.size_mean,
                "n_trades": n_child,
                "p_start": p_start,
                "p_end": p_end,
                "impact": meta_sign * np.log(p_end / p_start),
            }
        ).sort_values("start", ignore_index=True)

    truth = truth_values(p)
    truth["n_trades"] = int(t_tr.size)
    truth["n_metaorders"] = int(n_child.size)
    return SimResult(trades=trades, quotes=quotes, l2=None, truth=truth, metaorders=metaorders)
