"""ULID (https://github.com/ulid/spec): 48 бит времени (Unix ms) + 80 бит случайности,
26 символов Crockford base32. Встроенная реализация — без внешней зависимости.

Лексикографический порядок совпадает с порядком по времени с точностью до миллисекунды;
монотонность внутри одной миллисекунды не гарантируется (для id это не требуется).
"""

import os
import re
import time

__all__ = ["CROCKFORD32", "ULID_RE", "is_ulid", "new_ulid"]

CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# Первый символ ≤ 7: 26 × 5 = 130 бит, из них значимы 128.
ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_MAX_TS = (1 << 48) - 1


def new_ulid(ts_ms: int | None = None) -> str:
    """Новый ULID.

    Args:
        ts_ms: время в Unix ms UTC; по умолчанию — текущее.

    Raises:
        ValueError: `ts_ms` вне [0, 2**48).
    """
    ts = int(time.time() * 1000) if ts_ms is None else ts_ms
    if not 0 <= ts <= _MAX_TS:
        raise ValueError(f"ULID timestamp out of range: {ts}")
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def is_ulid(value: str) -> bool:
    """Строка — ULID в канонической форме (верхний регистр Crockford base32)."""
    return ULID_RE.fullmatch(value) is not None
