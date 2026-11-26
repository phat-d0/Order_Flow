"""Daily yfinance 1m bar appender for every Tier C instrument (CL=F, GC=F, 6E=F).

Idempotent and safe to re-run or restart: for each symbol it fetches from (last stored bar −
``overlap_minutes``) — or ``initial_lookback_days`` on the first run — up to now, keeps only completed
bars, merges with what is stored (Yahoo's newer value wins on revised bars) and rewrites the affected
UTC-date files. Every run logs added rows, revisions, dropped rows, zero-volume bars and missing session
minutes to ``logs/bar_appender/`` (one JSON report per run + ``bar_appender.log``).

Yahoo keeps 1m bars for ~30 days, so run it at least weekly (daily is the plan). Cron example (UTC)::

    17 6 * * * cd /path/to/Order_Flow && .venv/bin/python -m flow_model.data.recorders.bar_appender >> logs/cron.log 2>&1

CLI: ``python -m flow_model.data.recorders.bar_appender [--symbols CL=F GC=F] [--dry-run]``.
Exit code 0 if every symbol succeeded, 1 if any failed (other symbols still run).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from flow_model.data import schema, storage
from flow_model.data.loaders.yfinance_bars import YFinanceBarsLoader, session_gaps
from flow_model.settings import REPO_ROOT, instrument, list_instruments, load_config

log = logging.getLogger("flow_model.bar_appender")


@dataclass
class SymbolResult:
    """Outcome of appending one symbol."""

    symbol: str
    status: str = "ok"                    # ok | failed
    error: str | None = None
    window: list[str] = field(default_factory=list)
    fetched: int = 0
    incomplete_skipped: int = 0
    added: int = 0
    revised: int = 0
    stored_total: int = 0
    first_ts: str | None = None
    last_ts: str | None = None
    files: list[str] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)
    zero_volume: int = 0
    zero_volume_after_gap: int = 0        # session-open bars Yahoo zeroed (see yfinance_bars docstring)
    zero_volume_kept_stored: int = 0      # refetched zeros that did not overwrite a stored positive volume
    gap_summary: dict[str, int] = field(default_factory=dict)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0
    warnings: list[str] = field(default_factory=list)


def _utc(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def tier_symbols(tier: str, source: str) -> list[str]:
    """Instruments of ``tier`` whose ``sources`` include ``source``."""
    return [s for s in list_instruments(tier=tier) if source in instrument(s)["sources"]]


def _merge(stored: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, int, int, int]:
    """Union on ``ts`` with ``new`` winning, except that a stored positive volume is never replaced by 0.

    Yahoo zeroes the volume of each trading day's first bar once that day is no longer the latest, so a
    later refetch of an overlap must not erase the value captured live. Returns (merged, rows added,
    rows revised, zero-volume overwrites refused).
    """
    value_cols = ["open", "high", "low", "close", "volume"]
    if stored.empty:
        return new.reset_index(drop=True), len(new), 0, 0
    new = new.copy()
    old_vol = new["ts"].map(stored.set_index("ts")["volume"])
    keep_old = (new["volume"] == 0) & (old_vol > 0)
    new.loc[keep_old, "volume"] = old_vol[keep_old]
    both = stored.merge(new, on="ts", suffixes=("_old", ""), how="inner")
    changed = pd.Series(False, index=both.index)
    for col in value_cols:
        changed |= ~((both[f"{col}_old"] == both[col]) | (both[f"{col}_old"].isna() & both[col].isna()))
    merged = pd.concat([stored.loc[~stored["ts"].isin(new["ts"])], new], ignore_index=True)
    merged = merged.sort_values("ts", kind="stable").reset_index(drop=True)
    return merged, len(merged) - len(stored), int(changed.sum()), int(keep_old.sum())


def append_symbol(
    loader: YFinanceBarsLoader, symbol: str, cfg: dict[str, Any], root: Path | None, dry_run: bool = False
) -> SymbolResult:
    """Fetch, merge and store new completed bars for one symbol."""
    app = cfg["appender"]
    res = SymbolResult(symbol=symbol)
    now = _utc(loader.now())
    dates = storage.list_dates(loader.source, symbol, "bars", root)
    if dates:
        last = storage.read_range(loader.source, symbol, "bars", start=pd.Timestamp(dates[-1]), root=root)["ts"].max()
        start = last - pd.Timedelta(minutes=app["overlap_minutes"])
    else:
        start = now - pd.Timedelta(days=app["initial_lookback_days"])
    res.window = [start.isoformat(), now.isoformat()]
    frame, report = loader.fetch_with_report("bars", symbol, start, now)
    res.requests, res.dropped, res.warnings = len(report.requests), report.dropped, report.warnings
    res.zero_volume, res.zero_volume_after_gap = report.n_zero_volume, report.n_zero_volume_after_gap
    cutoff = now - pd.Timedelta(minutes=cfg["interval_minutes"]) - pd.Timedelta(seconds=app["settle_lag_s"])
    incomplete = frame["ts"] > cutoff
    res.incomplete_skipped = int(incomplete.sum())
    frame = frame.loc[~incomplete].reset_index(drop=True)
    res.fetched = len(frame)

    session = cfg["sessions"][instrument(symbol)["session"]]
    gap_start = max(start, now - pd.Timedelta(days=cfg["lookback_days"]))
    gaps, summary = session_gaps(frame, gap_start, min(now, cutoff), session, cfg["interval_minutes"],
                                 app["gap_report_min_minutes"])
    res.gaps, res.gap_summary = report.gaps + gaps, summary

    if not frame.empty:
        days = sorted(frame["ts"].dt.date.unique())
        stored = storage.read_range(loader.source, symbol, "bars", start=pd.Timestamp(days[0]),
                                    end=pd.Timestamp(days[-1]) + pd.Timedelta(days=1), root=root)
        merged, res.added, res.revised, res.zero_volume_kept_stored = _merge(stored, frame)
        problems = schema.validate(merged, "bars")
        if problems:
            raise ValueError(f"{symbol}: merged bars invalid: {problems}")
        if not dry_run:
            res.files = [str(p) for p in storage.write_partitioned(merged, "bars", root=root, mode="overwrite")]
    if not dry_run:
        all_dates = storage.list_dates(loader.source, symbol, "bars", root)
        if all_dates:
            everything = storage.read_range(loader.source, symbol, "bars", root=root)
            res.stored_total = len(everything)
            res.first_ts, res.last_ts = everything["ts"].min().isoformat(), everything["ts"].max().isoformat()
    return res


def run(
    symbols: list[str] | None = None,
    root: Path | None = None,
    log_dir: Path | None = None,
    loader: YFinanceBarsLoader | None = None,
    dry_run: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Append every configured symbol; write and return the run report. Never raises per symbol."""
    cfg = load_config("sources")["yfinance"]
    loader = loader or YFinanceBarsLoader(cfg)
    symbols = symbols or tier_symbols(cfg["appender"]["tier"], loader.source)
    started = (clock or (lambda: datetime.now(timezone.utc)))()
    results = []
    for i, symbol in enumerate(symbols):
        if i:
            loader.sleep(cfg["request_pause_s"])
        try:
            res = append_symbol(loader, symbol, cfg, root, dry_run)
        except Exception as exc:  # noqa: BLE001 - one symbol failing must not stop the others
            res = SymbolResult(symbol=symbol, status="failed", error=f"{type(exc).__name__}: {exc}")
            log.error("%s failed: %s", symbol, res.error)
        else:
            log.info("%s: +%d bars (%d revised, %d incomplete skipped, %d zero-volume of which %d at session open, "
                     "%d stored volumes protected), stored %d [%s .. %s], "
                     "missing %d of %d session minutes, %d gaps >= %d min, dropped %s",
                     symbol, res.added, res.revised, res.incomplete_skipped, res.zero_volume, res.zero_volume_after_gap,
                     res.zero_volume_kept_stored, res.stored_total,
                     res.first_ts, res.last_ts, res.gap_summary.get("missing", 0),
                     res.gap_summary.get("expected", 0), len(res.gaps), cfg["appender"]["gap_report_min_minutes"],
                     res.dropped or "{}")
        results.append(asdict(res))
    report = {"started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
              "dry_run": dry_run, "symbols": results,
              "ok": all(r["status"] == "ok" for r in results)}
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"run_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(path)
    return report


def _setup_logging(log_dir: Path, cfg: dict[str, Any]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout),
                                       RotatingFileHandler(log_dir / "bar_appender.log", maxBytes=cfg["log_max_bytes"],
                                                           backupCount=cfg["log_backups"])]
    root_logger = logging.getLogger("flow_model")
    root_logger.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root_logger.addHandler(h)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Holds a lock file so overlapping cron runs cannot interleave."""
    parser = argparse.ArgumentParser(description="Append new yfinance 1m bars for Tier C instruments.")
    parser.add_argument("--symbols", nargs="*", help="default: every Tier C yfinance instrument")
    parser.add_argument("--root", type=Path, help="raw store root (default: sources.yaml storage.root)")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = parser.parse_args(argv)
    cfg = load_config("sources")["yfinance"]["appender"]
    log_dir = Path(cfg["log_dir"])
    log_dir = log_dir if log_dir.is_absolute() else REPO_ROOT / log_dir
    _setup_logging(log_dir, cfg)
    with open(log_dir / ".lock", "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("another bar_appender run holds %s; exiting", log_dir / ".lock")
            return 1
        report = run(args.symbols, root=args.root, log_dir=log_dir, dry_run=args.dry_run)
    log.info("run report: %s", report.get("report_path"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
