"""Bar appender (idempotency, volume protection, failures) and roll detection. No network."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from flow_model.assets.commodities import rolls
from flow_model.data import storage
from flow_model.data.loaders import yfinance_bars as yb
from flow_model.data.recorders import bar_appender as ba
from flow_model.settings import load_config

CFG = load_config("sources")["yfinance"]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = pd.Timestamp(now)

    def __call__(self) -> datetime:
        return self.now.to_pydatetime()


def scripted_client(volume_zero_at: set[pd.Timestamp] | None = None, fail: set[str] | None = None):  # noqa: ANN201
    """Deterministic prices (function of ts); volume 0 on the first row and at ``volume_zero_at``."""
    zero = volume_zero_at or set()

    def client(symbol, start, end, interval, timeout_s, prepost):  # noqa: ANN001, ANN202
        if symbol in (fail or set()):
            raise yb.Rejected("boom")
        idx = pd.date_range(pd.Timestamp(start).ceil("min"), end, freq="1min", inclusive="left")
        minute = (idx.asi8 // 60_000_000_000) % 1000
        close = 80 + minute / 1000
        vol = np.where(idx.isin(list(zero)), 0.0, 10.0 + minute % 7)
        frame = pd.DataFrame({"Open": close, "High": close + 0.01, "Low": close - 0.01, "Close": close,
                              "Volume": vol}, index=idx.tz_convert("America/New_York"))
        if len(frame):
            frame.iloc[0, frame.columns.get_loc("Volume")] = 0.0
        return frame

    return client


def make_loader(clock: Clock, client) -> yb.YFinanceBarsLoader:  # noqa: ANN001
    return yb.YFinanceBarsLoader(CFG, client=client, sleep=lambda s: None, now=clock)


def test_tier_symbols_cover_tier_c() -> None:
    assert set(ba.tier_symbols("C", "yfinance")) == {"CL=F", "GC=F", "6E=F"}


def test_appender_first_run_then_idempotent(tmp_path) -> None:  # noqa: ANN001
    clock = Clock(datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc))
    loader = make_loader(clock, scripted_client())
    first = ba.run(["CL=F"], root=tmp_path, log_dir=tmp_path / "logs", loader=loader, clock=clock)
    res = first["symbols"][0]
    assert first["ok"] and res["added"] > 0 and res["revised"] == 0
    assert res["added"] == res["stored_total"]
    # bars that are not yet complete (ts + 1m + settle lag > now) are skipped
    assert pd.Timestamp(res["last_ts"]) <= clock.now - pd.Timedelta(minutes=1) - pd.Timedelta(seconds=CFG["appender"]["settle_lag_s"])
    assert json.loads((tmp_path / "logs").glob("run_*.json").__next__().read_text())["symbols"][0]["symbol"] == "CL=F"
    second = ba.run(["CL=F"], root=tmp_path, loader=loader, clock=clock)
    assert second["symbols"][0]["added"] == 0 and second["symbols"][0]["revised"] == 0
    clock.now += pd.Timedelta(minutes=30)
    third = ba.run(["CL=F"], root=tmp_path, loader=loader, clock=clock)
    assert third["symbols"][0]["added"] == 30
    stored = storage.read_range("yfinance", "CL=F", "bars", root=tmp_path)
    assert stored["ts"].is_unique and len(stored) == third["symbols"][0]["stored_total"]


def test_refetched_zero_volume_does_not_erase_stored_volume(tmp_path) -> None:  # noqa: ANN001
    clock = Clock(datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc))
    ba.run(["GC=F"], root=tmp_path, loader=make_loader(clock, scripted_client()), clock=clock)
    target = pd.Timestamp("2026-11-25 11:30", tz="UTC")  # inside the next run's overlap
    before = storage.read_range("yfinance", "GC=F", "bars", root=tmp_path).set_index("ts")["volume"][target]
    assert before > 0
    clock.now += pd.Timedelta(minutes=10)
    out = ba.run(["GC=F"], root=tmp_path, loader=make_loader(clock, scripted_client({target})), clock=clock)
    assert out["symbols"][0]["zero_volume_kept_stored"] == 1
    after = storage.read_range("yfinance", "GC=F", "bars", root=tmp_path).set_index("ts")["volume"][target]
    assert after == before


def test_one_symbol_failing_does_not_stop_others(tmp_path) -> None:  # noqa: ANN001
    clock = Clock(datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc))
    out = ba.run(["CL=F", "GC=F"], root=tmp_path, loader=make_loader(clock, scripted_client(fail={"CL=F"})), clock=clock)
    status = {r["symbol"]: r["status"] for r in out["symbols"]}
    assert status == {"CL=F": "failed", "GC=F": "ok"} and not out["ok"]
    assert "rejected" in out["symbols"][0]["error"]


def test_dry_run_writes_nothing(tmp_path) -> None:  # noqa: ANN001
    clock = Clock(datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc))
    out = ba.run(["6E=F"], root=tmp_path, loader=make_loader(clock, scripted_client()), dry_run=True, clock=clock)
    assert out["symbols"][0]["added"] > 0 and not list(tmp_path.rglob("*.parquet"))


def test_merge_reports_revisions() -> None:
    ts = pd.date_range("2026-11-24 14:00", periods=3, freq="1min", tz="UTC")
    stored = pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 5.0})
    new = stored.iloc[1:].copy()
    new.loc[new.index[0], "close"] = 1.0
    new.loc[new.index[0], "high"] = 2.0
    new = pd.concat([new, pd.DataFrame({"ts": [ts[-1] + pd.Timedelta(minutes=1)], "open": 1.0, "high": 1.0,
                                        "low": 1.0, "close": 1.0, "volume": 3.0})], ignore_index=True)
    merged, added, revised, kept = ba._merge(stored, new)
    assert (added, revised, kept) == (1, 1, 0) and merged["high"].iloc[1] == 2.0


# ---------------------------------------------------------------- rolls

def test_calendar_rules_known_dates() -> None:
    cl = rolls.calendar_roll_dates({"rule": "nymex_cl"}, "2026-11-01", "2026-11-30")
    assert cl == [pd.Timestamp("2026-11-20")]            # CLF27 expiry: 3 bdays before Wed 25th
    gc = rolls.calendar_roll_dates({"rule": "last_bday_before_delivery", "delivery_months": [2, 4, 6, 8, 10, 12]},
                                   "2026-09-01", "2027-01-31")
    assert gc == [pd.Timestamp("2026-09-30"), pd.Timestamp("2026-11-30"), pd.Timestamp("2027-01-29")]
    imm = rolls.calendar_roll_dates({"rule": "imm_quarterly", "months": [3, 6, 9, 12]}, "2026-01-01", "2026-12-31")
    assert imm[0] == pd.Timestamp("2026-03-16") and len(imm) == 4
    # 25th on a Sunday: anchor is Friday 23rd, minus 3 bdays
    assert rolls.calendar_roll_dates({"rule": "nymex_cl"}, "2027-04-01", "2027-04-30") == [pd.Timestamp("2027-04-20")]
    with pytest.raises(ValueError):
        rolls.calendar_roll_dates({"rule": "nope"}, "2026-01-01", "2026-02-01")


def test_yahoo_offsets_match_observed_rolls() -> None:
    cl = rolls.yahoo_roll_dates({"rule": "nymex_cl", "yahoo_offset_bdays": -2}, "2026-11-01", "2026-11-30")
    gc = rolls.yahoo_roll_dates({"rule": "last_bday_before_delivery", "delivery_months": [12],
                                 "yahoo_offset_bdays": -3}, "2026-11-01", "2026-11-30")
    assert cl == [pd.Timestamp("2026-11-18")] and gc == [pd.Timestamp("2026-11-25")]


def _bars_with_roll(roll_ts: pd.Timestamp, jump: float, news_ts: pd.Timestamp | None = None) -> pd.DataFrame:
    idx = pd.date_range("2026-11-16", "2026-11-21", freq="1min", tz="UTC", inclusive="left")
    rng = np.random.default_rng(1)
    logp = np.log(80) + np.cumsum(rng.normal(0, 3e-4, len(idx)))
    logp[idx >= roll_ts] += jump
    if news_ts is not None:
        logp[idx >= news_ts] += 3 * jump
    close = np.exp(logp)
    return pd.DataFrame({"ts": idx, "open": close, "high": close, "low": close, "close": close, "volume": 1.0})


def test_flag_rolls_picks_jump_near_expected_date_not_news() -> None:
    roll_ts = pd.Timestamp("2026-11-18 08:31", tz="UTC")
    bars = _bars_with_roll(roll_ts, -0.015, news_ts=pd.Timestamp("2026-11-16 13:30", tz="UTC"))
    out = rolls.flag_rolls(bars, "CL=F", rule={"rule": "nymex_cl", "yahoo_offset_bdays": -2})
    assert out["calendar_rolls"] == ["2026-11-18"]
    assert len(out["rolls"]) == 1 and out["rolls"][0]["ts"] == roll_ts.isoformat()
    assert len(out["gaps"]) == 1 and "suspected contract roll" in out["gaps"][0]["reason"]
    assert len(out["jumps"]) >= 2  # the news jump is detected but not called a roll


def test_flag_rolls_reports_missing_jump_and_missing_rule() -> None:
    bars = _bars_with_roll(pd.Timestamp("2026-11-18 08:31", tz="UTC"), 0.0)
    out = rolls.flag_rolls(bars, "CL=F", rule={"rule": "nymex_cl", "yahoo_offset_bdays": -2})
    assert out["rolls"][0]["ts"] is None and "no jump" in out["caveats"][0]
    none = rolls.flag_rolls(bars, "6E=F", rule=None)
    assert none["rolls"] == [] and "no roll_rule" in none["caveats"][0]


def test_detect_jumps_short_input_and_match_contract() -> None:
    assert rolls.detect_jumps(pd.DataFrame({"ts": [], "close": []}), CFG["roll_detection"]).empty
    ts = pd.date_range("2026-11-18 08:29", periods=4, freq="1min", tz="UTC")
    cont = pd.DataFrame({"ts": ts, "close": [84.3, 84.33, 83.11, 83.03]})
    k = pd.DataFrame({"ts": ts, "close": [83.1, 83.12, 83.11, 83.03]})
    m = rolls.match_contract(cont, k, CFG["roll_detection"]["contract_match_tol"])
    assert m["match"].tolist() == [False, False, True, True]
