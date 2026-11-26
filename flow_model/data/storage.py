"""Partitioned parquet store: ``<root>/<source>/<symbol>/<kind>/<YYYY-MM-DD>.parquet`` (UTC dates)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from flow_model.data import schema
from flow_model.settings import storage_root

_UNSAFE = str.maketrans({"=": "_", "/": "_", "\\": "_", ":": "_"})


def safe_symbol(symbol: str) -> str:
    """Filesystem-safe form of a symbol (``CL=F`` -> ``CL_F``)."""
    return symbol.translate(_UNSAFE)


def partition_dir(source: str, symbol: str, kind: str, root: Path | None = None) -> Path:
    """Directory holding the daily files for one source/symbol/kind."""
    return Path(root or storage_root()) / source / safe_symbol(symbol) / kind


def _dedupe(df: pd.DataFrame) -> pd.DataFrame:
    keys = [c for c in df.columns if c != "recv_ts"]
    return df.drop_duplicates(subset=keys, keep="first").sort_values("ts", kind="stable")


def write_partitioned(
    df: pd.DataFrame, kind: str, root: Path | None = None, mode: str = "append"
) -> list[Path]:
    """Validate ``df`` and write one parquet file per (source, symbol, UTC date).

    ``mode="append"`` merges with an existing file and de-duplicates on every column except
    ``recv_ts``; ``mode="overwrite"`` replaces it. Returns the files written.
    """
    if mode not in ("append", "overwrite"):
        raise ValueError("mode must be 'append' or 'overwrite'")
    if df.empty:
        return []
    frame = schema.coerce(df, kind).sort_values("ts", kind="stable")
    problems = schema.validate(frame.reset_index(drop=True), kind)
    if problems:
        raise ValueError(f"refusing to write invalid {kind}: {problems}")
    written: list[Path] = []
    groups = frame.groupby([frame["source"], frame["symbol"], frame["ts"].dt.date], sort=True)
    for (source, symbol, day), part in groups:
        path = partition_dir(source, symbol, kind, root) / f"{day.isoformat()}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append" and path.exists():
            part = pd.concat([_read_file(path, kind), part], ignore_index=True)
        part = _dedupe(part).reset_index(drop=True)
        pq.write_table(schema.to_table(part, kind), path)
        written.append(path)
    return written


def _read_file(path: Path, kind: str) -> pd.DataFrame:
    return schema.coerce(pq.read_table(path).to_pandas(), kind)


def list_dates(source: str, symbol: str, kind: str, root: Path | None = None) -> list[date]:
    """UTC dates for which a file exists, ascending."""
    directory = partition_dir(source, symbol, kind, root)
    if not directory.exists():
        return []
    return sorted(date.fromisoformat(p.stem) for p in directory.glob("*.parquet"))


def _as_utc(value: datetime | str | pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def read_range(
    source: str,
    symbol: str,
    kind: str,
    start: datetime | str | None = None,
    end: datetime | str | None = None,
    root: Path | None = None,
) -> pd.DataFrame:
    """Rows with ``start <= ts < end`` (naive inputs are taken as UTC). Empty frame if none."""
    lo, hi = _as_utc(start), _as_utc(end)
    days = [
        d
        for d in list_dates(source, symbol, kind, root)
        if (lo is None or d >= lo.date()) and (hi is None or d <= hi.date())
    ]
    if not days:
        return schema.empty_frame(kind)
    directory = partition_dir(source, symbol, kind, root)
    frame = pd.concat([_read_file(directory / f"{d}.parquet", kind) for d in days], ignore_index=True)
    mask = pd.Series(True, index=frame.index)
    if lo is not None:
        mask &= frame["ts"] >= lo
    if hi is not None:
        mask &= frame["ts"] < hi
    return frame.loc[mask].sort_values("ts", kind="stable").reset_index(drop=True)
