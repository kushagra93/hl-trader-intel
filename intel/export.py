"""
Export the latest immutable snapshot to a single JSON file the static dashboard
reads. No secrets, read-only. The dashboard never talks to the DB directly.
"""
from __future__ import annotations

import json
import logging

from . import storage
from .config import IntelConfig
from .crowding import concentration_top3

log = logging.getLogger("intel.export")


def export_latest(cfg: IntelConfig | None = None) -> str:
    cfg = cfg or IntelConfig()
    conn = storage.connect(cfg.db_path)
    try:
        snap_id = storage.latest_snapshot_id(conn)
        if not snap_id:
            raise SystemExit("No snapshot to export. Run the ranker first.")
        snap = dict(conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snap_id,)).fetchone())

        ranks = [dict(r) for r in conn.execute(
            "SELECT * FROM trader_ranks WHERE snapshot_id=? ORDER BY rank_unrealized", (snap_id,))]
        ranks = ranks[: snap["top_n"]]

        positions: dict[str, list] = {}
        for r in conn.execute("SELECT * FROM trader_positions WHERE snapshot_id=?", (snap_id,)):
            positions.setdefault(r["address"], []).append(dict(r))

        market = [dict(r) for r in conn.execute(
            "SELECT * FROM market_ctx WHERE snapshot_id=? ORDER BY CAST(day_ntl_vlm AS REAL) DESC", (snap_id,))]
        crowding = [dict(r) for r in conn.execute(
            "SELECT * FROM agg_crowding WHERE snapshot_id=?", (snap_id,))]

        payload = {
            "snapshot": snap,
            "concentration": concentration_top3(crowding),
            "traders": ranks,
            "positions": positions,
            "market": market,
            "crowding": crowding,
            "disclaimer": (
                "Positions shown are public on-chain data for informational purposes only. "
                "This is not financial advice, a signal, or a recommendation. Following lagged "
                "positions is not a strategy: entries are seen late and exits are never shown. "
                "Realized PnL is unavailable (no upstream fill history); figures are unrealized "
                "PnL plus funding over the window, marked with their basis."
            ),
        }
        with open(cfg.export_path, "w") as f:
            json.dump(payload, f, indent=2)
        log.info("exported snapshot %s -> %s (%d traders)", snap_id, cfg.export_path, len(ranks))
        return cfg.export_path
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(export_latest())
