"""
SQLite storage for the intel layer.

Two groups of tables, all owned by THIS layer (we never write the collector's
tables):
  - discovery: xyz_participants, xyz_fills  (populated by intel.discover)
  - snapshots: snapshots, trader_ranks, trader_positions, market_ctx, agg_crowding

Snapshots are immutable: each ranker run inserts a new snapshot_id row-set and
never mutates prior ones. The dashboard always reads the latest snapshot_id.

Money/size values are stored as TEXT (Decimal canonical form) — never as REAL —
so no float drift survives a round-trip.
"""
from __future__ import annotations

import sqlite3
import time

DDL = """
PRAGMA journal_mode=WAL;

-- ---- discovery (this layer's lightweight participant/fill capture) ----
CREATE TABLE IF NOT EXISTS xyz_participants (
  address     TEXT PRIMARY KEY,
  first_seen  INTEGER NOT NULL,   -- ms
  last_seen   INTEGER NOT NULL,   -- ms
  trade_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS xyz_fills (
  tid       INTEGER NOT NULL,
  address   TEXT NOT NULL,
  coin      TEXT NOT NULL,
  side      TEXT,                 -- B | A (as reported in the trade)
  px        TEXT NOT NULL,
  sz        TEXT NOT NULL,
  time      INTEGER NOT NULL,     -- ms
  hash      TEXT,
  PRIMARY KEY (tid, address)
);
CREATE INDEX IF NOT EXISTS xyz_fills_addr_time ON xyz_fills(address, time);

-- ---- immutable ranked snapshots ----
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id   TEXT PRIMARY KEY,
  created_at    INTEGER NOT NULL,   -- ms
  dex           TEXT NOT NULL,
  window        TEXT NOT NULL,
  top_n         INTEGER NOT NULL,
  address_count INTEGER NOT NULL,
  realized_basis TEXT NOT NULL,     -- global note, e.g. "unavailable"
  dry_run       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trader_ranks (
  snapshot_id    TEXT NOT NULL,
  address        TEXT NOT NULL,
  rank_unrealized INTEGER,
  rank_roi        INTEGER,
  rank_realized   INTEGER,          -- null while realized unavailable
  unrealized_pnl  TEXT,
  realized_pnl    TEXT,             -- null while unavailable
  funding_window  TEXT,             -- signed funding flow over window (xyz coins only)
  net_pnl_window  TEXT,             -- unrealized + funding (labeled basis)
  roi_pct         TEXT,
  account_value   TEXT,
  vol_window      TEXT,             -- notional traded over window from captured fills
  realized_basis  TEXT NOT NULL,    -- exact | reconstructed | unavailable
  n_positions     INTEGER,
  PRIMARY KEY (snapshot_id, address)
);
CREATE INDEX IF NOT EXISTS trader_ranks_snap ON trader_ranks(snapshot_id);

CREATE TABLE IF NOT EXISTS trader_positions (
  snapshot_id    TEXT NOT NULL,
  address        TEXT NOT NULL,
  coin           TEXT NOT NULL,
  szi            TEXT,
  entry_px       TEXT,
  position_value TEXT,
  unrealized_pnl TEXT,
  roe            TEXT,
  leverage_value TEXT,
  liq_px         TEXT,
  margin_used    TEXT,
  funding_paid_window TEXT,
  PRIMARY KEY (snapshot_id, address, coin)
);
CREATE INDEX IF NOT EXISTS trader_positions_snap ON trader_positions(snapshot_id);

CREATE TABLE IF NOT EXISTS market_ctx (
  snapshot_id  TEXT NOT NULL,
  coin         TEXT NOT NULL,
  mark_px      TEXT,
  oracle_px    TEXT,
  funding      TEXT,
  open_interest TEXT,
  day_ntl_vlm  TEXT,
  at_oi_cap    INTEGER NOT NULL DEFAULT 0,
  sz_decimals  INTEGER,
  category     TEXT,
  PRIMARY KEY (snapshot_id, coin)
);

CREATE TABLE IF NOT EXISTS agg_crowding (
  snapshot_id  TEXT NOT NULL,
  coin         TEXT NOT NULL,
  n_long       INTEGER NOT NULL,
  n_short      INTEGER NOT NULL,
  net_notional TEXT,
  pct_long     TEXT,
  n_fresh      INTEGER NOT NULL DEFAULT 0,   -- positions opened within window
  funding      TEXT,
  long_into_negative_funding INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (snapshot_id, coin)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(DDL)
        conn.commit()
    finally:
        conn.close()


def now_ms() -> int:
    return int(time.time() * 1000)


# ---- discovery writers ----

def upsert_participants(conn, rows):
    """rows: iterable of (address, seen_ms). Maintains first/last seen + count."""
    conn.executemany(
        """
        INSERT INTO xyz_participants(address, first_seen, last_seen, trade_count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(address) DO UPDATE SET
          last_seen = MAX(last_seen, excluded.last_seen),
          first_seen = MIN(first_seen, excluded.first_seen),
          trade_count = trade_count + 1
        """,
        list(rows),
    )


def insert_fills(conn, rows):
    """rows: iterable of (tid, address, coin, side, px, sz, time, hash)."""
    conn.executemany(
        """
        INSERT OR IGNORE INTO xyz_fills(tid, address, coin, side, px, sz, time, hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        list(rows),
    )


def get_participants(conn, limit: int | None = None):
    q = "SELECT address FROM xyz_participants ORDER BY last_seen DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [r["address"] for r in conn.execute(q)]


def vol_window_by_address(conn, coin_prefix: str, since_ms: int):
    """Sum |px*sz| per address over captured xyz fills since since_ms."""
    rows = conn.execute(
        """
        SELECT address, px, sz FROM xyz_fills
        WHERE time >= ? AND coin LIKE ?
        """,
        (since_ms, coin_prefix + "%"),
    ).fetchall()
    from .money import D
    acc: dict[str, "object"] = {}
    for r in rows:
        acc[r["address"]] = acc.get(r["address"], D(0)) + abs(D(r["px"]) * D(r["sz"]))
    return acc


# ---- snapshot readers ----

def latest_snapshot_id(conn) -> str | None:
    row = conn.execute("SELECT snapshot_id FROM snapshots ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["snapshot_id"] if row else None
