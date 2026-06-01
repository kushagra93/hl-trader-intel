"""
Address sources for the ranker.

The original spec assumed an upstream L1 collector exposing a participant set.
That collector is not present in this repo, so the ranker reads addresses via a
pluggable AddressSource. Two implementations ship today:

  - SqliteParticipantsSource: reads the xyz_participants table that intel.discover
    populates from the public recentTrades feed (real, live addresses).
  - SeedListSource: reads a flat file of addresses, for --dry-run / testing.

If a "real" upstream collector appears later, add a third implementation and
point the ranker at it — nothing else changes.
"""
from __future__ import annotations

import os
import re
from typing import Protocol

_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _valid(addr: str) -> bool:
    return bool(_ADDR_RE.match(addr.strip()))


class AddressSource(Protocol):
    def addresses(self, limit: int | None = None) -> list[str]:
        ...


class SeedListSource:
    def __init__(self, path: str):
        self.path = path

    def addresses(self, limit: int | None = None) -> list[str]:
        if not os.path.exists(self.path):
            return []
        out: list[str] = []
        with open(self.path) as f:
            for line in f:
                a = line.strip().lower()
                if a and not a.startswith("#") and _valid(a):
                    out.append(a)
        # de-dupe preserving order
        seen, uniq = set(), []
        for a in out:
            if a not in seen:
                seen.add(a)
                uniq.append(a)
        return uniq[:limit] if limit else uniq


class SqliteParticipantsSource:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def addresses(self, limit: int | None = None) -> list[str]:
        from . import storage
        conn = storage.connect(self.db_path)
        try:
            return storage.get_participants(conn, limit)
        finally:
            conn.close()
