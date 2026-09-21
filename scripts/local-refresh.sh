#!/bin/zsh
# Regenerates the whale snapshot from Hyperliquid and redeploys — the local
# counterpart of .github/workflows/refresh.yml. GitHub's shared cron scheduler
# drops most */15 slots (observed gaps of 2-5 hours), so launchd fires this
# every 45 min while the Mac is awake; the GitHub cron stays on as fallback.
set -e
cd /Users/kushagrasingh/Development/hl-trader-intel
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export HL_PERP_DEX=xyz
export INTEL_DB_PATH=intel.db
export INTEL_EXPORT_PATH=public/intel_snapshot.json

# one refresh at a time; a lock older than 90 min is from a killed run
LOCK=/tmp/hl-intel-refresh.lock.d
if [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +90 2>/dev/null)" ]; then rmdir "$LOCK"; fi
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK"' EXIT

echo "[$(date '+%F %T')] refresh start"
python3 -m intel.discover --duration 90
# 140 addresses, not the workflow's 200: this Mac reuses one IP so Hyperliquid's
# rate limit bites much harder than on GitHub's fresh-IP runners — a smaller
# budget keeps a full cycle near an hour instead of 2.5h
python3 -m intel.ranker --window 7d --top-n 100 --limit-addrs 140 --export
vercel deploy --prod --yes
echo "[$(date '+%F %T')] refresh done"
