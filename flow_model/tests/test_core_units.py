"""Unit tests for flow_model/core on small hand-built inputs (exact expected values)."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from flow_model.core import execution as ex
from flow_model.core import impact, ofi, resilience, seasonality, signing
from flow_model.core.metrics import FitResult, floor_ns, json_safe, ols_fit, train_test_split_by_date, ts_ns
from flow_model.data import schema
from flow_model.settings import load_config

CFG = load_config("core")
T0 = pd.Timestamp("2026-01-05 14:30:00", tz="UTC")


def trades_frame(prices, sides, sizes=None, secs=None) -> pd.DataFrame:
    n = len(prices)
    secs = secs if secs is not None else list(range(n))
    return schema.coerce(pd.DataFrame({
        "ts": [T0 + pd.Timedelta(seconds=s) for s in secs], "symbol": "X", "source": "t", "venue": "t",
        "price": prices, "size": sizes or [1.0] * n, "side": sides,
    }), "trades")


def quotes_frame(rows, secs=None) -> pd.DataFrame:
    secs = secs if secs is not None else list(range(len(rows)))
    bp, bs, ap, as_ = zip(*rows)
    return schema.coerce(pd.DataFrame({
        "ts": [T0 + pd.Timedelta(seconds=s) for s in secs], "symbol": "X", "source": "t", "venue": "t",
        "bid_px": bp, "bid_sz": bs, "ask_px": ap, "ask_sz": as_,
    }), "quotes")


# ---------------- metrics ----------------
def test_ols_recovers_coefficients_and_hac():
    rng = np.random.default_rng(0)
    x = rng.normal(size=5000)
    y = 2.0 + 3.0 * x + rng.normal(scale=0.1, size=5000)
    f = ols_fit(y, pd.DataFrame({"x": x}), hac_lags=5)
    assert f.params["x"] == pytest.approx(3.0, abs=0.01)
    assert f.params["const"] == pytest.approx(2.0, abs=0.01)
    assert f.r2 > 0.99 and f.n == 5000 and "HAC" in f.extra["se_type"]


def test_ols_drops_nan_and_fails_cleanly():
    f = ols_fit(np.array([1.0, np.nan]), np.array([1.0, 2.0]))
    assert f.params == {} and "error" in f.extra


def test_fitresult_to_dict_is_strict_json():
    f = FitResult("x", {"a": np.float64(np.nan), "b": np.int64(3)}, extra={"arr": np.array([np.inf, 1.0])})
    d = f.to_dict()
    json.dumps(d, allow_nan=False)
    assert d["params"] == {"a": None, "b": 3} and d["extra"]["arr"] == [None, 1.0]


def test_split_by_whole_dates():
    ts = pd.date_range("2026-01-01", periods=5 * 24, freq="1h", tz="UTC")
    df = pd.DataFrame({"ts": ts, "v": range(len(ts))})
    tr, te, span = train_test_split_by_date(df, 0.6)
    assert span == {"train": ["2026-01-01", "2026-01-03"], "test": ["2026-01-04", "2026-01-05"]}
    assert tr["ts"].max() < te["ts"].min() and len(tr) + len(te) == len(df)
    one = df[df["ts"] < "2026-01-02"]
    tr1, te1, span1 = train_test_split_by_date(one, 0.6)
    assert len(te1) == 0 and span1["test"] == []


def test_time_helpers_match_pandas():
    ts = pd.Series(pd.date_range("2026-01-01", periods=10, freq="777ms", tz="UTC"))
    assert (floor_ns(ts, "1s") == ts_ns(ts.dt.floor("1s"))).all()


# ---------------- signing ----------------
def test_tick_rule_and_zero_ticks():
    t = trades_frame([10, 11, 11, 10, 10, 12], [1] * 6)
    assert signing.tick_rule(t).tolist() == [0, 1, 1, -1, -1, 1]


def test_lee_ready_uses_prior_quote_and_tick_at_mid():
    q = quotes_frame([(9.0, 1, 11.0, 1), (10.0, 1, 12.0, 1)], secs=[0, 2])
    # trade at t=2 must use the quote at t=0 (strictly before): mid 10
    t = trades_frame([10.5, 11.0, 9.5, 10.0], [0] * 4, secs=[1, 2, 2.5, 3])
    out = signing.lee_ready(t, q, pd.Timedelta(0)).tolist()
    assert out[:3] == [1, 1, -1]
    assert out[3] == -1  # 10.0 < mid 11 at t=3
    exact = signing.lee_ready(t, q, pd.Timedelta(0), allow_exact=True).tolist()
    # with the t=2 quote (mid 11) the 11.0 print is at the mid -> tick rule: 10.5 -> 11.0 is an uptick
    assert exact[1] == 1


def test_sign_trades_auto_fills_unknown():
    t = trades_frame([10, 11, 12], [1, 0, -1])
    assert signing.sign_trades(t).tolist() == [1, 1, -1]


def test_signing_accuracy_excludes_unknown_and_weights():
    true = pd.Series([1, -1, 1, 0])
    inf = pd.Series([1, 1, 1, 1])
    a = signing.signing_accuracy(true, inf, sizes=pd.Series([1.0, 3.0, 1.0, 9.0]))
    assert a["n"] == 3 and a["accuracy"] == pytest.approx(2 / 3)
    assert a["accuracy_volume_weighted"] == pytest.approx(2 / 5)
    assert a["confusion"]["-1,1"] == 1


@pytest.mark.parametrize("dist,dof", [("t", CFG["signing"]["bvc_dof"]), ("normal", None)])
def test_bvc_splits_volume(dist, dof):
    close = np.array([100, 101, 100.5, 100.5, 102, 101])
    bars = pd.DataFrame({"close": close, "volume": np.full(6, 10.0)})
    b = signing.bulk_volume_classification(bars, window=3, dist=dist, dof=dof)
    assert np.allclose(b["buy_volume"] + b["sell_volume"], 10.0)
    assert b["buy_volume"].iloc[0] == 5.0
    assert b["signed_volume"].iloc[4] > 0 and b["signed_volume"].iloc[5] < 0


# ---------------- OFI ----------------
def test_event_ofi_matches_cks_cases():
    q = quotes_frame([
        (10.0, 5, 10.2, 7),   # start
        (10.0, 8, 10.2, 7),   # bid size +3          -> +3
        (10.1, 2, 10.2, 7),   # bid price up, new q  -> +2
        (10.1, 2, 10.2, 4),   # ask size -3          -> +3
        (10.1, 2, 10.3, 6),   # ask price up         -> +qa_prev = +4
        (10.0, 9, 10.3, 6),   # bid price down       -> -qb_prev = -2
        (10.0, 9, 10.2, 1),   # ask price down       -> -qa_new = -1
    ])
    assert ofi.event_ofi(q).tolist() == [0, 3, 2, 3, 4, -2, -1]


def test_aggregate_ofi_windows_and_dmid():
    q = quotes_frame([(10.0, 5, 10.2, 5), (10.0, 6, 10.2, 5), (10.1, 1, 10.3, 5), (10.1, 1, 10.3, 5)],
                     secs=[0, 0.5, 1.2, 3.1])
    a = ofi.aggregate_ofi(q, "1s")
    assert a["n_events"].tolist() == [2, 1, 1]
    assert a["ofi"].tolist() == [1, 1 + 5, 0]
    assert np.isnan(a["dmid"].iloc[0]) and a["dmid"].iloc[1] == pytest.approx(0.1)


# ---------------- impact ----------------
def test_build_metaorders_runs_and_gaps():
    t = trades_frame([10, 10.1, 10.2, 10.3, 10, 9.9, 9.8, 10, 10.1, 10.2],
                     [1, 1, 1, 1, -1, -1, -1, 1, 1, 1],
                     sizes=[1, 2, 3, 4, 1, 1, 1, 1, 1, 1],
                     secs=[0, 1, 2, 3, 4, 5, 6, 7, 8, 30])
    m = impact.build_metaorders(t, pd.Timedelta("5s"), min_trades=3)
    assert m["n_trades"].tolist() == [4, 3]
    assert m["Q"].tolist() == [10, 3]
    assert m["sign"].tolist() == [1, -1]
    assert m["impact"].iloc[0] == pytest.approx(math.log(10.3 / 10))
    assert m["impact"].iloc[1] == pytest.approx(-math.log(9.8 / 10))


def test_sqrt_law_exact_on_noise_free_data():
    Q = np.geomspace(10, 1e4, 50)
    meta = pd.DataFrame({"Q": Q, "impact": 0.7 * 0.02 * np.sqrt(Q / 1e6)})
    f = impact.fit_sqrt_law(meta, 0.02, 1e6)
    assert f.params["Y"] == pytest.approx(0.7) and f.extra["delta"] == pytest.approx(0.5, abs=1e-6)
    curve = impact.impact_curve(meta, 0.02, 1e6, 5, f.params["Y"])
    assert np.allclose(curve["fitted"], curve["impact_norm"], rtol=0.05)


def test_kyle_and_amihud():
    idx = pd.date_range(T0, periods=600, freq="1min")
    rng = np.random.default_rng(1)
    sv = pd.Series(rng.normal(size=600), index=idx)
    r = 1e-4 * sv + pd.Series(rng.normal(scale=1e-6, size=600), index=idx)
    assert impact.kyle_lambda(r, sv).params["lambda"] == pytest.approx(1e-4, rel=1e-2)
    dv = pd.Series(1e6, index=idx)
    a = impact.amihud(r, dv, "1h")
    assert a.n == 11 and a.params["illiq"] > 0  # 600 min from 14:30 touch 11 clock hours


def test_bar_flow_fills_empty_bars():
    t = trades_frame([10, 10.1, 10.2], [1, 1, -1], sizes=[1, 2, 3], secs=[0, 5, 185])
    bf = impact.bar_flow(t, "1min")
    assert len(bf) == 4 and bf["signed_volume"].tolist() == [3, 0, 0, -3]
    assert bf["ret"].iloc[3] == pytest.approx(math.log(10.2 / 10.1))


# ---------------- resilience ----------------
def _decay(rho=0.05, perm=1e-4, trans=3e-4):
    lags = np.array([0.0] + CFG["resilience"]["horizons_s"], dtype=float)
    y = perm + trans * np.exp(-rho * lags)
    y[0] = 0.0
    return pd.DataFrame({"lag_s": lags, "price_reversion": y, "spread": 0.0, "depth": 1.0,
                         "n": 100, "price_reversion_se": 1e-6})


def test_exponential_fit_exact_and_aic_prefers_it():
    d = _decay()
    e, p = resilience.fit_exponential(d), resilience.fit_power_law(d)
    assert e.params["rho"] == pytest.approx(0.05, rel=1e-4)
    assert e.extra["permanent_fraction"] == pytest.approx(0.25, rel=1e-4)
    assert e.extra["half_life_s"] == pytest.approx(math.log(2) / 0.05, rel=1e-4)
    assert resilience.compare_resilience(e, p)["preferred"] == "exponential"
    curves = resilience.fitted_curves(d, e, p)
    assert {"exp_fit", "pl_fit"} <= set(curves.columns)


def test_power_law_fit_and_aic_prefers_it():
    d = _decay()
    t = d["lag_s"].to_numpy()
    d["price_reversion"] = np.where(t > 0, 1e-4 + 3e-4 * np.where(t > 0, t, 1) ** -0.4, 0.0)
    e, p = resilience.fit_exponential(d), resilience.fit_power_law(d)
    assert p.params["beta"] == pytest.approx(0.4, rel=1e-3)
    assert resilience.compare_resilience(e, p)["preferred"] == "power_law"


def test_event_study_and_large_trades():
    q = quotes_frame([(9.9, 5, 10.1, 5), (10.0, 5, 10.2, 5), (9.95, 5, 10.15, 5)], secs=[0, 1, 5])
    t = trades_frame([10.1, 10.1], [1, 1], sizes=[1, 100], secs=[0.5, 1])
    ev = resilience.large_trade_events(t, 0.5)
    assert len(ev) == 1 and ev["size"].iloc[0] == 100
    d = resilience.event_study(ev, t, q, [pd.Timedelta(seconds=1), pd.Timedelta(seconds=10)])
    assert d["lag_s"].tolist() == [0.0, 1.0, 10.0]
    assert d["price_reversion"].iloc[1] == pytest.approx(math.log(10.1 / 10.0))  # pre-trade mid 10.0 at t=0
    assert np.isnan(d["price_reversion"].iloc[2])  # beyond the last quote


def test_propagator_recovers_simple_kernel():
    rng = np.random.default_rng(3)
    n, g = 50_000, np.array([1e-4, -3e-5, -2e-5])
    eps = rng.choice([-1.0, 1.0], n)
    r = np.convolve(eps, g)[:n] + rng.normal(scale=1e-6, size=n)
    lp = np.concatenate([[0.0], np.cumsum(r)])
    prices = 100 * np.exp(lp[:n])
    # trade n's return r_n is lp[n+1]-lp[n]
    t = trades_frame(list(prices), list(eps.astype(int)), secs=list(np.arange(n) * 0.01))
    f = resilience.fit_propagator(t, max_lag=10)
    G = np.array(f.extra["G"])
    assert G[0] == pytest.approx(g[0], rel=0.05)
    assert G[2] == pytest.approx(g.sum(), rel=0.1)


# ---------------- execution ----------------
def test_schedules_sum_to_Q_and_vwap_follows_profile():
    m = ex.ImpactModel(1e-4, 5e-4, 0.01, None, 0.01, 0.0, price=100.0)
    for s in (ex.twap(1000, 600, 10), ex.vwap(1000, 600, 10, np.arange(1, 11)),
              ex.almgren_chriss(1000, 600, 10, m, 1e-3), ex.obizhaeva_wang(1000, 600, 10, m)):
        assert s.trades.sum() == pytest.approx(1000) and len(s.times) == 10
    v = ex.vwap(1000, 600, 10, np.arange(1, 11))
    assert v.trades[-1] == pytest.approx(10 * v.trades[0])


def test_ow_is_optimal_under_its_cost():
    m = ex.ImpactModel(1e-4, 5e-4, 0.01, None, 0.01, 0.0, price=100.0)
    ow = ex.obizhaeva_wang(1000, 600, 20, m)
    base = ex.expected_cost(ow, m)
    rng = np.random.default_rng(0)
    for _ in range(50):
        d = rng.normal(size=20)
        d -= d.mean()
        assert ex.expected_cost(ex.Schedule(ow.times, ow.trades + d, "p"), m) >= base
    assert ow.trades[0] > ow.trades[5] and ow.trades[-1] > ow.trades[5]  # blocks at the ends


def test_almgren_chriss_front_loads_with_risk_aversion():
    m = ex.ImpactModel(1e-4, 5e-4, None, None, 0.05, 0.0, price=100.0)
    ac = ex.almgren_chriss(1000, 600, 20, m, 1e-2)
    assert np.all(np.diff(ac.trades) < 0)
    assert np.allclose(ex.almgren_chriss(1000, 600, 20, m, 0.0).trades, 50.0)


def test_expected_cost_closed_form():
    m = ex.ImpactModel(1e-4, 5e-4, None, None, 0.0, 0.02, price=100.0)
    s = ex.twap(100, 10, 4)
    # spread/2*Q + perm*Q^2/2 + eta*sum(x^2)/2
    assert ex.expected_cost(s, m) == pytest.approx(0.01 * 100 + 1e-4 * 100**2 / 2 + 5e-4 * 4 * 25**2 / 2)


def test_impact_model_from_fits_round_trip():
    kappa, phi, rho, price = 1e-5, 0.3, 0.05, 100.0
    bar = pd.Timedelta(CFG["impact"]["kyle_bar"]).total_seconds()
    lam = kappa * (phi + (1 - phi) * (1 - math.exp(-rho * bar)) / (rho * bar))
    fits = {"methods": {
        "kyle_lambda": {"status": "ok", "in_sample": {"params": {"lambda": lam}}},
        "resilience": {"status": "ok", "in_sample": {"exponential": {
            "params": {"rho": rho}, "extra": {"permanent_fraction": phi}}}},
    }}
    m = ex.ImpactModel.from_fits(fits, CFG, price=price, sigma=0.02, spread=0.01)
    assert m.lambda_perm == pytest.approx(phi * kappa * price)
    assert m.eta_temp == pytest.approx((1 - phi) * kappa * price)
    fits["methods"]["resilience"] = {"status": "not_applicable", "reason": "tier C"}
    c = ex.ImpactModel.from_fits(fits, CFG, price=price)
    assert c.rho is None and c.lambda_perm == pytest.approx(lam * price)
    tab = ex.execution_table(m, 1e6, CFG)
    e = CFG["execution"]
    assert len(tab) == len(e["order_sizes_pct_adv"]) * 3 * e["n_steps"]


# ---------------- seasonality ----------------
def test_intraday_profile_session_and_deseasonalize():
    ts = pd.date_range("2026-01-05 13:00", "2026-01-07 23:00", freq="5min", tz="UTC")
    tod = ts.tz_convert("America/New_York").hour
    df = pd.DataFrame({"ts": ts, "v": np.where(tod < 12, 2.0, 1.0)})
    s = CFG["seasonality"]
    prof = seasonality.intraday_profile(df, "v", s["bucket"], tz=s["equities_session_tz"],
                                        session=tuple(s["equities_session"]))
    assert prof["bucket"].iloc[0] == "09:30" and prof["bucket"].iloc[-1] == "15:30"
    assert len(prof) == 13
    assert prof.set_index("bucket").loc["10:00", "mean"] == 2.0
    d = seasonality.deseasonalize(df, prof, "v", s["bucket"], tz=s["equities_session_tz"])
    inside = seasonality._in_session(df["ts"], s["equities_session_tz"], tuple(s["equities_session"]))
    assert np.allclose(d[inside], 1.0)


def test_json_safe_handles_timestamps():
    assert json_safe({"t": pd.Timestamp("2026-01-01", tz="UTC"), "x": float("inf")}) == {
        "t": "2026-01-01T00:00:00+00:00", "x": None}
