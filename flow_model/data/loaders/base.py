"""Loader interface every data source implements (docs/CONTRACTS.md §1.3).

Adding a paid feed later = one new ``Loader`` subclass + a config entry. ``core/`` never changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from flow_model.data import schema, storage


class NotSupported(Exception):
    """The source does not provide this kind of data (e.g. quotes from a bar-only source)."""


class SourceUnavailable(Exception):
    """The source could not be reached or refused the request (network, auth, geo-block, limit)."""


@dataclass
class SourceCheck:
    """Result of a live reachability probe, written into the data sources log."""

    source: str
    ok: bool
    endpoint: str
    detail: str
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return asdict(self)


class Loader(ABC):
    """Base class for all market-data sources."""

    source: str = ""
    venue: str = ""
    tier: str = ""
    kinds: frozenset[str] = frozenset()

    @abstractmethod
    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Download ``[start, end)`` from the source as a schema-valid frame.

        Raises ``NotSupported`` if ``kind`` is not offered and ``SourceUnavailable`` on access errors.
        """

    @abstractmethod
    def check(self) -> SourceCheck:
        """Probe the source from this machine. Must never raise."""

    def _require(self, kind: str) -> None:
        if kind not in self.kinds:
            raise NotSupported(f"{self.source} does not provide {kind!r}")

    def load(
        self,
        kind: str,
        symbol: str,
        start: datetime,
        end: datetime,
        root: Path | None = None,
        fetch_missing: bool = True,
    ) -> pd.DataFrame:
        """Read ``[start, end)`` from the local store, fetching and storing missing UTC dates first.

        A date that already has a file is not refetched (even if partial, e.g. today); to refresh,
        call ``fetch`` and ``storage.write_partitioned`` directly (append mode de-duplicates).
        """
        self._require(kind)
        if fetch_missing:
            have = set(storage.list_dates(self.source, symbol, kind, root))
            for day_start in _days(start, end):
                if day_start.date() not in have:
                    day_end = min(day_start + timedelta(days=1), _utc(end))
                    frame = self.fetch(kind, symbol, max(day_start, _utc(start)), day_end)
                    storage.write_partitioned(frame, kind, root=root)
        frame = storage.read_range(self.source, symbol, kind, start, end, root=root)
        problems = schema.validate(frame, kind)
        if problems:
            raise ValueError(f"{self.source}/{symbol}/{kind} invalid: {problems}")
        return frame


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _days(start: datetime, end: datetime) -> list[datetime]:
    """Midnight-UTC day starts covering ``[start, end)``."""
    lo, hi = _utc(start), _utc(end)
    day = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    while day < hi:
        out.append(day)
        day += timedelta(days=1)
    return out
