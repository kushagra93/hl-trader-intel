"""
Ranker: scheduled, idempotent leaderboard snapshots for the HIP-3 dex.

Reads the participant address set (pluggable source), fetches each address's
positions (clearinghouseState, dex-scoped) and funding (userFunding, filtered to
the dex namespace), computes multiple ranking definitions, and writes an
immutable timestamped snapshot to SQLite.

Ranking definitions implemented:
  1. Unrealized PnL        — exact, sum of unrealizedPnl over dex positions.
  2. ROI %                 — margin-based ROE: sum(unrealizedPnl)/sum(marginUsed).
  3. Realized PnL          — UNAVAILABLE here (no per-address fill history stored
                             upstream). Surfaced as null with basis="unavailable".
  4. Net (unreal+funding)  — unrealized + funding flow over window (labeled).
  5. Volume-weighted       — notional traded over window from captured fills
                             (conviction signal, not profit).

Every PnL figure carries a `basis` tag: "exact" (from API) or "unavailable".

CLI:
  python -m intel.ranker --dry-run --top-n 25 --window 7d --export
  python -m intel.ranker --window 24h --top-n 100 --export
"""
from __future__ import annotations

import argparse
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import storage
from .config import IntelConfig
from .crowding import compute as compute_crowding
from .hl_intel_client import HLIntelClient
from .money import D, ZERO, to_str, safe_div
from .participants import SeedListSource, SqliteParticipantsSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("intel.ranker")


def _market_context(client: HLIntelClient, cfg: IntelConfig):
    """coin -> ctx map (zipped positionally), universe meta, OI-cap set."""
    meta, ctxs = client.meta_and_asset_ctxs(cfg.dex)
    universe = (meta or {}).get("universe", [])
    coin_ctx: dict[str, dict] = {}
    sz_dec: dict[str, int] = {}
    for i, u in enumerate(universe):
        if u.get("isDelisted"):
            continue
        name = u.get("name")
        ctx = ctxs[i] if i < len(ctxs) else {}
        coin_ctx[name] = {
            "mark_px": D(ctx.get("markPx")),
            "oracle_px": D(ctx.get("oraclePx")),
            "funding": D(ctx.get("funding")),
            "open_interest": D(ctx.get("openInterest")),
            "day_ntl_vlm": D(ctx.get("dayNtlVlm")),
        }
        sz_dec[name] = u.get("szDecimals")
    capped = set(client.perps_at_oi_cap(cfg.dex) or [])
    return coin_ctx, sz_dec, capped


def _evaluate_address(client: HLIntelClient, cfg: IntelConfig, addr: str,
                      since_ms: int, now_ms: int, fresh_coins: set[str]) -> dict:
    """Fetch + reduce one address into a record (no DB writes here)."""
    ch = client.clearinghouse_state(addr, cfg.dex)
    positions = []
    sum_unreal = ZERO
    sum_margin = ZERO
    prefix = cfg.coin_prefix()
    for ap in (ch or {}).get("assetPositions", []):
        p = ap.get("position") or {}
        coin = p.get("coin")
        if not coin or not coin.startswith(prefix):
            continue  # defensive: only this dex's coins
        szi = D(p.get("szi"))
        upnl = D(p.get("unrealizedPnl"))
        margin = D(p.get("marginUsed"))
        pv = D(p.get("positionValue"))
        lev = p.get("leverage") or {}
        sum_unreal += upnl
        sum_margin += margin
        positions.append({
            "coin": coin, "szi": szi, "position_value": pv,
            "unrealized_pnl": upnl, "roe": D(p.get("returnOnEquity")),
            "leverage_value": D(lev.get("value")) if isinstance(lev, dict) else ZERO,
            "liq_px": p.get("liquidationPx"), "margin_used": margin,
            "fresh": (addr, coin) in fresh_coins,
        })

    # Funding over window — userFunding is NOT dex-scoped, filter to dex coins.
    funding_window = ZERO
    try:
        deltas = client.user_funding(addr, since_ms, now_ms)
        for d in deltas:
            dd = d.get("delta", {})
            if str(dd.get("coin", "")).startswith(prefix):
                funding_window += D(dd.get("usdc"))
    except RuntimeError as e:
        log.warning("userFunding(%s) failed: %s", addr, e)

    roi = safe_div(sum_unreal, sum_margin)
    account_value = (ch or {}).get("marginSummary", {}).get("accountValue")
    return {
        "address": addr,
        "positions": positions,
        "unrealized_pnl": sum_unreal,
        "funding_window": funding_window,
        "net_pnl_window": sum_unreal + funding_window,
        "roi_pct": (roi * 100) if roi is not None else None,
        "account_value": D(account_value),
        "n_positions": len(positions),
    }


