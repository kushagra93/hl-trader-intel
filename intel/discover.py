"""
Address discovery for the HIP-3 dex via the public recentTrades feed.

There is no info endpoint that lists "all users of a dex". But recentTrades for
each coin returns users:[buyer, seller] per trade, so polling recentTrades
across the dex universe harvests real participant addresses (and their recent
fills) into SQLite. Run it on a schedule (n8n / cron) to keep the set fresh.

This is a lightweight, public-data participant collector — NOT a rebuild of an
L1 block ingester. It only sees recent on-chain trades, which is enough to seed
and continuously refresh the ranker's address set.

CLI:
  python -m intel.discover --once                 # one sweep over all coins
  python -m intel.discover --duration 120         # sweep repeatedly for 120s
  python -m intel.discover --once --max-coins 20  # limit universe (testing)
"""
from __future__ import annotations

import argparse
import logging
import time

from . import storage
from .config import IntelConfig
from .hl_intel_client import HLIntelClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("intel.discover")


def universe_coins(client: HLIntelClient, dex: str, max_coins: int | None = None) -> list[str]:
    meta, _ctxs = client.meta_and_asset_ctxs(dex)
    coins = [c["name"] for c in (meta or {}).get("universe", []) if not c.get("isDelisted")]
    return coins[:max_coins] if max_coins else coins


def sweep_once(client: HLIntelClient, conn, coins: list[str]) -> tuple[int, int]:
    """One pass over all coins. Returns (addresses_touched, fills_seen)."""
    seen_ms = storage.now_ms()
    part_rows: list[tuple] = []
    fill_rows: list[tuple] = []
    for coin in coins:
        try:
            trades = client.recent_trades(coin) or []
        except RuntimeError as e:
            log.warning("recentTrades(%s) failed: %s", coin, e)
            continue
        for t in trades:
            users = t.get("users") or []
            tid = t.get("tid")
            t_time = int(t.get("time", seen_ms))
            for addr in users:
                if not addr:
                    continue
                a = addr.lower()
                part_rows.append((a, t_time, t_time))
                if tid is not None:
                    fill_rows.append((tid, a, coin, t.get("side"), str(t.get("px")),
                                      str(t.get("sz")), t_time, t.get("hash")))
    if part_rows:
        storage.upsert_participants(conn, part_rows)
    if fill_rows:
        storage.insert_fills(conn, fill_rows)
    conn.commit()
    distinct = len({r[0] for r in part_rows})
    return distinct, len(fill_rows)


def run(duration: int = 0, once: bool = True, max_coins: int | None = None,
        cfg: IntelConfig | None = None) -> dict:
    cfg = cfg or IntelConfig()
    storage.init_schema(cfg.db_path)
    client = HLIntelClient(rps=cfg.rps, burst=cfg.burst, concurrency=cfg.concurrency,
                           max_retries=cfg.max_retries, verify_tls=cfg.verify_tls)
    conn = storage.connect(cfg.db_path)
    try:
        coins = universe_coins(client, cfg.dex, max_coins)
        log.info("discovering across %d %s coins", len(coins), cfg.dex)
        sweeps = 0
        total_addr = total_fills = 0
        deadline = time.monotonic() + duration
        while True:
            a, f = sweep_once(client, conn, coins)
            sweeps += 1
            total_addr += a
            total_fills += f
            total_known = conn.execute("SELECT COUNT(*) c FROM xyz_participants").fetchone()["c"]
            log.info("sweep %d: +%d addr touched, +%d fills | %d known participants",
                     sweeps, a, f, total_known)
            if once or time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        known = conn.execute("SELECT COUNT(*) c FROM xyz_participants").fetchone()["c"]
        return {"sweeps": sweeps, "fills_seen": total_fills, "known_participants": known}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Discover HIP-3 dex participants via recentTrades.")
    ap.add_argument("--once", action="store_true", help="single sweep then exit")
    ap.add_argument("--duration", type=int, default=0, help="seconds to keep sweeping")
    ap.add_argument("--max-coins", type=int, default=None, help="limit universe size (testing)")
    args = ap.parse_args()
    once = args.once or args.duration <= 0
    result = run(duration=args.duration, once=once, max_coins=args.max_coins)
    log.info("discovery done: %s", result)


if __name__ == "__main__":
    main()
