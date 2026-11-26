"""Coinbase loader + WS recorder tests on small committed fixtures (no network unless marked)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from flow_model.data import schema, storage
from flow_model.data.loaders import coinbase as cb
from flow_model.data.loaders.base import NotSupported
from flow_model.data.recorders import coinbase_ws as rec

FIX = Path(__file__).parent / "fixtures" / "crypto"
RECV = pd.Timestamp("2026-11-26T07:44:41Z")


def _msgs() -> list[dict[str, Any]]:
    return json.loads((FIX / "coinbase_ws_messages.json").read_text())


def _rest() -> list[dict[str, Any]]:
    return json.loads((FIX / "coinbase_rest_trades.json").read_text())


def _processed() -> rec.FeedProcessor:
    proc = rec.FeedProcessor(cb.cfg())
    for m in _msgs():
        proc.process(m, RECV)
    return proc


# ---------------------------------------------------------------- side mapping (maker -> aggressor)


def test_maker_buy_means_seller_aggressor() -> None:
    assert cb.aggressor_side("buy") == -1
    assert cb.aggressor_side("sell") == 1
    assert cb.aggressor_side("???") == 0


def test_ticker_side_is_taker_side() -> None:
    assert cb.taker_side("buy") == 1
    assert cb.taker_side("sell") == -1


def test_match_row_inverts_maker_side() -> None:
    last_match, match = _msgs()[3], _msgs()[4]
    assert cb.match_row(last_match, RECV)["side"] == -1  # maker buy at the bid -> seller hit it
    assert cb.match_row(match, RECV)["side"] == 1  # maker sell at the ask -> buyer lifted it


def test_rest_trades_use_same_mapping() -> None:
    rows = [cb.rest_trade_row(t, "BTC-USD") for t in _rest()]
    assert [r["side"] for r in rows] == [-1, 1, -1]
    assert all(r["recv_ts"] is None for r in rows)


def test_ticker_cross_check_agrees_with_inverted_match() -> None:
    proc = _processed()
    assert proc.stats["side_check"] == {"agree": 1, "disagree": 0}


# ---------------------------------------------------------------- processor


def test_processor_rows_are_schema_valid() -> None:
    proc = _processed()
    out = proc.drain()
    for kind in ("trades", "quotes", "l2"):
        frame = cb.frame(out[kind], kind)
        assert schema.validate(frame, kind) == [], kind
    assert len(out["trades"]) == 3  # last_match, match, match (duplicate dropped)
    assert len(out["l2"]) == 6 + 2 + 1
    assert sum(r["is_snapshot"] for r in out["l2"]) == 6


def test_top_of_book_from_l2_book() -> None:
    quotes = cb.frame(_processed().drain()["quotes"], "quotes")
    assert len(quotes) == 2  # snapshot top, then change; the deep bid update leaves top unchanged
    first, second = quotes.iloc[0], quotes.iloc[1]
    assert (first.bid_px, first.ask_px) == (60558.65, 60558.66)
    assert (second.bid_px, second.bid_sz, second.ask_px, second.ask_sz) == (60558.70, 0.2, 60559.83, 0.00016745)


def test_trade_id_gap_duplicate_and_heartbeat() -> None:
    proc = _processed()
    gaps = [g for g in proc.gaps if g["kind"] == "trades"]
    assert len(gaps) == 1 and gaps[0]["missing"] == 3
    assert proc.pending_backfill[0][:3] == ("BTC-USD", 1148200531, 1148200535)
    assert proc.stats["messages"]["duplicate_trade"] == 1
    assert proc.stats["messages"]["heartbeat_ahead"] == 1


def test_l2_auth_error_is_recorded() -> None:
    errs = _processed().stats["errors"]
    assert len(errs) == 1 and "require authentication" in errs[0]["reason"]


def test_ticker_quotes_fallback() -> None:
    proc = rec.FeedProcessor({**cb.cfg(), "quotes_from": "ticker"})
    for m in _msgs():
        proc.process(m, RECV)
    quotes = proc.drain()["quotes"]
    assert len(quotes) == 1 and quotes[0]["ask_sz"] == pytest.approx(0.02607992)


def test_book_best_tracking() -> None:
    book = rec.Book()
    book.reset([["100", "1"], ["99", "2"]], [["101", "1"], ["102", "3"]])
    book.apply("buy", 100.0, 0.0)
    assert book.best_bid == 99.0
    book.apply("sell", 100.5, 4.0)
    assert book.top() == (99.0, 2.0, 100.5, 4.0)
    book.apply("sell", 100.5, 0.0)
    assert book.best_ask == 101.0


def test_l2update_before_snapshot_ignored() -> None:
    proc = rec.FeedProcessor(cb.cfg())
    proc.process(_msgs()[6], RECV)
    assert proc.stats["messages"]["l2update_before_snapshot"] == 1
    assert not proc.buffers["l2"]


# ---------------------------------------------------------------- recorder (fake network / fake REST)


class _FakeREST:
    def trades_between_ids(self, symbol: str, lo: int, hi: int) -> list[dict[str, Any]]:
        return [t for t in _rest() if lo < int(t["trade_id"]) < hi]


def test_flush_backfills_gap_and_writes_store(tmp_path: Path) -> None:
    r = rec.Recorder(["BTC-USD"], cb.cfg(), root=tmp_path / "store", logs_dir=tmp_path / "logs", minutes=1)
    r.rest = _FakeREST()
    for m in _msgs():
        r.proc.process(m, RECV)
    asyncio.run(r.flush())
    trades = storage.read_range("coinbase", "BTC-USD", "trades", root=tmp_path / "store")
    assert trades["trade_id"].tolist() == [str(i) for i in range(1148200530, 1148200536)]
    assert schema.validate(trades, "trades") == []
    gaps = [json.loads(line) for line in r.gaps_path.read_text().splitlines()]
    assert gaps[0]["backfilled"] == 3 and "back-filled 3" in gaps[0]["reason"]
    assert len(storage.read_range("coinbase", "BTC-USD", "quotes", root=tmp_path / "store")) == 2


def test_flush_rolls_over_utc_days(tmp_path: Path) -> None:
    proc = rec.FeedProcessor(cb.cfg())
    base = _msgs()[4]
    for i, t in enumerate(["2026-11-26T23:59:59.900000Z", "2026-11-27T00:00:00.100000Z"]):
        proc.process({**base, "trade_id": 10 + i, "time": t}, RECV)
    paths = storage.write_partitioned(cb.frame(proc.drain()["trades"], "trades"), "trades", root=tmp_path)
    assert sorted(p.name for p in paths) == ["2026-11-26.parquet", "2026-11-27.parquet"]


class _FakeWS:
    def __init__(self, msgs: list[dict[str, Any]]):
        self.msgs = [json.dumps(m) for m in msgs]

    async def __aenter__(self) -> "_FakeWS":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def send(self, _: str) -> None:
        return None

    async def recv(self) -> str:
        if not self.msgs:
            raise ConnectionResetError("fake drop")
        return self.msgs.pop(0)


def test_reconnect_logs_gap_and_dedupes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: _FakeWS(_msgs()))
    conf = {**cb.cfg(), "reconnect_backoff_s": [0.05], "backfill_trades_on_gap": False, "flush_every_s": 0.1}
    r = rec.Recorder(["BTC-USD"], conf, root=tmp_path / "store", logs_dir=tmp_path / "logs", minutes=0.01)
    summary = asyncio.run(r.run())
    assert summary["reconnects"] >= 1
    assert any(g.get("reconnect") for g in summary["gap_records"])
    trades = storage.read_range("coinbase", "BTC-USD", "trades", root=tmp_path / "store")
    assert len(trades) == 3  # replayed messages after reconnect are de-duplicated
    assert r.gaps_path.exists()


def test_stale_feed_logs_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import websockets

    class _Silent(_FakeWS):
        async def recv(self) -> str:
            if self.msgs:
                return self.msgs.pop(0)
            await asyncio.sleep(10)
            return ""

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: _Silent(_msgs()[:5]))
    conf = {**cb.cfg(), "heartbeat_timeout_s": 0.1, "reconnect_backoff_s": [0.05], "backfill_trades_on_gap": False}
    r = rec.Recorder(["BTC-USD"], conf, root=tmp_path / "s", logs_dir=tmp_path / "l", minutes=0.008)
    summary = asyncio.run(r.run())
    assert any("stale" in g["reason"] for g in summary["gap_records"])


# ---------------------------------------------------------------- loader with fake REST


class _Resp:
    def __init__(self, data: Any):
        self._data, self.status_code, self.text = data, 200, ""

    def json(self) -> Any:
        return self._data


class _FakeSession:
    """Emulates GET /products/<id>/trades?limit&after (ids < after, newest first)."""

    def __init__(self, n: int = 50):
        t0 = pd.Timestamp("2026-11-25T23:59:50Z")
        self.trades = [
            {"trade_id": i, "side": "buy" if i % 2 else "sell", "size": "0.1", "price": str(100 + i),
             "time": (t0 + pd.Timedelta(seconds=i * 0.5)).isoformat().replace("+00:00", "Z")}
            for i in range(1, n + 1)
        ]
        self.calls = 0

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float = 0) -> _Resp:
        self.calls += 1
        params = params or {}
        after = int(params.get("after", 10**12))
        page = [t for t in reversed(self.trades) if t["trade_id"] < after][: int(params.get("limit", 1000))]
        return _Resp(page)


def test_loader_fetch_trades_window() -> None:
    conf = {**cb.cfg(), "rest_pause_s": 0, "rest_page_limit": 7}
    loader = cb.CoinbaseLoader(rest=cb.CoinbaseREST(conf, session=_FakeSession()))
    loader.cfg = conf
    start, end = pd.Timestamp("2026-11-26T00:00:00Z"), pd.Timestamp("2026-11-26T00:00:05Z")
    out = loader.fetch("trades", "BTC-USD", start.to_pydatetime(), end.to_pydatetime())
    assert schema.validate(out, "trades") == []
    assert out["ts"].min() >= start and out["ts"].max() < end
    assert out["trade_id"].tolist() == [str(i) for i in range(20, 30)]
    assert set(out["side"]) == {-1, 1}


def test_loader_quotes_not_supported() -> None:
    loader = cb.CoinbaseLoader(rest=cb.CoinbaseREST({**cb.cfg(), "rest_pause_s": 0}, session=_FakeSession()))
    with pytest.raises(NotSupported):
        loader.fetch("quotes", "BTC-USD", pd.Timestamp("2026-11-26", tz="UTC"), pd.Timestamp("2026-11-27", tz="UTC"))


def test_check_never_raises() -> None:
    class _Boom:
        def get(self, *a: Any, **k: Any) -> Any:
            raise OSError("down")

    conf = {**cb.cfg(), "rest_pause_s": 0, "rest_max_retries": 0, "rest_retry_backoff_s": 0}
    res = cb.CoinbaseLoader(rest=cb.CoinbaseREST(conf, session=_Boom())).check()
    assert res.ok is False and "down" in res.detail


@pytest.mark.network
def test_live_rest_check() -> None:
    assert cb.CoinbaseLoader().check().ok
