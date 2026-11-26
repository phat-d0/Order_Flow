"""Binance archive loader tests on small committed CSV fixtures (no network unless marked)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from flow_model.data import schema, storage
from flow_model.data.loaders import binance as bn
from flow_model.data.loaders.base import NotSupported, SourceUnavailable

FIX = Path(__file__).parent / "fixtures" / "crypto"


def test_symbol_mapping() -> None:
    assert bn.binance_symbol("BTC-USD") == "BTCUSDT"
    assert bn.binance_symbol("ETH-USD") == "ETHUSDT"
    assert bn.binance_symbol("BTCUSDT") == "BTCUSDT"


def test_aggtrades_microseconds_no_header() -> None:
    out = bn.parse_aggtrades_csv((FIX / "binance_aggtrades_us.csv").read_bytes(), "BTCUSDT")
    assert schema.validate(out, "trades") == []
    assert out["side"].tolist() == [-1, 1, 1]  # is_buyer_maker True -> seller aggressor
    assert out["ts"].iloc[0] == pd.Timestamp("2026-11-25T00:00:00.097558Z")
    assert (out["venue"] == "binance_spot").all() and (out["symbol"] == "BTCUSDT").all()
    assert out["trade_id"].tolist() == ["4130980582", "4130980583", "4130980584"]
    assert out["recv_ts"].isna().all()


def test_aggtrades_milliseconds_with_header() -> None:
    out = bn.parse_aggtrades_csv((FIX / "binance_aggtrades_ms_header.csv").read_bytes(), "BTCUSDT")
    assert schema.validate(out, "trades") == []
    assert out["ts"].iloc[0] == pd.Timestamp("2024-01-01T00:00:00.123Z")
    assert out["side"].tolist() == [-1, 1]


def test_bookticker_chunks_to_quotes() -> None:
    conf = bn.cfg()
    chunks = pd.read_csv(FIX / "binance_bookticker.csv", chunksize=2)
    lo, hi = pd.Timestamp("2024-03-30T00:00:00.004Z"), pd.Timestamp("2024-03-31T00:00:00Z")
    out = bn.parse_bookticker_chunks(chunks, "BTCUSDT", conf, lo, hi)
    assert schema.validate(out, "quotes") == []
    assert len(out) == 2 and (out["source"] == "binance_um").all() and (out["venue"] == "binance_usdm").all()


class _Resp:
    def __init__(self, status: int, body: bytes = b""):
        self.status_code, self.content, self.text = status, body, body.decode(errors="ignore")

    def iter_content(self, chunk_size: int) -> Any:
        yield self.content

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Archive:
    """Serves one zipped aggTrades file + its CHECKSUM; everything else 404."""

    def __init__(self, day: str, csv: bytes, bad_checksum: bool = False):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"BTCUSDT-aggTrades-{day}.csv", csv)
        self.zip = buf.getvalue()
        digest = "0" * 64 if bad_checksum else hashlib.sha256(self.zip).hexdigest()
        self.name = f"BTCUSDT-aggTrades-{day}.zip"
        self.checksum = f"{digest}  {self.name}".encode()

    def get(self, url: str, timeout: float = 0, stream: bool = False) -> _Resp:
        if url.endswith(self.name):
            return _Resp(200, self.zip)
        if url.endswith(self.name + ".CHECKSUM"):
            return _Resp(200, self.checksum)
        return _Resp(404)


def _loader(archive: _Archive) -> bn.BinanceSpotLoader:
    return bn.BinanceSpotLoader({**bn.cfg(), "request_pause_s": 0}, session=archive)  # type: ignore[arg-type]


def test_spot_fetch_and_load_into_store(tmp_path: Path) -> None:
    csv = (FIX / "binance_aggtrades_us.csv").read_bytes()
    loader = _loader(_Archive("2026-11-25", csv))
    start, end = pd.Timestamp("2026-11-25", tz="UTC"), pd.Timestamp("2026-11-26", tz="UTC")
    out = loader.load("trades", "BTC-USD", start, end, root=tmp_path)
    assert len(out) == 3 and schema.validate(out, "trades") == []
    assert storage.list_dates("binance", "BTCUSDT", "trades", root=tmp_path) == [start.date()]


def test_spot_fetch_filters_window() -> None:
    loader = _loader(_Archive("2026-11-25", (FIX / "binance_aggtrades_us.csv").read_bytes()))
    out = loader.fetch("trades", "BTCUSDT", pd.Timestamp("2026-11-25T00:00:00.1Z"),
                       pd.Timestamp("2026-11-25T00:00:00.5Z"))
    assert out["trade_id"].tolist() == ["4130980583"]


def test_missing_file_raises_unavailable() -> None:
    loader = _loader(_Archive("2026-11-25", b""))
    with pytest.raises(SourceUnavailable, match="404"):
        loader.fetch("trades", "BTCUSDT", pd.Timestamp("2026-11-26", tz="UTC"), pd.Timestamp("2026-11-27", tz="UTC"))


def test_checksum_mismatch_raises() -> None:
    loader = _loader(_Archive("2026-11-25", (FIX / "binance_aggtrades_us.csv").read_bytes(), bad_checksum=True))
    with pytest.raises(SourceUnavailable, match="checksum"):
        loader.fetch("trades", "BTCUSDT", pd.Timestamp("2026-11-25", tz="UTC"), pd.Timestamp("2026-11-26", tz="UTC"))


def test_spot_has_no_quotes() -> None:
    with pytest.raises(NotSupported):
        _loader(_Archive("2026-11-25", b"")).fetch("quotes", "BTCUSDT", pd.Timestamp("2026-11-25", tz="UTC"),
                                                   pd.Timestamp("2026-11-26", tz="UTC"))


def test_um_bookticker_after_archive_end_not_supported() -> None:
    loader = bn.BinanceUMLoader()
    with pytest.raises(NotSupported, match="2024-03-30"):
        loader.fetch("quotes", "BTC-USD", pd.Timestamp("2026-11-25", tz="UTC"), pd.Timestamp("2026-11-26", tz="UTC"))
    assert loader.source == "binance_um"


@pytest.mark.network
def test_live_archive_check() -> None:
    assert bn.BinanceSpotLoader().check().ok
    assert bn.BinanceUMLoader().check().ok
