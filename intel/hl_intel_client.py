"""
Hyperliquid info-API client for the intel layer.

Adds what the ranker needs on top of the existing core.hl_client patterns:
  - a thread-safe token-bucket rate limiter (info endpoint is weight-limited per IP),
  - a concurrency cap (semaphore) for fan-out across many addresses,
  - exponential backoff honoring Retry-After on 429 / 5xx,
  - userFunding pagination (walks startTime forward when a page returns the 500 cap).

All request bodies match the verified Hyperliquid info schemas.
"""
from __future__ import annotations

import logging
import threading
import time

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("intel.client")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
_FUNDING_PAGE_CAP = 500  # API returns at most 500 userFunding rows per call


class TokenBucket:
    """Simple thread-safe token bucket. capacity tokens, refilled at rate/sec."""

    def __init__(self, rate: float, capacity: int):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = (1.0 - self._tokens) / self.rate
            time.sleep(max(deficit, 0.005))


class HLIntelClient:
    def __init__(self, rps: float = 8.0, burst: int = 8, concurrency: int = 5,
                 max_retries: int = 5, verify_tls: bool = True, timeout: int = 15):
        self.bucket = TokenBucket(rps, burst)
        self.sem = threading.Semaphore(concurrency)
        self.max_retries = max_retries
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        adapter = HTTPAdapter(pool_connections=max(32, concurrency * 4),
                              pool_maxsize=max(32, concurrency * 4))
        self.session.mount("https://", adapter)

    def _post(self, body: dict):
        """One throttled, retried POST to the info endpoint."""
        last_err = None
        for attempt in range(self.max_retries):
            self.bucket.acquire()
            with self.sem:
                try:
                    resp = self.session.post(HL_INFO_URL, json=body,
                                             timeout=self.timeout, verify=self.verify_tls)
                except requests.RequestException as e:
                    last_err = str(e)
                    resp = None
            if resp is not None:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else min(60.0, 2 ** attempt)
                    last_err = f"HTTP {resp.status_code}"
                    logger.warning("info %s on %s; backing off %.1fs (attempt %d)",
                                   resp.status_code, body.get("type"), wait, attempt + 1)
                    time.sleep(wait)
                    continue
                # Other 4xx: not retryable.
                raise RuntimeError(f"info {body.get('type')} -> HTTP {resp.status_code}: {resp.text[:300]}")
            time.sleep(min(30.0, (2 ** attempt) * 0.5))
        raise RuntimeError(f"info {body.get('type')} failed after {self.max_retries} attempts: {last_err}")

    # ---- typed endpoints (verified request bodies) ----

    def meta_and_asset_ctxs(self, dex: str):
        """Returns [meta, assetCtxs]. universe[i] aligns positionally with assetCtxs[i]."""
        return self._post({"type": "metaAndAssetCtxs", "dex": dex})

    def recent_trades(self, coin: str):
        """Recent trades for a coin; each trade carries users:[buyer, seller]."""
        return self._post({"type": "recentTrades", "coin": coin})

    def clearinghouse_state(self, user: str, dex: str):
        """Per-address positions + account summary, scoped to the HIP-3 dex.
        The dex param is REQUIRED — omitting it returns the core perp dex."""
        return self._post({"type": "clearinghouseState", "user": user, "dex": dex})

    def perp_dex_limits(self, dex: str):
        return self._post({"type": "perpDexLimits", "dex": dex})

    def perps_at_oi_cap(self, dex: str):
        try:
            return self._post({"type": "perpsAtOpenInterestCap", "dex": dex}) or []
        except RuntimeError:
            return []

    def user_funding(self, user: str, start_time_ms: int, end_time_ms: int | None = None):
        """All funding deltas in [start, end]. Paginates by walking startTime forward
        whenever a page hits the 500-row cap. NOTE: not dex-scoped — callers must
        filter deltas by coin prefix to attribute funding to the HIP-3 dex."""
        out: list[dict] = []
        cursor = int(start_time_ms)
        seen_hashes: set[tuple] = set()
        while True:
            body = {"type": "userFunding", "user": user, "startTime": cursor}
            if end_time_ms is not None:
                body["endTime"] = int(end_time_ms)
            page = self._post(body) or []
            if not page:
                break
            new = 0
            for item in page:
                key = (item.get("hash"), item.get("time"), item.get("delta", {}).get("coin"))
                if key in seen_hashes:
                    continue
                seen_hashes.add(key)
                out.append(item)
                new += 1
            if len(page) < _FUNDING_PAGE_CAP or new == 0:
                break
            # Walk forward off the last row's time (inclusive overlap deduped above).
            cursor = int(page[-1].get("time", cursor)) + 1
        return out
