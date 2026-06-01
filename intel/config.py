"""
Intel-layer configuration. Reads from env with safe defaults, and inherits the
target dex from the existing analyzer settings (config.settings.HL_PERP_DEX).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Inherit the dex from the existing project config so the whole repo agrees on
# which HIP-3 namespace we target (defaults to "xyz").
try:
    from config.settings import HL_PERP_DEX as _SETTINGS_DEX
except Exception:  # pragma: no cover - settings may require other env at import
    _SETTINGS_DEX = os.environ.get("HL_PERP_DEX", "xyz").strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# Ranking windows in milliseconds. "all" => from epoch-ish (clamped at call site).
WINDOWS_MS = {
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
    "all": None,
}


@dataclass
class IntelConfig:
    dex: str = field(default_factory=lambda: (os.environ.get("HL_PERP_DEX", "").strip() or _SETTINGS_DEX or "xyz"))
    window: str = field(default_factory=lambda: os.environ.get("INTEL_WINDOW", "7d").strip())
    top_n: int = field(default_factory=lambda: _env_int("INTEL_TOP_N", 100))

    # Rate limiting for the info endpoint (weight-limited per IP).
    # Conservative defaults; tune via env for the full address set.
    # Hyperliquid info endpoint is ~1200 weight/min/IP; clearinghouseState &
    # userFunding are weight ~20, i.e. ~60/min ≈ 1/sec sustained. Defaults stay
    # under that; raise only if you control a higher limit.
    rps: float = field(default_factory=lambda: _env_float("INTEL_RPS", 1.5))         # token refill / sec
    burst: int = field(default_factory=lambda: _env_int("INTEL_BURST", 3))            # bucket capacity
    concurrency: int = field(default_factory=lambda: _env_int("INTEL_CONCURRENCY", 2))  # in-flight cap
    max_retries: int = field(default_factory=lambda: _env_int("INTEL_MAX_RETRIES", 8))
    verify_tls: bool = field(default_factory=lambda: os.environ.get("INTEL_VERIFY_TLS", "true").lower() != "false")

    # Storage. Separate DB file from the collector — we never write collector tables.
    db_path: str = field(default_factory=lambda: os.environ.get("INTEL_DB_PATH", str(ROOT / "intel" / "intel.db")))

    # Dashboard JSON export target.
    export_path: str = field(default_factory=lambda: os.environ.get("INTEL_EXPORT_PATH", str(ROOT / "dashboard" / "intel_snapshot.json")))

    # Seed list for --dry-run when no participant set exists yet.
    seed_path: str = field(default_factory=lambda: os.environ.get("INTEL_SEED_PATH", str(ROOT / "intel" / "seed_addresses.txt")))

    def window_ms(self) -> int | None:
        return WINDOWS_MS.get(self.window, WINDOWS_MS["7d"])

    def coin_prefix(self) -> str:
        return f"{self.dex}:"
