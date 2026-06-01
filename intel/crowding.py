"""
Aggregate / crowding metrics across the top-N traders, per coin.

All metrics are descriptive positioning facts, never recommendations.
"""
from __future__ import annotations

from .money import D, ZERO, to_str, safe_div


def compute(trader_records: list[dict], coin_ctx: dict[str, dict]) -> list[dict]:
    """
    trader_records: each has 'positions' = list of dicts with coin, szi (Decimal),
                    position_value (Decimal), fresh (bool).
    coin_ctx:       coin -> {funding: Decimal, ...}.
    Returns one row per coin that any top trader holds.
    """
    by_coin: dict[str, dict] = {}
    for rec in trader_records:
        for p in rec["positions"]:
            coin = p["coin"]
            agg = by_coin.setdefault(coin, {
                "coin": coin, "n_long": 0, "n_short": 0,
                "net_notional": ZERO, "n_fresh": 0,
            })
            szi = p["szi"]
            pv = p["position_value"]
            if szi > 0:
                agg["n_long"] += 1
                agg["net_notional"] += pv
            elif szi < 0:
                agg["n_short"] += 1
                agg["net_notional"] -= pv
            if p.get("fresh"):
                agg["n_fresh"] += 1

    out = []
    for coin, agg in by_coin.items():
        total = agg["n_long"] + agg["n_short"]
        pct_long = safe_div(D(agg["n_long"]), D(total))
        funding = (coin_ctx.get(coin) or {}).get("funding", ZERO)
        # "Long into negative funding" = the crowd leans long while funding is
        # negative (longs pay shorts) -> expensive to hold. Descriptive flag.
        long_into_neg = bool(agg["net_notional"] > 0 and funding < 0)
        out.append({
            "coin": coin,
            "n_long": agg["n_long"],
            "n_short": agg["n_short"],
            "net_notional": to_str(agg["net_notional"]),
            "pct_long": to_str(pct_long) if pct_long is not None else None,
            "n_fresh": agg["n_fresh"],
            "funding": to_str(funding),
            "long_into_negative_funding": long_into_neg,
        })
    # Sort by absolute net lean, biggest first.
    out.sort(key=lambda r: abs(D(r["net_notional"])), reverse=True)
    return out


def concentration_top3(crowding_rows: list[dict]) -> dict:
    """Share of total top-trader notional sitting in the 3 biggest coins."""
    notionals = sorted((abs(D(r["net_notional"])) for r in crowding_rows), reverse=True)
    total = sum(notionals, ZERO)
    top3 = sum(notionals[:3], ZERO)
    share = safe_div(top3, total)
    return {"top3_notional": to_str(top3), "total_notional": to_str(total),
            "top3_share": to_str(share) if share is not None else None}
