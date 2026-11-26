"""Alpaca market data v2 loader — IEX feed trades and top-of-book quotes (Tier B, venue-level).

Read-only market data only: this module talks to ``data.alpaca.markets`` and nothing else. It never
touches trading/account endpoints.

Auth (``sources.yaml`` -> ``alpaca``):
- key ID from env var ``key_env`` -> header ``key_header``;
- secret from env var ``secret_env`` if set, otherwise ``secret_placeholder`` is sent in ``secret_header``
  and the cloud network proxy replaces it with a stored network secret.
A local ``.env`` (repo root) is loaded with python-dotenv. Credentials never appear in reprs, logs,
exceptions or the store.

CLI (one command once keys exist)::

    .venv/bin/python -m flow_model.data.loaders.alpaca check
    .venv/bin/python -m flow_model.data.loaders.alpaca pull            # last full RTH day, all equities
    .venv/bin/python -m flow_model.data.loaders.alpaca pull --date 2026-11-25 --symbols SPY AAPL
    .venv/bin/python -m flow_model.data.loaders.alpaca depth --symbols SPY
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from flow_model import settings
from flow_model.data import schema, storage
from flow_model.data.loaders.base import Loader, NotSupported, SourceCheck, SourceUnavailable

log = logging.getLogger(__name__)

SOURCE = "alpaca"
_KIND_PATH = {"trades": "trades", "quotes": "quotes"}
_RATE_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")


# --------------------------------------------------------------------------------------------- auth
@dataclass(frozen=True)
class AlpacaCredentials:
    """Key ID + secret. ``secret=None`` means "let the proxy inject it" (placeholder is sent).

    ``repr``/``str`` never show the values.
    """

    key_id: str | None = field(repr=False)
    secret: str | None = field(repr=False)
    placeholder: str = field(repr=False, default="")

    @classmethod
    def from_env(
        cls, cfg: Mapping[str, Any], env: Mapping[str, str] | None = None, dotenv_path: Path | None = None
    ) -> "AlpacaCredentials":
        """Read credentials from ``env`` (default: ``os.environ`` after loading ``.env`` if present)."""
        if env is None:
            _load_dotenv(dotenv_path or settings.REPO_ROOT / ".env")
            env = os.environ
        key_id = (env.get(cfg["key_env"]) or "").strip() or None
        secret = (env.get(cfg["secret_env"]) or "").strip() or None
        return cls(key_id=key_id, secret=secret, placeholder=str(cfg["secret_placeholder"]))

    @property
    def configured(self) -> bool:
        """True when a key ID is available (the secret may come from the proxy)."""
        return self.key_id is not None

    @property
    def secret_source(self) -> str:
        """``"env"`` if the secret came from the environment, else ``"proxy"``."""
        return "env" if self.secret else "proxy"

    def headers(self, cfg: Mapping[str, Any]) -> dict[str, str]:
        """HTTP auth headers. Do not log the result."""
        if not self.configured:
            raise SourceUnavailable("alpaca: keys not configured")
        return {
            cfg["key_header"]: str(self.key_id),
            cfg["secret_header"]: self.secret or self.placeholder,
        }

    def __repr__(self) -> str:  # explicit, so no future field change can leak a value
        return f"AlpacaCredentials(configured={self.configured}, secret_source={self.secret_source!r})"

    __str__ = __repr__


def _load_dotenv(path: Path) -> None:
    """Load ``path`` into ``os.environ`` without overriding existing variables (silently if absent)."""
    if not path.exists():
        return
    from dotenv import load_dotenv

    load_dotenv(path, override=False)


# ---------------------------------------------------------------------------------- rate limiting
class RateLimiter:
    """Spaces requests at least ``60 / max_per_min`` seconds apart."""

    def __init__(
        self,
        max_per_min: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 60.0 / float(max_per_min)
        self._clock, self._sleep = clock, sleep
        self._next = 0.0

    def wait(self) -> None:
        """Block until the next request is allowed."""
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


# ------------------------------------------------------------------------------------------ parsing
def _parse_ts(values: list[str]) -> pd.Series:
    return pd.to_datetime(pd.Series(values, dtype="object"), utc=True, format="ISO8601").astype(
        "datetime64[ns, UTC]"
    )


def frac_digits(ts: str) -> int:
    """Number of fractional-second digits in an RFC3339 timestamp string (precision evidence)."""
    if "." not in ts:
        return 0
    frac = ts.split(".", 1)[1]
    return len(frac.rstrip("Z").split("+")[0].split("-")[0])


def join_conditions(codes: list[str] | None) -> str | None:
    """Alpaca condition list -> comma-joined string (codes stripped; blanks removed; null if none)."""
    kept = [str(c).strip() for c in (codes or []) if str(c).strip()]
    return ",".join(kept) if kept else None


def parse_trades(rows: list[dict[str, Any]], symbol: str, venue: str) -> pd.DataFrame:
    """Alpaca trade objects -> ``trades`` schema. No aggressor flag -> ``side = 0``.

    Kept: ``t`` -> ts, ``p`` -> price, ``s`` -> size, ``i`` -> trade_id, ``c`` -> conditions
    (codes comma-joined, stripped; null if none). Dropped: ``x`` (exchange code, always IEX here),
    ``z`` (tape A/B/C).
    """
    if not rows:
        return schema.empty_frame("trades")
    df = pd.DataFrame(
        {
            "ts": _parse_ts([r["t"] for r in rows]),
            "symbol": symbol,
            "source": SOURCE,
            "venue": venue,
            "price": [float(r["p"]) for r in rows],
            "size": [float(r["s"]) for r in rows],
            "side": 0,
            "trade_id": [None if r.get("i") is None else str(r["i"]) for r in rows],
            "conditions": [join_conditions(r.get("c")) for r in rows],
            "recv_ts": pd.NaT,
        }
    )
    return schema.coerce(df, "trades")


def parse_quotes(rows: list[dict[str, Any]], symbol: str, venue: str) -> pd.DataFrame:
    """Alpaca quote objects -> ``quotes`` schema (IEX BBO, not NBBO).

    Kept: ``t``, ``bp``, ``bs``, ``ap``, ``as``. Dropped: ``bx``/``ax`` (exchange codes), ``c``
    (condition codes), ``z`` (tape). Sizes are passed through as Alpaca reports them.
    """
    if not rows:
        return schema.empty_frame("quotes")
    df = pd.DataFrame(
        {
            "ts": _parse_ts([r["t"] for r in rows]),
            "symbol": symbol,
            "source": SOURCE,
            "venue": venue,
            "bid_px": [float(r.get("bp", 0.0)) for r in rows],
            "bid_sz": [float(r.get("bs", float("nan"))) for r in rows],
            "ask_px": [float(r.get("ap", 0.0)) for r in rows],
            "ask_sz": [float(r.get("as", float("nan"))) for r in rows],
            "recv_ts": pd.NaT,
        }
    )
    return schema.coerce(df, "quotes")


def clean_quotes(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Drop quotes the schema forbids (one-sided: price <= 0; crossed: bid > ask). Returns counts.

    IEX's book is often one-sided or empty; these rows are reported, never silently removed.
    """
    one_sided = (df["bid_px"] <= 0) | (df["ask_px"] <= 0)
    crossed = ~one_sided & (df["bid_px"] > df["ask_px"])
    keep = ~(one_sided | crossed)
    counts = {"dropped_one_sided": int(one_sided.sum()), "dropped_crossed": int(crossed.sum())}
    return df.loc[keep].reset_index(drop=True), counts


