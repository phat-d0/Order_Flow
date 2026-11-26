"""Common market-data schemas. Every loader outputs exactly these (see docs/CONTRACTS.md §1.1).

Timestamps are ``timestamp[ns, tz=UTC]``. ``side`` is +1 buyer-initiated, -1 seller-initiated, 0 unknown.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

TS = pa.timestamp("ns", tz="UTC")
KINDS: tuple[str, ...] = ("trades", "quotes", "l2", "bars")

_COMMON = [
    pa.field("ts", TS, nullable=False),
    pa.field("symbol", pa.string(), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("venue", pa.string(), nullable=False),
]

SCHEMAS: dict[str, pa.Schema] = {
    "trades": pa.schema(
        _COMMON
        + [
            pa.field("price", pa.float64(), nullable=False),
            pa.field("size", pa.float64(), nullable=False),
            pa.field("side", pa.int8(), nullable=False),
            pa.field("trade_id", pa.string()),
            pa.field("conditions", pa.string()),  # source condition codes, comma-joined; null if none
            pa.field("recv_ts", TS),
        ]
    ),
    "quotes": pa.schema(
        _COMMON
        + [
            pa.field("bid_px", pa.float64(), nullable=False),
            pa.field("bid_sz", pa.float64()),
            pa.field("ask_px", pa.float64(), nullable=False),
            pa.field("ask_sz", pa.float64()),
            pa.field("recv_ts", TS),
        ]
    ),
    "l2": pa.schema(
        _COMMON
        + [
            pa.field("book_side", pa.int8(), nullable=False),
            pa.field("price", pa.float64(), nullable=False),
            pa.field("size", pa.float64(), nullable=False),
            pa.field("is_snapshot", pa.bool_(), nullable=False),
            pa.field("recv_ts", TS),
        ]
    ),
    "bars": pa.schema(
        _COMMON
        + [
            pa.field("interval", pa.string(), nullable=False),
            pa.field("open", pa.float64(), nullable=False),
            pa.field("high", pa.float64(), nullable=False),
            pa.field("low", pa.float64(), nullable=False),
            pa.field("close", pa.float64(), nullable=False),
            pa.field("volume", pa.float64(), nullable=False),
        ]
    ),
}

_TS_COLS = ("ts", "recv_ts")
_INT8_COLS = ("side", "book_side")
_PRICE_COLS = ("price", "bid_px", "ask_px", "open", "high", "low", "close")


def _check_kind(kind: str) -> pa.Schema:
    if kind not in SCHEMAS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    return SCHEMAS[kind]


def columns(kind: str) -> list[str]:
    """Column names of ``kind`` in schema order."""
    return _check_kind(kind).names


def empty_frame(kind: str) -> pd.DataFrame:
    """A zero-row frame with the exact dtypes of ``kind``."""
    return _check_kind(kind).empty_table().to_pandas()


def _to_utc_ns(values: pd.Series) -> pd.Series:
    out = pd.to_datetime(values, utc=True)
    return out.astype("datetime64[ns, UTC]")


def coerce(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    """Return ``df`` with schema columns in order, dtypes cast, missing nullable columns filled.

    Raises ``ValueError`` if a non-nullable column is missing.
    """
    schema = _check_kind(kind)
    out = pd.DataFrame(index=df.index)
    for field in schema:
        name = field.name
        if name not in df.columns:
            if not field.nullable:
                raise ValueError(f"{kind}: required column {name!r} missing")
            out[name] = pd.Series(pd.NA, index=df.index, dtype="object")
        else:
            out[name] = df[name]
        if name in _TS_COLS:
            out[name] = _to_utc_ns(out[name])
        elif name in _INT8_COLS:
            out[name] = out[name].astype("int8")
        elif pa.types.is_floating(field.type):
            out[name] = pd.to_numeric(out[name], errors="coerce").astype("float64")
        elif pa.types.is_boolean(field.type):
            out[name] = out[name].astype(bool)
        elif pa.types.is_string(field.type):
            mask = out[name].isna()
            out[name] = out[name].astype("string")
            out.loc[mask, name] = pd.NA
    return out.reset_index(drop=True)


def to_table(df: pd.DataFrame, kind: str) -> pa.Table:
    """Coerce and convert to a pyarrow table that matches the schema exactly."""
    return pa.Table.from_pandas(coerce(df, kind), schema=SCHEMAS[kind], preserve_index=False)


def validate(df: pd.DataFrame, kind: str) -> list[str]:
    """Check ``df`` against the ``kind`` contract. Returns a list of problems (empty = valid)."""
    schema = _check_kind(kind)
    problems = [f"missing column {c!r}" for c in schema.names if c not in df.columns]
    if problems:
        return problems
    for field in schema:
        col = df[field.name]
        if not field.nullable and col.isna().any():
            problems.append(f"nulls in non-nullable column {field.name!r}")
        if field.name in _TS_COLS and not (
            isinstance(col.dtype, pd.DatetimeTZDtype) and str(col.dtype.tz) == "UTC"
        ):
            problems.append(f"{field.name!r} must be tz-aware UTC, got {col.dtype}")
    if problems:
        return problems
    if not df["ts"].is_monotonic_increasing:
        problems.append("'ts' is not sorted ascending")
    for name in _INT8_COLS:
        if name in df.columns and not df[name].isin([-1, 0, 1]).all():
            problems.append(f"{name!r} has values outside {{-1, 0, 1}}")
    if kind == "l2" and (df["book_side"] == 0).any():
        problems.append("'book_side' must be +1 or -1")
    for name in _PRICE_COLS:
        if name in df.columns and (df[name] <= 0).any():
            problems.append(f"non-positive values in {name!r}")
    if kind == "quotes" and (df["bid_px"] > df["ask_px"]).any():
        problems.append("crossed quotes: bid_px > ask_px")
    if kind == "bars":
        bad = (df["high"] < df[["open", "close", "low"]].max(axis=1)) | (
            df["low"] > df[["open", "close"]].min(axis=1)
        )
        if bad.any():
            problems.append("bars with high/low inconsistent with open/close")
    return problems
