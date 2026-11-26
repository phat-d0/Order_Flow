"""yfinance 1m loader: normalisation, chunking, retries, lookback, sessions, gaps. No network."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from flow_model.data import schema
from flow_model.data.loaders import yfinance_bars as yb
from flow_model.data.loaders.base import NotSupported, SourceUnavailable
from flow_model.settings import load_config

FIXTURE = Path(__file__).parent / "fixtures" / "commodities" / "yf_1m_sample.csv"
CFG = load_config("sources")["yfinance"]
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def raw_fixture() -> pd.DataFrame:
    frame = pd.read_csv(FIXTURE)
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("Datetime"), utc=True)).tz_convert("America/New_York")
    return frame


def synthetic_raw(start: datetime, end: datetime, seed: int = 0) -> pd.DataFrame:
    """yfinance-shaped minute bars for [start, end) with Yahoo's first-row volume artifact."""
    idx = pd.date_range(pd.Timestamp(start).ceil("min"), end, freq="1min", inclusive="left")
    rng = np.random.default_rng(seed)
    close = 80 + np.cumsum(rng.normal(0, 0.01, len(idx)))
    frame = pd.DataFrame({"Open": close, "High": close + 0.02, "Low": close - 0.02, "Close": close,
                          "Adj Close": close, "Volume": rng.integers(1, 50, len(idx)).astype(float)},
                         index=idx.tz_convert("America/New_York"))
    if len(frame):
        frame.iloc[0, frame.columns.get_loc("Volume")] = 0.0
    return frame