def _rfc3339(value: datetime) -> str:
    utc = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return utc.isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------- loader
class AlpacaLoader(Loader):
    """Alpaca v2 stock trades and quotes, ``feed`` from config (IEX on the free plan)."""

    source = SOURCE
    tier = "B"
    kinds = frozenset({"trades", "quotes"})

    def __init__(
        self,
        cfg: Mapping[str, Any] | None = None,
        credentials: AlpacaCredentials | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = dict(cfg if cfg is not None else settings.load_config("sources")[SOURCE])
        self.venue = str(self.cfg["feed"])
        self.credentials = credentials if credentials is not None else AlpacaCredentials.from_env(self.cfg)
        self.session = session or requests.Session()
        self._sleep = sleep
        self.limiter = RateLimiter(self.cfg["max_requests_per_min"], clock=clock, sleep=sleep)
        self.last_stats: dict[str, Any] = {}
        self.rate_limit_seen: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"AlpacaLoader(feed={self.venue!r}, credentials={self.credentials!r})"

    # ---- HTTP
    def _url(self, path: str) -> str:
        return f"{str(self.cfg['data_url']).rstrip('/')}/{path.lstrip('/')}"

    def _get_once(self, url: str, params: Mapping[str, Any]) -> requests.Response:
        self.limiter.wait()
        headers = {"User-Agent": str(self.cfg["user_agent"]), **self.credentials.headers(self.cfg)}
        resp = self.session.get(url, params=dict(params), headers=headers, timeout=self.cfg["timeout_s"])
        for name in _RATE_HEADERS:
            if name in resp.headers:
                self.rate_limit_seen[name] = resp.headers[name]
        return resp

    def _get(self, url: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """GET with rate limiting and retries (429 / 5xx / connection errors). Raises SourceUnavailable."""
        backoff = [float(b) for b in self.cfg["retry_backoff_s"]]
        retry_statuses = set(self.cfg["retry_statuses"])
        problem = ""
        for attempt in range(len(backoff) + 1):
            resp: requests.Response | None = None
            try:
                resp = self._get_once(url, params)
            except requests.RequestException as exc:
                problem = f"connection error: {type(exc).__name__}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                problem = _status_detail(resp)
                if resp.status_code not in retry_statuses:
                    raise SourceUnavailable(f"alpaca {url}: {problem}")
            if attempt == len(backoff):
                break
            wait = self._retry_wait(backoff[attempt], resp)
            log.warning("alpaca %s: %s; retry %d in %.1fs", url, problem, attempt + 1, wait)
            self._sleep(wait)
        raise SourceUnavailable(f"alpaca {url}: {problem} after {len(backoff)} retries")

    @staticmethod
    def _retry_wait(default: float, resp: requests.Response | None) -> float:
        """Use ``X-RateLimit-Reset`` (unix seconds) on 429 when it asks for longer than the backoff."""
        if resp is None or resp.status_code != 429:
            return float(default)
        reset = resp.headers.get("X-RateLimit-Reset")
        try:
            return max(float(default), float(reset) - time.time()) if reset else float(default)
        except ValueError:
            return float(default)

    def _pages(self, kind: str, symbol: str, start: datetime, end: datetime) -> Iterator[list[dict]]:
        url = self._url(f"stocks/{symbol}/{_KIND_PATH[kind]}")
        params: dict[str, Any] = {
            "start": _rfc3339(start),
            "end": _rfc3339(end),
            "limit": self.cfg["page_limit"],
            "feed": self.cfg["feed"],
            "sort": self.cfg["sort"],
        }
        for _ in range(int(self.cfg["max_pages"])):
            body = self._get(url, params)
            yield body.get(kind) or []
            token = body.get("next_page_token")
            if not token:
                return
            params["page_token"] = token
        raise SourceUnavailable(f"alpaca {symbol} {kind}: hit max_pages={self.cfg['max_pages']}")

    # ---- Loader API
    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Download ``[start, end)`` of ``kind`` for ``symbol``; stats land in ``self.last_stats``."""
        self._require(kind)
        if not self.credentials.configured:
            raise SourceUnavailable("alpaca: keys not configured")
        parser = parse_trades if kind == "trades" else parse_quotes
        frames, n_pages, n_raw, digits = [], 0, 0, 0
        t0 = time.monotonic()
        for rows in self._pages(kind, symbol, start, end):
            n_pages += 1
            n_raw += len(rows)
            if rows:
                digits = max(digits, max(frac_digits(r["t"]) for r in rows))
                frames.append(parser(rows, symbol, self.venue))
            if n_pages % int(self.cfg["log_every_pages"]) == 0:
                log.info("alpaca %s %s: %d pages, %d rows", symbol, kind, n_pages, n_raw)
        frame = pd.concat(frames, ignore_index=True) if frames else schema.empty_frame(kind)
        stats: dict[str, Any] = {"pages": n_pages, "rows_raw": n_raw, "ts_frac_digits_max": digits}
        if kind == "quotes":
            frame, dropped = clean_quotes(frame)
            stats.update(dropped)
        # end is exclusive per the Loader contract (Alpaca's own `end` is inclusive)
        frame = frame[frame["ts"] < pd.Timestamp(_rfc3339(end))]
        frame = frame.sort_values("ts", kind="stable").reset_index(drop=True)
        stats.update(rows=len(frame), seconds=round(time.monotonic() - t0, 2))
        self.last_stats = {"kind": kind, "symbol": symbol, **stats}
        if stats.get("dropped_one_sided") or stats.get("dropped_crossed"):
            log.info("alpaca %s quotes: dropped %s", symbol, {k: v for k, v in stats.items() if "dropped" in k})
        return frame

    def check(self) -> SourceCheck:
        """Latest-trade probe for ``check_symbol``. Never raises."""
        symbol = str(self.cfg["check_symbol"])
        endpoint = self._url(f"stocks/{symbol}/trades/latest")
        extra: dict[str, Any] = {"feed": self.venue, "secret_source": self.credentials.secret_source}
        try:
            if not self.credentials.configured:
                return SourceCheck(SOURCE, False, endpoint, "keys not configured", extra=extra)
            resp = self._get_once(endpoint, {"feed": self.cfg["feed"]})
            extra["rate_limit"] = dict(self.rate_limit_seen)
            if resp.status_code != 200:
                return SourceCheck(SOURCE, False, endpoint, _status_detail(resp), extra=extra)
            trade = resp.json().get("trade") or {}
            extra["latest_trade_ts"] = trade.get("t")
            return SourceCheck(SOURCE, True, endpoint, "200 ok", extra=extra)
        except Exception as exc:  # noqa: BLE001 - check() must never raise
            return SourceCheck(SOURCE, False, endpoint, f"unreachable: {type(exc).__name__}", extra=extra)

    # ---- helpers for the CLI
    def earliest_trade(self, symbol: str, probe_start: datetime, end: datetime) -> str | None:
        """Timestamp of the first available trade in ``[probe_start, end)`` (history depth probe)."""
        url = self._url(f"stocks/{symbol}/trades")
        params = {"start": _rfc3339(probe_start), "end": _rfc3339(end), "limit": 1,
                  "feed": self.cfg["feed"], "sort": "asc"}
        rows = self._get(url, params).get("trades") or []
        return rows[0]["t"] if rows else None

    def has_trades(self, symbol: str, start: datetime, end: datetime) -> bool:
        """True if any trade exists in ``[start, end)`` (one request)."""
        url = self._url(f"stocks/{symbol}/trades")
        params = {"start": _rfc3339(start), "end": _rfc3339(end), "limit": 1, "feed": self.cfg["feed"]}
        return bool(self._get(url, params).get("trades"))


def _status_detail(resp: requests.Response) -> str:
    """Short, secret-free description of a non-200 response."""
    names = {401: "401 unauthorized", 403: "403 forbidden", 429: "429 rate limited"}
    detail = names.get(resp.status_code, f"{resp.status_code} {resp.reason or ''}".strip())
    try:
        msg = resp.json().get("message")
    except ValueError:
        msg = None
    return f"{detail}: {msg}" if msg else detail


# ------------------------------------------------------------------------------------------ RTH days
def rth_window(day: date, cfg: Mapping[str, Any]) -> tuple[datetime, datetime]:
    """UTC ``[open, close)`` of the regular session on ``day`` (exchange local time from config)."""
    tz = ZoneInfo(str(cfg["rth_tz"]))
    open_t = datetime.strptime(str(cfg["rth_open"]), "%H:%M").time()
    close_t = datetime.strptime(str(cfg["rth_close"]), "%H:%M").time()
    lo = datetime.combine(day, open_t, tzinfo=tz).astimezone(timezone.utc)
    hi = datetime.combine(day, close_t, tzinfo=tz).astimezone(timezone.utc)
    return lo, hi


def last_full_rth_day(loader: AlpacaLoader, symbol: str, now: datetime | None = None) -> date:
    """Most recent weekday whose session ran to the configured close (skips holidays/half-days).

    Evidence = at least one trade in the last ``full_day_probe_min`` minutes before close.
    """
    cfg = loader.cfg
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo(str(cfg["rth_tz"]))).date()
    probe = timedelta(minutes=float(cfg["full_day_probe_min"]))
    for _ in range(int(cfg["lookback_days"])):
        lo, hi = rth_window(day, cfg)
        if day.weekday() < 5 and hi <= now and loader.has_trades(symbol, hi - probe, hi):
            return day
        day -= timedelta(days=1)
    raise SourceUnavailable(f"no full RTH day found in the last {cfg['lookback_days']} days")


# ---------------------------------------------------------------------------------------------- CLI
def pull(
    loader: AlpacaLoader,
    symbols: list[str],
    kinds: list[str],
    day: date | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Fetch one RTH day for each symbol/kind into the store. Returns a JSON-safe summary."""
    day = day or last_full_rth_day(loader, symbols[0])
    lo, hi = rth_window(day, loader.cfg)
    summary: dict[str, Any] = {"date": day.isoformat(), "window_utc": [_rfc3339(lo), _rfc3339(hi)],
                               "feed": loader.venue, "results": []}
    for symbol in symbols:
        for kind in kinds:
            entry: dict[str, Any] = {"symbol": symbol, "kind": kind}
            try:
                frame = loader.fetch(kind, symbol, lo, hi)
                paths = storage.write_partitioned(frame, kind, root=root)
                entry.update(loader.last_stats, status="ok", files=[str(p) for p in paths])
                if not frame.empty:
                    entry.update(first_ts=str(frame["ts"].iloc[0]), last_ts=str(frame["ts"].iloc[-1]))
            except (SourceUnavailable, NotSupported, ValueError) as exc:
                entry.update(status="failed", reason=str(exc))
            summary["results"].append(entry)
            log.info("%s", json.dumps(entry))
    summary["rate_limit_headers"] = dict(loader.rate_limit_seen)
    return summary


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flow_model.data.loaders.alpaca", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="probe the API (never prints keys)")
    p_pull = sub.add_parser("pull", help="fetch one full RTH day into the store")
    p_pull.add_argument("--date", type=date.fromisoformat, default=None)
    p_pull.add_argument("--symbols", nargs="+", default=None)
    p_pull.add_argument("--kinds", nargs="+", default=["trades", "quotes"])
    p_pull.add_argument("--root", type=Path, default=None)
    p_depth = sub.add_parser("depth", help="earliest available trade per symbol on this plan/feed")
    p_depth.add_argument("--symbols", nargs="+", default=None)
    p_depth.add_argument("--since", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    loader = AlpacaLoader()
    check = loader.check()
    if args.cmd == "check" or not check.ok:
        print(json.dumps(check.to_dict(), indent=2))
        return 0 if check.ok else 1
    symbols = args.symbols or settings.list_instruments(asset_class="equities")
    if args.cmd == "pull":
        out = pull(loader, symbols, args.kinds, args.date, args.root)
    else:
        now = datetime.now(timezone.utc)
        since_day = args.since or date.fromisoformat(str(loader.cfg["depth_probe_start"]))
        since = datetime.combine(since_day, datetime.min.time(), tzinfo=timezone.utc)
        out = {s: loader.earliest_trade(s, since, now) for s in symbols}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
