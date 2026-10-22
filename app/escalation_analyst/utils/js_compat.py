"""Explicit JavaScript compatibility rules used by deterministic reports."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP


def js_round(value: float) -> int:
    return math.floor(value + 0.5)


def fixed(value: float, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def utf16_slice(value: str, length: int) -> str:
    return value.encode("utf-16-le", errors="surrogatepass")[: length * 2].decode(
        "utf-16-le", errors="surrogatepass"
    )
