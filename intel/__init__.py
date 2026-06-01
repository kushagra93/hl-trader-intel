"""
Trader Intelligence Layer for the HIP-3 (builder-deployed) perp dex.

Two layers built on top of the existing market-level analyzer:
  - Ranker  (intel.ranker)    : scheduled, idempotent leaderboard snapshots.
  - Dashboard (dashboard/intel.html) : read-only view over the latest snapshot.

This is a market-intelligence / positioning product. It describes what is
happening on-chain. Nothing here is advice, a signal to copy, or a
recommendation. See README_INTEL.md.
"""
