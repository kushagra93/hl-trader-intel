"""
Money math helpers. Hyperliquid returns all px/usdc/size values as strings.
Parse to Decimal for arithmetic — never float — to avoid rounding drift on
money. Display rounding is applied only at the edges (export / UI).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

ZERO = Decimal("0")


def D(value, default: Decimal = ZERO) -> Decimal:
    """Parse an API string/number to Decimal, tolerating None/garbage."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def to_str(value: Decimal | None) -> str | None:
    """Canonical string form for storage/JSON (preserves precision, no float)."""
    if value is None:
        return None
    return format(value, "f")


def safe_div(num: Decimal, den: Decimal) -> Decimal | None:
    if den == 0:
        return None
    return num / den