def run_ranker(cfg: IntelConfig | None = None, dry_run: bool = False,
               limit_addrs: int | None = None, export: bool = False) -> dict:
    cfg = cfg or IntelConfig()
    storage.init_schema(cfg.db_path)
    client = HLIntelClient(rps=cfg.rps, burst=cfg.burst, concurrency=cfg.concurrency,
                           max_retries=cfg.max_retries, verify_tls=cfg.verify_tls)
    conn = storage.connect(cfg.db_path)

    # ---- address set ----
    if dry_run:
        src = SeedListSource(cfg.seed_path)
        addresses = src.addresses(limit_addrs)
        if not addresses:  # fall back to discovered set so dry-run still has data
            addresses = SqliteParticipantsSource(cfg.db_path).addresses(limit_addrs or 25)
            log.info("seed list empty; using %d discovered participants for dry-run", len(addresses))
    else:
        addresses = SqliteParticipantsSource(cfg.db_path).addresses(limit_addrs)
    if not addresses:
        raise SystemExit("No addresses to rank. Run `python -m intel.discover --once` first, "
                         "or add addresses to " + cfg.seed_path)

    now_ms = storage.now_ms()
    window_ms = cfg.window_ms()
    since_ms = 0 if window_ms is None else now_ms - window_ms

    # Coins each address was recently active in (capture-window proxy for "fresh").
    fresh_coins = _fresh_pairs(conn, cfg.coin_prefix(), since_ms)
    vol_by_addr = storage.vol_window_by_address(conn, cfg.coin_prefix(), since_ms)

    coin_ctx, sz_dec, capped = _market_context(client, cfg)

    # ---- fan out across addresses (throttled by the client) ----
    log.info("ranking %d addresses on dex=%s window=%s", len(addresses), cfg.dex, cfg.window)
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        futs = {ex.submit(_evaluate_address, client, cfg, a, since_ms, now_ms, fresh_coins): a
                for a in addresses}
        for fut in as_completed(futs):
            a = futs[fut]
            try:
                records.append(fut.result())
            except RuntimeError as e:
                log.warning("address %s failed: %s", a, e)

    # ---- rankings ----
    by_unreal = sorted(records, key=lambda r: r["unrealized_pnl"], reverse=True)
    rank_unreal = {r["address"]: i + 1 for i, r in enumerate(by_unreal)}
    roi_ranked = sorted([r for r in records if r["roi_pct"] is not None],
                        key=lambda r: r["roi_pct"], reverse=True)
    rank_roi = {r["address"]: i + 1 for i, r in enumerate(roi_ranked)}

    # Top-N (headline = unrealized) drives the displayed leaderboard + crowding.
    top = by_unreal[:cfg.top_n]
    for r in records:
        r["vol_window"] = vol_by_addr.get(r["address"], ZERO)

    # ---- write immutable snapshot ----
    snap_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO snapshots(snapshot_id, created_at, dex, window, top_n, address_count, realized_basis, dry_run) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (snap_id, now_ms, cfg.dex, cfg.window, cfg.top_n, len(records), "unavailable", int(dry_run)),
    )
    conn.executemany(
        "INSERT INTO trader_ranks(snapshot_id,address,rank_unrealized,rank_roi,rank_realized,"
        "unrealized_pnl,realized_pnl,funding_window,net_pnl_window,roi_pct,account_value,vol_window,"
        "realized_basis,n_positions) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(
            snap_id, r["address"], rank_unreal.get(r["address"]), rank_roi.get(r["address"]), None,
            to_str(r["unrealized_pnl"]), None, to_str(r["funding_window"]), to_str(r["net_pnl_window"]),
            to_str(r["roi_pct"]) if r["roi_pct"] is not None else None, to_str(r["account_value"]),
            to_str(r["vol_window"]), "exact", r["n_positions"],
        ) for r in records],
    )
    # positions only for displayed top-N (bounds snapshot size)
    pos_rows = []
    for r in top:
        for p in r["positions"]:
            pos_rows.append((
                snap_id, r["address"], p["coin"], to_str(p["szi"]), None,
                to_str(p["position_value"]), to_str(p["unrealized_pnl"]), to_str(p["roe"]),
                to_str(p["leverage_value"]), p["liq_px"], to_str(p["margin_used"]), None,
            ))
    conn.executemany(
        "INSERT INTO trader_positions(snapshot_id,address,coin,szi,entry_px,position_value,"
        "unrealized_pnl,roe,leverage_value,liq_px,margin_used,funding_paid_window) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", pos_rows,
    )
    conn.executemany(
        "INSERT INTO market_ctx(snapshot_id,coin,mark_px,oracle_px,funding,open_interest,"
        "day_ntl_vlm,at_oi_cap,sz_decimals,category) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(
            snap_id, coin, to_str(c["mark_px"]), to_str(c["oracle_px"]), to_str(c["funding"]),
            to_str(c["open_interest"]), to_str(c["day_ntl_vlm"]), int(coin in capped),
            sz_dec.get(coin), None,
        ) for coin, c in coin_ctx.items()],
    )
    crowding = compute_crowding(top, coin_ctx)
    conn.executemany(
        "INSERT INTO agg_crowding(snapshot_id,coin,n_long,n_short,net_notional,pct_long,n_fresh,"
        "funding,long_into_negative_funding) VALUES (?,?,?,?,?,?,?,?,?)",
        [(
            snap_id, c["coin"], c["n_long"], c["n_short"], c["net_notional"], c["pct_long"],
            c["n_fresh"], c["funding"], int(c["long_into_negative_funding"]),
        ) for c in crowding],
    )
    conn.commit()
    conn.close()

    log.info("snapshot %s: %d traders, %d coins in crowding", snap_id, len(records), len(crowding))
    result = {"snapshot_id": snap_id, "addresses": len(records), "coins": len(crowding), "dry_run": dry_run}
    if export:
        from .export import export_latest
        path = export_latest(cfg)
        result["export"] = path
    return result


