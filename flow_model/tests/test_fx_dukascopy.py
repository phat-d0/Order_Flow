"""FX agent tests for the Dukascopy loader. No network: HTTP and sleep are injected."""

from __future__ import annotations

import lzma
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from flow_model import settings
from flow_model.data import schema, storage
from flow_model.data.loaders.base import NotSupported, SourceUnavailable
from flow_model.data.loaders.dukascopy import (
    DukascopyLoader,
    decode_bi5,
    encode_bi5,
    hour_url,
    pull_days,
)

CFG = settings.load_config("sources")["dukascopy"]
H10 = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)

# (ms offset, ask points, bid points, ask_vol, bid_vol) — EURUSD around 1.05
RECORDS = [
    (0, 105012, 105010, 1.5, 2.25),
    (250, 105013, 105010, 0.75, 1.0),
    (250, 105014, 105011, 1.0, 1.0),  # same ms as previous: kept, stable order
    (3_599_999, 105020, 105018, 3.0, 0.5),
]


class FakeHttp:
    """Serves a scripted sequence of (status, body) per URL; records calls."""

    def __init__(self, routes: dict[str, list[tuple[int, bytes]]], default: tuple[int, bytes] = (200, b"")):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, bytes, dict[str, str]]:
        self.calls.append(url)
        queue = self.routes.get(url)
        status, body = queue.pop(0) if queue else self.default
        return status, body, {}


def _loader(http: FakeHttp, sleeps: list[float] | None = None) -> DukascopyLoader:
    log = sleeps if sleeps is not None else []
    return DukascopyLoader(cfg=CFG, http_get=http, sleep=log.append)


def test_hour_url_month_is_zero_based() -> None:
    url = hour_url(CFG["base_url"], "EURUSD", datetime(2026, 1, 5, 7, tzinfo=timezone.utc))
    assert url.endswith("/EURUSD/2026/00/05/07h_ticks.bi5")
    assert hour_url("b", "EURUSD", H10) == "b/EURUSD/2026/10/24/10h_ticks.bi5"


def test_decode_roundtrip_big_endian() -> None:
    raw = encode_bi5(RECORDS)
    assert raw[:1] == b"\x5d"  # LZMA alone header (lc=3, lp=0, pb=2), as served by Dukascopy
    rec = decode_bi5(raw)
    assert rec["ms"].tolist() == [r[0] for r in RECORDS]
    assert rec["ask"].tolist() == [r[1] for r in RECORDS]
    np.testing.assert_allclose(rec["bid_vol"], [r[4] for r in RECORDS])
    assert len(decode_bi5(b"")) == 0


def test_decode_rejects_truncated_payload() -> None:
    bad = lzma.compress(b"\x00" * 19, format=lzma.FORMAT_ALONE)
    with pytest.raises(ValueError):
        decode_bi5(bad)


def test_fetch_hour_schema_prices_and_timestamps() -> None:
    url = hour_url(CFG["base_url"], "EURUSD", H10)
    frame, res = _loader(FakeHttp({url: [(200, encode_bi5(RECORDS))]})).fetch_hour("EURUSD", H10)
    assert res.status == "ok" and res.n_rows == 4
    assert schema.validate(frame, "quotes") == []
    assert list(frame.columns) == schema.columns("quotes")
    assert frame["venue"].unique().tolist() == ["dukascopy"]
    assert frame["bid_px"].iloc[0] == pytest.approx(1.05010)
    assert frame["ask_px"].iloc[0] == pytest.approx(1.05012)
    assert frame["ask_sz"].iloc[0] == pytest.approx(1.5) and frame["bid_sz"].iloc[0] == pytest.approx(2.25)
    assert frame["ts"].iloc[1] == pd.Timestamp("2026-11-24 10:00:00.250", tz="UTC")
    assert frame["ts"].iloc[-1] == pd.Timestamp("2026-11-24 10:59:59.999", tz="UTC")
    assert frame["recv_ts"].isna().all()


def test_empty_hour_and_404_are_empty_not_failed() -> None:
    url = hour_url(CFG["base_url"], "EURUSD", H10)
    for status in (200, 404):
        frame, res = _loader(FakeHttp({url: [(status, b"")]})).fetch_hour("EURUSD", H10)
        assert res.status == "empty" and frame.empty
        assert schema.validate(frame, "quotes") == []


