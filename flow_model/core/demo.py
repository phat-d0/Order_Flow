"""End-to-end synthetic run: simulate, fit every method in-sample / out-of-sample, write §3 results.

Serves three purposes: the quality-gate test of ``write_results``/``validate_results``, the
true-vs-fitted table in the Quant report, and a worked reference for asset agents on how to
fill ``fits.json`` (``python -m flow_model.core.demo [out_dir]``).

Owned by the Quant agent. Parameters come from config/core.yaml.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from flow_model.core import execution as ex
from flow_model.core import impact, ofi, resilience, seasonality, signing
from flow_model.core.metrics import FitResult, json_safe, train_test_split_by_date, ts_ns
from flow_model.core.results import METHODS, method_entry, write_results
from flow_model.core.simulator import SimParams, SimResult, simulate
from flow_model.settings import load_config


def _date_mask(ts: pd.Series, span: list[str]) -> np.ndarray:
    if not span:
        return np.zeros(len(ts), dtype=bool)
    t = ts_ns(ts)
    lo = pd.Timestamp(span[0], tz="UTC").value
    hi = (pd.Timestamp(span[1], tz="UTC") + pd.Timedelta(days=1)).value
    return (t >= lo) & (t < hi)


def _fit_sample(trades: pd.DataFrame, quotes: pd.DataFrame, meta: pd.DataFrame | None,
                truth: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """All fits on one sample (IS or OOS)."""
    sc, ic, rc = cfg["signing"], cfg["impact"], cfg["resilience"]
    lag = pd.Timedelta(milliseconds=sc["lee_ready_quote_lag_ms"])
    out: dict[str, Any] = {}
    out["signing"] = {
        "tick_rule": signing.signing_accuracy(trades["side"], signing.tick_rule(trades), sizes=trades["size"]),
        "lee_ready": signing.signing_accuracy(trades["side"], signing.lee_ready(trades, quotes, lag),
                                              sizes=trades["size"]),
    }
    aggs = {w: ofi.aggregate_ofi(quotes, w) for w in cfg["ofi"]["windows"]}
    out["ofi"] = [ofi.fit_ofi(a) for a in aggs.values()]
    for f, w in zip(out["ofi"], aggs):
        f.name, f.extra["window"] = f"ofi_{w}", w
    out["_ofi_aggs"] = aggs
    if meta is not None and len(meta) >= 3:
        out["sqrt_law"] = impact.fit_sqrt_law(meta, truth["sigma_daily"], truth["volume_daily"])
    else:
        out["sqrt_law"] = FitResult.failed("sqrt_law", "fewer than 3 metaorders")
    bf = impact.bar_flow(trades, ic["kyle_bar"], quotes=quotes)
    out["_bars"] = bf
    out["kyle_lambda"] = impact.kyle_lambda(bf["ret"], bf["signed_volume"], hac_lags=ic["hac_lags"])
    out["amihud"] = impact.amihud(bf["ret"], bf["dollar_volume"], ic["amihud_freq"])
    horizons = [pd.Timedelta(seconds=h) for h in rc["horizons_s"]]
    ev = resilience.large_trade_events(trades, rc["large_trade_quantile"])
    decay = resilience.event_study(ev, trades, quotes, horizons)
    e_fit, p_fit = resilience.fit_exponential(decay), resilience.fit_power_law(decay)
    out["resilience"] = {
        "exponential": e_fit, "power_law": p_fit,
        "propagator": resilience.fit_propagator(trades, rc["propagator_max_lag"], quotes=quotes),
        "comparison": resilience.compare_resilience(e_fit, p_fit),
    }
    out["_decay"] = resilience.fitted_curves(decay, e_fit, p_fit)
    return out


def _tables(fits_is: dict, fits_oos: dict | None, meta_is, meta_oos, truth: dict, cfg: dict) -> dict[str, pd.DataFrame]:
    tabs: dict[str, list[pd.DataFrame]] = {"impact_curve": [], "ofi_bins": [], "resilience_decay": []}
    Y = fits_is["sqrt_law"].params.get("Y")
    samples = [("IS", fits_is, meta_is)] + ([("OOS", fits_oos, meta_oos)] if fits_oos else [])
    for sample, f, meta in samples:
        if meta is not None and len(meta):
            c = impact.impact_curve(meta, truth["sigma_daily"], truth["volume_daily"], cfg["impact"]["n_bins"], Y)
            tabs["impact_curve"].append(c.assign(sample=sample))
        for w, agg in f["_ofi_aggs"].items():
            tabs["ofi_bins"].append(ofi.ofi_bins(agg, cfg["ofi"]["n_bins"], w).assign(sample=sample))
        tabs["resilience_decay"].append(f["_decay"].assign(sample=sample))
    return {k: pd.concat(v, ignore_index=True) for k, v in tabs.items() if v}


def _intraday(trades: pd.DataFrame, quotes: pd.DataFrame, bars: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    b = cfg["seasonality"]["bucket"]
    bars = bars.reset_index().assign(ret2=lambda d: d["ret"] ** 2)
    q = pd.DataFrame({"ts": quotes["ts"], "spread": (quotes["ask_px"] - quotes["bid_px"])
                      / (0.5 * (quotes["ask_px"] + quotes["bid_px"]))})
    vol = seasonality.intraday_profile(bars, "volume", b)
    sp = seasonality.intraday_profile(q, "spread", b)
    rv = seasonality.intraday_profile(bars, "ret2", b)
    out = vol[["bucket", "mean", "n"]].rename(columns={"mean": "volume"})
    out = out.merge(sp[["bucket", "mean"]].rename(columns={"mean": "spread"}), on="bucket", how="left")
    out = out.merge(rv[["bucket", "mean"]].rename(columns={"mean": "volatility"}), on="bucket", how="left")
    out["volatility"] = np.sqrt(out["volatility"])
    return out[["bucket", "volume", "spread", "volatility", "n"]]


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def run_demo(cfg: dict[str, Any] | None = None, out_dir: Path | None = None,
             **sim_overrides: Any) -> tuple[dict[str, Any], dict[str, pd.DataFrame], pd.DataFrame, SimResult]:
    """Simulate, fit on the first ``split.train_frac`` of dates and test on the rest.

    Returns ``(fits, tables, truth_vs_fit, sim)``; writes results to ``out_dir`` if given.
    ``sim_overrides`` go to ``SimParams.from_config`` (e.g. ``duration_s`` to span several dates).
    """
    cfg = cfg or load_config("core")
    sim = simulate(SimParams.from_config(cfg, **sim_overrides))
    truth = sim.truth
    tr, te, split = train_test_split_by_date(sim.trades, cfg["split"]["train_frac"],
                                             min_test_days=cfg["split"]["min_test_days"])
    q_is, q_oos = (sim.quotes.loc[_date_mask(sim.quotes["ts"], split[k])] for k in ("train", "test"))
    meta = sim.metaorders
    m_is = meta.loc[_date_mask(meta["start"], split["train"])] if meta is not None else None
    m_oos = meta.loc[_date_mask(meta["start"], split["test"])] if meta is not None else None
    f_is = _fit_sample(tr, q_is, m_is, truth, cfg)
    f_oos = _fit_sample(te, q_oos, m_oos, truth, cfg) if len(te) else None

    def entry(name: str) -> dict[str, Any]:
        oos = f_oos[name] if f_oos else None
        fit = f_is[name]
        if isinstance(fit, FitResult) and "error" in fit.extra:
            return method_entry("failed", fit.extra["error"], fit, oos)
        return method_entry("ok", None, fit, oos)

    methods = {m: entry(m) for m in ("signing", "ofi", "sqrt_law", "kyle_lambda", "amihud", "resilience")}
    price = float(sim.trades["price"].mean())
    market = {"price": price, "sigma": truth["noise_sigma"] * price,
              "spread": float((q_is["ask_px"] - q_is["bid_px"]).mean())}
    model = ex.ImpactModel.from_fits({"methods": json_safe(methods), "market": market}, cfg)
    adv = truth["volume_daily"]
    exec_tab = ex.execution_table(model, adv, cfg, volume_profile=None)
    summary = exec_tab.drop_duplicates(["order_size_pct_adv", "schedule"])[
        ["order_size_pct_adv", "schedule", "cost_bps", "savings_vs_twap_bps"]]
    methods["execution"] = method_entry("ok", None, {"rows": summary.to_dict("records"),
                                                     "model": vars(model)}, {})
    intraday = _intraday(sim.trades, sim.quotes, impact.bar_flow(sim.trades, "1min", sim.quotes), cfg)
    methods["seasonality"] = method_entry("ok", None, {"rows": intraday.to_dict("records")}, {})
    assert set(methods) == set(METHODS)
    fits = {
        "symbol": sim.trades["symbol"].iloc[0], "asset_class": "synthetic", "tier": "A",
        "sources": [{"source": "sim", "venue": "sim", "kinds": ["trades", "quotes"], "ts_precision": "ns"}],
        "caveats": ["Synthetic data from flow_model.core.simulator; sqrt_law uses the simulator's true "
                    "metaorder labels and true sigma/volume."],
        "data": {"start": sim.trades["ts"].min().isoformat(), "end": sim.trades["ts"].max().isoformat(),
                 "n_trades": len(sim.trades), "n_quotes": len(sim.quotes), "n_bars": 0, "gaps": []},
        "split": split, "methods": methods, "truth": _strip(truth), "config": cfg,
    }
    tables = _tables(f_is, f_oos, m_is, m_oos, truth, cfg)
    tables["intraday_profile"] = intraday
    tables["execution"] = exec_tab
    tvf = truth_vs_fit(truth, f_is, f_oos)
    if out_dir is not None:
        write_results(Path(out_dir), fits, tables)
    return fits, tables, tvf, sim


def truth_vs_fit(truth: dict, f_is: dict, f_oos: dict | None) -> pd.DataFrame:
    """Rows ``param, true, in_sample, out_of_sample`` for the report."""
    def get(f: dict | None, path: tuple) -> float:
        if f is None:
            return float("nan")
        obj: Any = f
        for p in path:
            if isinstance(obj, FitResult):
                obj = obj.params if p == "params" else obj.extra if p == "extra" else getattr(obj, p)
            elif isinstance(obj, (list, tuple)):
                obj = obj[p]
            else:
                obj = obj.get(p) if isinstance(obj, dict) else None
            if obj is None:
                return float("nan")
        return float(obj)

    rows = [
        ("kyle_lambda", "kyle_lambda", ("kyle_lambda", "params", "lambda")),
        ("sqrt_Y", "sqrt_Y", ("sqrt_law", "params", "Y")),
        ("rho", "rho", ("resilience", "exponential", "params", "rho")),
        ("permanent_fraction", "permanent_fraction", ("resilience", "exponential", "extra", "permanent_fraction")),
    ]
    rows += [(f"ofi_beta_{w}", "ofi_beta", ("ofi", i, "params", "beta"))
             for i, w in enumerate(f_is["_ofi_aggs"])]
    return pd.DataFrame([{"param": name, "true": truth.get(tk, float("nan")), "in_sample": get(f_is, path),
                          "out_of_sample": get(f_oos, path)} for name, tk, path in rows])


if __name__ == "__main__":  # pragma: no cover - manual demo
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    overrides = {"duration_s": float(sys.argv[2])} if len(sys.argv) > 2 else {}
    _, _, table, _ = run_demo(out_dir=target, **overrides)
    print(table.to_string(index=False))