def _fresh_pairs(conn, coin_prefix: str, since_ms: int) -> set[tuple]:
    """(address, coin) pairs with a captured fill inside the window — a proxy for
    'recently active in this coin'. Documented as approximate (we don't have exact
    position open times without full fill history)."""
    rows = conn.execute(
        "SELECT DISTINCT address, coin FROM xyz_fills WHERE time >= ? AND coin LIKE ?",
        (since_ms, coin_prefix + "%"),
    ).fetchall()
    return {(r["address"], r["coin"]) for r in rows}


def main():
    ap = argparse.ArgumentParser(description="Rank HIP-3 dex traders into an immutable snapshot.")
    ap.add_argument("--dry-run", action="store_true", help="use seed list / small discovered set")
    ap.add_argument("--window", choices=["24h", "7d", "30d", "all"], help="ranking window")
    ap.add_argument("--top-n", type=int, help="leaderboard size")
    ap.add_argument("--limit-addrs", type=int, default=None, help="cap addresses evaluated")
    ap.add_argument("--export", action="store_true", help="write dashboard JSON of latest snapshot")
    args = ap.parse_args()

    cfg = IntelConfig()
    if args.window:
        cfg.window = args.window
    if args.top_n:
        cfg.top_n = args.top_n
    result = run_ranker(cfg, dry_run=args.dry_run, limit_addrs=args.limit_addrs, export=args.export)
    log.info("done: %s", result)


if __name__ == "__main__":
    main()