def test_crossed_quotes_dropped_and_counted() -> None:
    url = hour_url(CFG["base_url"], "EURUSD", H10)
    recs = RECORDS + [(4000, 105000, 105005, 1.0, 1.0)]  # bid > ask
    frame, res = _loader(FakeHttp({url: [(200, encode_bi5(sorted(recs)))]})).fetch_hour("EURUSD", H10)
    assert res.n_crossed_dropped == 1 and len(frame) == 4
    assert schema.validate(frame, "quotes") == []


def test_backoff_on_429_then_success() -> None:
    url = hour_url(CFG["base_url"], "EURUSD", H10)
    sleeps: list[float] = []
    http = FakeHttp({url: [(429, b"{}"), (503, b""), (200, encode_bi5(RECORDS))]})
    frame, res = _loader(http, sleeps).fetch_hour("EURUSD", H10)
    assert res.status == "ok" and res.attempts == 3 and len(frame) == 4
    pause = float(CFG["request_pause_s"])
    assert sleeps == [float(CFG["backoff_s"][0]), pause, float(CFG["backoff_s"][1]), pause]


def test_persistent_429_marks_hour_failed_and_fetch_raises() -> None:
    url = hour_url(CFG["base_url"], "EURUSD", H10)
    http = FakeHttp({url: [(429, b"{}")] * 10})
    loader = _loader(http)
    _, res = loader.fetch_hour("EURUSD", H10)
    assert res.status == "failed" and res.http_status == 429
    assert res.attempts == len(CFG["backoff_s"]) + 1
    http = FakeHttp({url: [(429, b"{}")] * 10})
    loader = _loader(http)
    with pytest.raises(SourceUnavailable):
        loader.fetch("quotes", "EURUSD", H10, H10.replace(hour=11))
    assert loader.last_report is not None and loader.last_report.summary()["hours_failed"]


def test_fetch_filters_window_and_rejects_other_kinds() -> None:
    url = hour_url(CFG["base_url"], "EURUSD", H10)
    loader = _loader(FakeHttp({url: [(200, encode_bi5(RECORDS))]}))
    start = H10.replace(second=0, microsecond=100_000)  # 10:00:00.100 -> drops the first tick
    out = loader.fetch("quotes", "EURUSD", start, H10.replace(hour=11))
    assert len(out) == 3 and schema.validate(out, "quotes") == []
    with pytest.raises(NotSupported):
        loader.fetch("trades", "EURUSD", H10, H10.replace(hour=11))


def test_pull_days_stores_and_reports_empty_hours(tmp_path) -> None:
    url = hour_url(CFG["base_url"], "EURUSD", H10)
    loader = _loader(FakeHttp({url: [(200, encode_bi5(RECORDS))]}))
    reports = pull_days(loader, "EURUSD", [date(2026, 11, 24)], root=tmp_path)
    rep = reports[0]
    assert rep["rows"] == 4 and rep["hours_ok"] == 1 and len(rep["hours_empty"]) == 23
    assert rep["hours_failed"] == []
    stored = storage.read_range("dukascopy", "EURUSD", "quotes", root=tmp_path)
    assert len(stored) == 4 and schema.validate(stored, "quotes") == []


def test_check_never_raises_on_network_error() -> None:
    import requests

    def boom(url: str):
        raise requests.ConnectionError("reset")

    loader = DukascopyLoader(cfg=CFG, http_get=boom, sleep=lambda s: None)
    res = loader.check()
    assert res.ok is False and "ConnectionError" in res.detail


def test_pull_days_stops_after_consecutive_failures(tmp_path) -> None:
    http = FakeHttp({}, default=(503, b""))
    loader = _loader(http)
    reports = pull_days(loader, "EURUSD", [date(2026, 11, 24), date(2026, 11, 25)], root=tmp_path)
    limit = int(CFG["max_consecutive_failed_hours"])
    assert len(reports) == 1 and reports[0]["aborted"] is True
    assert len(reports[0]["hours_failed"]) == limit
    assert len(http.calls) == limit * (len(CFG["backoff_s"]) + 1)
