"""Lead-owned tests for the common schemas, partitioned storage, loader base and settings."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from flow_model import settings
from flow_model.data import schema, storage
from flow_model.data.loaders.base import Loader, NotSupported, SourceCheck


def _trades(ts: list[str], side: list[int] | None = None) -> pd.DataFrame:
    n = len(ts)
    return pd.DataFrame(
        {
            "ts": pd.to_datetime(ts, utc=True),
            "symbol": ["BTC-USD"] * n,
            "source": ["test"] * n,
            "venue": ["test"] * n,
            "price": [100.0 + i for i in range(n)],
            "size": [1.0] * n,
            "side": side or [1] * n,
        }
    )


@pytest.mark.parametrize("kind", schema.KINDS)
def test_empty_frame_is_valid(kind: str) -> None:
    assert schema.validate(schema.empty_frame(kind), kind) == []


def test_coerce_fills_nullable_and_validates() -> None:
    df = schema.coerce(_trades(["2026-01-01 00:00:01", "2026-01-01 00:00:02"]), "trades")
    assert list(df.columns) == schema.columns("trades")
    assert str(df["ts"].dtype) == "datetime64[ns, UTC]"
    assert df["side"].dtype == "int8"
    assert schema.validate(df, "trades") == []


def test_coerce_rejects_missing_required() -> None:
    with pytest.raises(ValueError):
        schema.coerce(_trades(["2026-01-01"]).drop(columns="price"), "trades")


def test_validate_catches_bad_rows() -> None:
    df = schema.coerce(_trades(["2026-01-01 00:00:02", "2026-01-01 00:00:01"], side=[1, 2]), "trades")
    problems = schema.validate(df, "trades")
    assert any("sorted" in p for p in problems)
    assert any("side" in p for p in problems)


def test_validate_crossed_quotes() -> None:
    q = pd.DataFrame(
        {"ts": pd.to_datetime(["2026-01-01"], utc=True), "symbol": ["X"], "source": ["t"],
         "venue": ["t"], "bid_px": [101.0], "bid_sz": [1.0], "ask_px": [100.0], "ask_sz": [1.0]}
    )
    assert any("crossed" in p for p in schema.validate(schema.coerce(q, "quotes"), "quotes"))


def test_naive_timestamps_rejected() -> None:
    df = schema.coerce(_trades(["2026-01-01"]), "trades")
    df["ts"] = df["ts"].dt.tz_localize(None)
    assert any("UTC" in p for p in schema.validate(df, "trades"))


def test_storage_partitions_by_utc_date_and_dedupes(tmp_path) -> None:
    df = _trades(["2026-01-01 23:59:59", "2026-01-02 00:00:00", "2026-01-02 00:00:01"])
    files = storage.write_partitioned(df, "trades", root=tmp_path)
    assert [p.name for p in files] == ["2026-01-01.parquet", "2026-01-02.parquet"]
    storage.write_partitioned(df, "trades", root=tmp_path)  # append same rows again
    out = storage.read_range("test", "BTC-USD", "trades", root=tmp_path)
    assert len(out) == 3
    assert schema.validate(out, "trades") == []
    window = storage.read_range("test", "BTC-USD", "trades", "2026-01-02", "2026-01-02 00:00:01", root=tmp_path)
    assert len(window) == 1


def test_storage_symbol_sanitised(tmp_path) -> None:
    df = _trades(["2026-01-01"]).assign(symbol="CL=F")
    (path,) = storage.write_partitioned(df, "trades", root=tmp_path)
    assert "CL_F" in path.parts
    assert storage.list_dates("test", "CL=F", "trades", root=tmp_path) == [datetime(2026, 1, 1).date()]


def test_storage_refuses_invalid(tmp_path) -> None:
    with pytest.raises(ValueError):
        storage.write_partitioned(_trades(["2026-01-01"], side=[5]), "trades", root=tmp_path)


class _FakeLoader(Loader):
    source = "test"
    venue = "test"
    tier = "A"
    kinds = frozenset({"trades"})

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, kind, symbol, start, end):  # noqa: D102
        self.calls += 1
        return _trades([start.isoformat()])

    def check(self) -> SourceCheck:  # noqa: D102
        return SourceCheck(self.source, True, "none", "fake")


def test_loader_fetches_missing_days_once(tmp_path) -> None:
    loader = _FakeLoader()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 3, tzinfo=timezone.utc)
    assert len(loader.load("trades", "BTC-USD", start, end, root=tmp_path)) == 2
    loader.load("trades", "BTC-USD", start, end, root=tmp_path)
    assert loader.calls == 2
    with pytest.raises(NotSupported):
        loader.load("quotes", "BTC-USD", start, end, root=tmp_path)


def test_settings_registry() -> None:
    assert set(settings.list_instruments(tier="A")) == {"BTC-USD", "ETH-USD"}
    meta = settings.instrument("CL=F")
    assert meta["tier"] == "C" and "ofi" not in meta["methods"]
    assert settings.core_config()["ofi"]["windows"] == ["1s", "10s", "1min"]
    assert settings.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}) == {"a": {"b": 3, "c": 2}}
