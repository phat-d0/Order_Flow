"""Equities agent tests for the Alpaca IEX loader. Mocked HTTP only — no network."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests

from flow_model import settings
from flow_model.data import schema, storage
from flow_model.data.loaders import alpaca
from flow_model.data.loaders.alpaca import AlpacaCredentials, AlpacaLoader, RateLimiter
from flow_model.data.loaders.base import NotSupported, SourceUnavailable

FIXTURES = Path(__file__).parent / "fixtures" / "equities"
KEY_ID = "PKTESTKEYID0000000000"
SECRET = "sEcReT-value-that-must-never-appear"
START = datetime(2026, 11, 25, 14, 30, tzinfo=timezone.utc)
END = datetime(2026, 11, 25, 21, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.reason = {200: "OK", 401: "Unauthorized", 429: "Too Many Requests", 500: "Server Error"}.get(
            status, ""
        )

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Returns queued responses (or raises queued exceptions) and records every call."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict, headers: dict, timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers), "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def cfg() -> dict[str, Any]:
    return settings.load_config("sources")["alpaca"]


def _loader(cfg: dict, responses: list[Any], secret: str | None = SECRET, key: str | None = KEY_ID):
    sleeps: list[float] = []
    creds = AlpacaCredentials(key_id=key, secret=secret, placeholder=cfg["secret_placeholder"])
    loader = AlpacaLoader(cfg=cfg, credentials=creds, session=FakeSession(responses),
                          sleep=sleeps.append, clock=lambda: 0.0)
    loader.limiter = RateLimiter(cfg["max_requests_per_min"], clock=lambda: 0.0, sleep=lambda s: None)
    return loader, sleeps


# ------------------------------------------------------------------------------------- credentials
def test_credentials_from_env_and_headers(cfg: dict) -> None:
    creds = AlpacaCredentials.from_env(cfg, env={cfg["key_env"]: KEY_ID, cfg["secret_env"]: SECRET})
    assert creds.configured and creds.secret_source == "env"
    assert creds.headers(cfg) == {cfg["key_header"]: KEY_ID, cfg["secret_header"]: SECRET}


def test_placeholder_secret_when_env_secret_absent(cfg: dict) -> None:
    creds = AlpacaCredentials.from_env(cfg, env={cfg["key_env"]: KEY_ID})
    assert creds.secret_source == "proxy"
    assert creds.headers(cfg)[cfg["secret_header"]] == cfg["secret_placeholder"]


def test_missing_key_id_is_not_configured(cfg: dict) -> None:
    creds = AlpacaCredentials.from_env(cfg, env={cfg["secret_env"]: SECRET, cfg["key_env"]: "  "})
    assert not creds.configured
    with pytest.raises(SourceUnavailable, match="keys not configured"):
        creds.headers(cfg)


def test_reprs_never_show_secrets(cfg: dict) -> None:
    loader, _ = _loader(cfg, [])
    for text in (repr(loader.credentials), str(loader.credentials), repr(loader)):
        assert SECRET not in text and KEY_ID not in text
    assert "configured=True" in repr(loader.credentials)


def test_logs_and_errors_never_leak_secrets(cfg: dict, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    responses = [FakeResponse(429, {"message": "too many requests"}),
                 requests.ConnectionError("boom"),
                 FakeResponse(200, _fixture("alpaca_trades_page2.json")),
                 FakeResponse(401, {"message": "unauthorized."})]
    loader, _ = _loader(cfg, responses)
    loader.fetch("trades", "SPY", START, END)
    with pytest.raises(SourceUnavailable) as err:
        loader.fetch("trades", "SPY", START, END)
    text = caplog.text + str(err.value) + json.dumps(loader.last_stats, default=str)
    assert SECRET not in text and KEY_ID not in text
    assert "retry" in caplog.text


# ---------------------------------------------------------------------------------------- fetching
def test_pagination_and_schema(cfg: dict) -> None:
    loader, _ = _loader(cfg, [FakeResponse(200, _fixture("alpaca_trades_page1.json")),
                              FakeResponse(200, _fixture("alpaca_trades_page2.json"))])
    df = loader.fetch("trades", "SPY", START, END)
    calls = loader.session.calls
    assert len(calls) == 2
    assert "page_token" not in calls[0]["params"]
    assert calls[1]["params"]["page_token"] == _fixture("alpaca_trades_page1.json")["next_page_token"]
    p = calls[0]["params"]
    assert p["feed"] == "iex" and p["limit"] == cfg["page_limit"]
    assert p["start"] == "2026-11-25T14:30:00Z" and p["end"] == "2026-11-25T21:00:00Z"
    assert calls[0]["url"].endswith("/v2/stocks/SPY/trades")
    assert calls[0]["headers"][cfg["secret_header"]] == SECRET
    # 4 raw rows; the one stamped exactly at END is excluded (half-open window)
    assert len(df) == 3 and loader.last_stats["rows_raw"] == 4 and loader.last_stats["pages"] == 2
    assert schema.validate(df, "trades") == []
    assert list(df.columns) == schema.columns("trades")
    assert (df["side"] == 0).all() and (df["venue"] == "iex").all() and (df["source"] == "alpaca").all()
    assert df["trade_id"].iloc[0] == "52983525029461"
    assert df["recv_ts"].isna().all()


def test_nanosecond_precision_preserved(cfg: dict) -> None:
    loader, _ = _loader(cfg, [FakeResponse(200, _fixture("alpaca_trades_page1.json")),
                              FakeResponse(200, _fixture("alpaca_trades_page2.json"))])
    df = loader.fetch("trades", "SPY", START, END)
    assert df["ts"].iloc[0] == pd.Timestamp("2026-11-25T14:30:00.012345678Z")
    assert df["ts"].iloc[1].nanosecond == 123
    assert loader.last_stats["ts_frac_digits_max"] == 9


def test_quotes_clean_and_count(cfg: dict) -> None:
    loader, _ = _loader(cfg, [FakeResponse(200, _fixture("alpaca_quotes_page.json"))])
    df = loader.fetch("quotes", "AAPL", START, END)
    assert schema.validate(df, "quotes") == []
    assert len(df) == 2  # normal + locked; one-sided and crossed dropped and counted
    assert loader.last_stats["dropped_one_sided"] == 1 and loader.last_stats["dropped_crossed"] == 1
    assert df["bid_sz"].tolist() == [3.0, 4.0] and df["ask_px"].tolist() == [230.12, 230.11]
    assert loader.session.calls[0]["url"].endswith("/v2/stocks/AAPL/quotes")


def test_retry_on_429_uses_backoff(cfg: dict) -> None:
    loader, sleeps = _loader(cfg, [FakeResponse(429), FakeResponse(500),
                                   FakeResponse(200, _fixture("alpaca_trades_page2.json"))])
    df = loader.fetch("trades", "SPY", START, END)
    assert len(df) == 1
    assert sleeps == [float(b) for b in cfg["retry_backoff_s"][:2]]


def test_retries_exhausted_raises(cfg: dict) -> None:
    n = len(cfg["retry_backoff_s"]) + 1
    loader, sleeps = _loader(cfg, [FakeResponse(429)] * n)
    with pytest.raises(SourceUnavailable, match="429 rate limited"):
        loader.fetch("trades", "SPY", START, END)
    assert len(loader.session.calls) == n and len(sleeps) == n - 1


def test_401_fails_fast(cfg: dict) -> None:
    loader, sleeps = _loader(cfg, [FakeResponse(401)])
    with pytest.raises(SourceUnavailable, match="401 unauthorized"):
        loader.fetch("trades", "SPY", START, END)
    assert sleeps == []


def test_fetch_without_keys_makes_no_request(cfg: dict) -> None:
    loader, _ = _loader(cfg, [], key=None)
    with pytest.raises(SourceUnavailable, match="keys not configured"):
        loader.fetch("trades", "SPY", START, END)
    assert loader.session.calls == []


def test_unsupported_kind(cfg: dict) -> None:
    loader, _ = _loader(cfg, [])
    with pytest.raises(NotSupported):
        loader.fetch("l2", "SPY", START, END)


def test_empty_page_gives_valid_empty_frame(cfg: dict) -> None:
    loader, _ = _loader(cfg, [FakeResponse(200, {"symbol": "SPY", "trades": None, "next_page_token": None})])
    df = loader.fetch("trades", "SPY", START, END)
    assert df.empty and schema.validate(df, "trades") == []


# ------------------------------------------------------------------------------------------- check
def test_check_without_keys(cfg: dict) -> None:
    loader, _ = _loader(cfg, [], key=None)
    res = loader.check()
    assert not res.ok and res.detail == "keys not configured" and loader.session.calls == []


@pytest.mark.parametrize(
    "item, detail",
    [(FakeResponse(401), "401 unauthorized"), (FakeResponse(403, {"message": "forbidden."}), "403 forbidden"),
     (requests.ConnectionError("x"), "unreachable: ConnectionError")],
)
def test_check_failures_never_raise(cfg: dict, item: Any, detail: str) -> None:
    loader, _ = _loader(cfg, [item])
    res = loader.check()
    assert not res.ok and res.detail.startswith(detail)
    assert SECRET not in json.dumps(res.to_dict())


def test_check_ok_records_rate_limit(cfg: dict) -> None:
    headers = {"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "199"}
    loader, _ = _loader(cfg, [FakeResponse(200, _fixture("alpaca_latest_trade.json"), headers)])
    res = loader.check()
    assert res.ok and res.extra["latest_trade_ts"].startswith("2026-11-25")
    assert res.extra["rate_limit"]["X-RateLimit-Limit"] == "200"
    assert loader.session.calls[0]["url"].endswith("/v2/stocks/SPY/trades/latest")


# ------------------------------------------------------------------------------- helpers and CLI
def test_rate_limiter_spacing() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    lim = RateLimiter(120, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        lim.wait()
    assert slept == pytest.approx([0.5, 0.5])


def test_rth_window_handles_dst(cfg: dict) -> None:
    lo, hi = alpaca.rth_window(date(2026, 11, 25), cfg)  # EST
    assert (lo.hour, lo.minute, hi.hour) == (14, 30, 21)
    lo, hi = alpaca.rth_window(date(2026, 7, 1), cfg)  # EDT
    assert (lo.hour, lo.minute, hi.hour) == (13, 30, 20)


def test_last_full_rth_day_skips_holiday_and_weekend(cfg: dict) -> None:
    loader, _ = _loader(cfg, [])
    asked: list[date] = []

    def has_trades(symbol: str, start: datetime, end: datetime) -> bool:
        asked.append(start.date())
        return start.date() != date(2026, 11, 26)  # Thanksgiving: no trades

    loader.has_trades = has_trades  # type: ignore[method-assign]
    now = datetime(2026, 11, 27, 22, 0, tzinfo=timezone.utc)  # Friday after close
    # Friday 27th is a half-day in reality; here its probe "has trades", so it is chosen.
    assert alpaca.last_full_rth_day(loader, "SPY", now) == date(2026, 11, 27)
    now = datetime(2026, 11, 26, 22, 0, tzinfo=timezone.utc)  # Thanksgiving evening
    assert alpaca.last_full_rth_day(loader, "SPY", now) == date(2026, 11, 25)


def test_frac_digits() -> None:
    assert alpaca.frac_digits("2026-11-25T14:30:00Z") == 0
    assert alpaca.frac_digits("2026-11-25T14:30:00.123Z") == 3
    assert alpaca.frac_digits("2026-11-25T14:30:00.123456789Z") == 9


def test_pull_writes_store_and_reports_failures(cfg: dict, tmp_path: Path) -> None:
    loader, _ = _loader(cfg, [FakeResponse(200, _fixture("alpaca_trades_page2.json")), FakeResponse(401)])
    out = alpaca.pull(loader, ["SPY"], ["trades", "quotes"], day=date(2026, 11, 25), root=tmp_path)
    ok, failed = out["results"]
    assert ok["status"] == "ok" and ok["rows"] == 1
    assert failed["status"] == "failed" and "401" in failed["reason"]
    stored = storage.read_range("alpaca", "SPY", "trades", root=tmp_path)
    assert len(stored) == 1 and schema.validate(stored, "trades") == []
    assert out["window_utc"] == ["2026-11-25T14:30:00Z", "2026-11-25T21:00:00Z"]


def test_trade_conditions_populated(cfg: dict) -> None:
    loader, _ = _loader(cfg, [FakeResponse(200, _fixture("alpaca_trades_page1.json")),
                              FakeResponse(200, _fixture("alpaca_trades_page2.json"))])
    df = loader.fetch("trades", "SPY", START, END)
    assert df["conditions"].tolist()[:2] == ["@", "@,I"]
    assert schema.validate(df, "trades") == []


def test_join_conditions() -> None:
    assert alpaca.join_conditions(["@", " ", "I"]) == "@,I"
    assert alpaca.join_conditions([" "]) is None
    assert alpaca.join_conditions(None) is None
