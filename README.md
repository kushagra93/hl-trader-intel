# hl-trader-intel — live HIP-3 Trader Intelligence

A live, auto-refreshing dashboard of the **top traders by profitability** on the
`xyz` HIP-3 perp dex, plus per-stock crowd positioning — fetched straight from
Hyperliquid's public API. No API key. Descriptive positioning data only; **not
advice, not a signal, not a recommendation.**

**Live:** deployed on Vercel, regenerated every 30 minutes by GitHub Actions.

## How it works

```
intel/                  the pipeline (mirrored from hyperliquid-perps-analyzer)
  discover.py           harvest participant addresses from the public recentTrades feed
  ranker.py             fetch positions (clearinghouseState) + funding (userFunding),
                        rank by profitability, write an immutable snapshot (SQLite)
  crowding.py           per-coin long/short, net lean, funding-adjusted flags
  export.py             latest snapshot -> intel_snapshot.json
index.html              static dashboard (Direction A "Stacked"), reads intel_snapshot.json,
                        auto-refetches every 60s and shows a live freshness pill
.github/workflows/refresh.yml   cron */30: discover -> rank top 100 -> export -> deploy
```

Each run is self-contained: it discovers a fresh participant set, ranks the
top 100 by net profitability (unrealized PnL + funding over the window), and
redeploys the static site. Snapshots are immutable; the page always reads the
latest.

### Realized-PnL caveat
There is no clean realized-PnL endpoint and no full upstream fill history, so
**realized PnL is reported as `unavailable`** — the profit figure is unrealized
PnL plus funding over the window, with `basis` tags surfaced in the UI. Funding
is netted in (a position green on price can be net-negative after funding).

## Run locally

```bash
pip install -r requirements.txt
python -m intel.discover --once
INTEL_EXPORT_PATH=intel_snapshot.json python -m intel.ranker --window 7d --top-n 100 --limit-addrs 150 --export
python -m http.server 8099          # http://localhost:8099
```

Tune via env: `HL_PERP_DEX`, `INTEL_WINDOW` (24h|7d|30d|all), `INTEL_TOP_N`,
`INTEL_RPS`/`INTEL_CONCURRENCY` (rate limiting), `INTEL_EXPORT_PATH`.

## The 30-minute refresh

`.github/workflows/refresh.yml` runs the full pipeline and deploys to Vercel.
It needs:
- repo **secret** `VERCEL_TOKEN` — a Vercel access token
- repo **variables** `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` — from `vercel link`

The evaluated-address count is bounded (`--limit-addrs`, default 200) so each run
finishes inside the rate-limit budget (~1 req/sec for the heavy calls). The
GitHub runner gets a fresh IP each run, so limits reset between refreshes.

## Note
The `intel/` package is mirrored from the `hyperliquid-perps-analyzer` repo for
isolated deployment. Pipeline logic changes there should be copied here.