class FakeClient:
    """Records calls; optional scripted exceptions before succeeding."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.calls: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        self.errors = list(errors or [])

    def __call__(self, symbol, start, end, interval, timeout_s, prepost):  # noqa: ANN001
        self.calls.append((pd.Timestamp(start), pd.Timestamp(end)))
        if self.errors:
            raise self.errors.pop(0)
        return synthetic_raw(start, end)


def make_loader(client=None, sleeps: list | None = None) -> yb.YFinanceBarsLoader:  # noqa: ANN001
    sleeps = sleeps if sleeps is not None else []
    return yb.YFinanceBarsLoader(CFG, client=client or FakeClient(), sleep=sleeps.append, now=lambda: NOW)


def test_normalize_counts_every_drop_and_outputs_utc_schema() -> None:
    report = yb.FetchReport("CL=F", "", "")
    out = yb.normalize_bars(raw_fixture(), "CL=F", "yfinance", "yahoo", "1m", report)
    assert schema.validate(out, "bars") == []
    assert str(out["ts"].dt.tz) == "UTC"
    assert out["ts"].iloc[0] == pd.Timestamp("2026-11-24 14:28", tz="UTC")
    assert report.n_raw == 9 and report.n_kept == len(out) == 6
    assert report.dropped == {"nan_price": 1, "inconsistent_ohlc": 1, "duplicate_ts": 1}
    assert report.dropped_examples["nan_price"] == ["2026-11-24 14:31:00+00:00"]
    # duplicate keeps the last row; zero-volume first bar kept and counted
    assert out.loc[out["ts"] == pd.Timestamp("2026-11-24 14:34", tz="UTC"), "volume"].item() == 96
    assert report.n_zero_volume == 1


def test_normalize_empty_and_missing_columns() -> None:
    assert yb.normalize_bars(pd.DataFrame(), "X", "yfinance", "yahoo", "1m").empty
    with pytest.raises(SourceUnavailable):
        yb.normalize_bars(raw_fixture().drop(columns="Volume"), "X", "yfinance", "yahoo", "1m")


def test_chunk_windows_respect_max_days() -> None:
    windows = yb.chunk_windows(datetime(2026, 11, 1), datetime(2026, 11, 20), 7)
    assert len(windows) == 3
    assert all(b - a <= pd.Timedelta(days=7) for a, b in windows)
    assert windows[0][0] == pd.Timestamp("2026-11-01", tz="UTC") and windows[-1][1] == pd.Timestamp("2026-11-20", tz="UTC")


def test_fetch_chunks_with_lead_in_pause_and_exact_window() -> None:
    client, sleeps = FakeClient(), []
    loader = make_loader(client, sleeps)
    start, end = pd.Timestamp("2026-11-05", tz="UTC"), pd.Timestamp("2026-11-20", tz="UTC")
    frame, report = loader.fetch_with_report("bars", "CL=F", start, end)
    lead = pd.Timedelta(minutes=CFG["lead_in_minutes"])
    assert len(client.calls) == 3
    assert client.calls[0][0] == start - lead
    assert all(b - a <= pd.Timedelta(days=CFG["max_days_per_request"]) + lead for a, b in client.calls)
    assert sleeps == [CFG["request_pause_s"]] * 2
    assert frame["ts"].min() == start and frame["ts"].max() < end
    assert frame["ts"].is_unique and schema.validate(frame, "bars") == []
    # the first-row zero-volume artifact falls in the discarded lead-in
    assert report.n_zero_volume == 0 and len(report.requests) == 3


def test_fetch_clips_to_lookback_and_records_gap() -> None:
    loader = make_loader()
    start = pd.Timestamp(NOW) - pd.Timedelta(days=40)
    frame, report = loader.fetch_with_report("bars", "GC=F", start, pd.Timestamp(NOW) - pd.Timedelta(days=25))
    oldest = pd.Timestamp(NOW) - pd.Timedelta(days=CFG["lookback_days"])
    assert frame["ts"].min() >= oldest
    assert report.gaps and "lookback" in report.gaps[0]["reason"]
    with pytest.raises(SourceUnavailable):
        loader.fetch("bars", "GC=F", start, start + pd.Timedelta(days=2))


def test_fetch_end_is_capped_at_now_and_kind_checked() -> None:
    frame = make_loader().fetch("bars", "CL=F", pd.Timestamp(NOW) - pd.Timedelta(hours=1),
                                pd.Timestamp(NOW) + pd.Timedelta(days=1))
    assert frame["ts"].max() < pd.Timestamp(NOW)
    with pytest.raises(NotSupported):
        make_loader().fetch("trades", "CL=F", NOW, NOW)


def test_rate_limit_backoff_then_success() -> None:
    client, sleeps = FakeClient([yb.RateLimited("429"), yb.RateLimited("429")]), []
    frame, report = make_loader(client, sleeps).fetch_with_report(
        "bars", "CL=F", pd.Timestamp(NOW) - pd.Timedelta(hours=2), NOW)
    assert len(frame) > 0
    base = CFG["backoff_base_s"]
    assert sleeps == [base, base * 2]
    assert [r["outcome"] for r in report.requests] == ["rate_limited", "rate_limited", "ok"]


def test_rate_limit_exhausted_raises_source_unavailable() -> None:
    errors = [yb.RateLimited("429")] * (CFG["max_retries"] + 1)
    sleeps: list = []
    with pytest.raises(SourceUnavailable, match="rate_limited"):
        make_loader(FakeClient(errors), sleeps).fetch("bars", "CL=F", pd.Timestamp(NOW) - pd.Timedelta(hours=1), NOW)
    assert len(sleeps) == CFG["max_retries"]
    assert max(sleeps) <= CFG["backoff_max_s"]


def test_rejected_is_not_retried_and_nodata_is_empty() -> None:
    sleeps: list = []
    with pytest.raises(SourceUnavailable, match="rejected"):
        make_loader(FakeClient([yb.Rejected("422 only 8 days")]), sleeps).fetch(
            "bars", "CL=F", pd.Timestamp(NOW) - pd.Timedelta(hours=1), NOW)
    assert sleeps == []
    frame, report = make_loader(FakeClient([yb.NoData("no price data")])).fetch_with_report(
        "bars", "CL=F", pd.Timestamp(NOW) - pd.Timedelta(hours=1), NOW)
    assert frame.empty and report.requests[0]["outcome"] == "no_data"


def test_yfinance_client_maps_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    import yfinance as yf
    from yfinance import exceptions as yfe

    class Ticker:
        exc: Exception = yfe.YFRateLimitError()

        def __init__(self, symbol: str) -> None:
            pass

        def history(self, **kwargs):  # noqa: ANN003, ANN201
            assert kwargs["keepna"] is True and kwargs["auto_adjust"] is False
            raise Ticker.exc

    monkeypatch.setattr(yf, "Ticker", Ticker)
    args = ("CL=F", NOW, NOW, "1m", 10, True)
    with pytest.raises(yb.RateLimited):
        yb.yfinance_client(*args)
    Ticker.exc = yfe.YFPricesMissingError("CL=F", "")
    with pytest.raises(yb.NoData):
        yb.yfinance_client(*args)
    Ticker.exc = Exception("1m data not available for startTime=1 and endTime=2")
    with pytest.raises(yb.Rejected):
        yb.yfinance_client(*args)
    assert yf.config.debug.hide_exceptions is True  # restored


def test_check_never_raises() -> None:
    ok = make_loader().check()
    assert ok.ok and ok.source == "yfinance" and "bars" in ok.detail
    bad = make_loader(FakeClient([yb.Rejected("nope")])).check()
    assert not bad.ok and "SourceUnavailable" in bad.detail


GLOBEX = CFG["sessions"]["globex"]


def test_session_minutes_globex_week() -> None:
    # Sunday 2026-11-22 00:00 UTC .. Saturday 2026-11-28 00:00 UTC (EST, UTC-5)
    grid = yb.session_minutes(datetime(2026, 11, 22), datetime(2026, 11, 28), GLOBEX, 1)
    assert grid.min() == pd.Timestamp("2026-11-22 23:00", tz="UTC")       # Sun 18:00 ET
    assert grid.max() == pd.Timestamp("2026-11-27 21:59", tz="UTC")       # Fri 16:59 ET
    assert pd.Timestamp("2026-11-23 22:30", tz="UTC") not in grid         # daily 17:00-18:00 ET halt
    assert len(grid) == 5 * 23 * 60
    # EDT week: Sunday open is 22:00 UTC
    summer = yb.session_minutes(datetime(2026, 7, 12), datetime(2026, 7, 13), GLOBEX, 1)
    assert summer.min() == pd.Timestamp("2026-07-12 22:00", tz="UTC")


def test_session_gaps_lists_long_runs_and_counts_short() -> None:
    start, end = pd.Timestamp("2026-11-24 14:00", tz="UTC"), pd.Timestamp("2026-11-24 15:00", tz="UTC")
    grid = pd.date_range(start, end, freq="1min", inclusive="left")
    missing = set(grid[10:20]) | {grid[30], grid[31]}
    bars = pd.DataFrame({"ts": [t for t in grid if t not in missing]})
    gaps, summary = yb.session_gaps(bars, start, end, GLOBEX, 1, min_report_minutes=5)
    assert summary["expected"] == 60 and summary["missing"] == 12 and summary["missing_runs"] == 2
    assert summary["short_runs_minutes"] == 2
    assert len(gaps) == 1 and gaps[0]["minutes"] == 10
    assert gaps[0]["start"] == grid[10].isoformat() and gaps[0]["end"] == grid[20].isoformat()
