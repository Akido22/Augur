"""Детерминированный `client_order_id` (ARCHITECTURE.md §7.3).

`'jn' + base32(sha256(f"{signal_id}|{leg}|{attempt}"))[:22]` → 24 символа: влезает в MT5
comment (≤ 31) и в лимиты бирж (обычно ≤ 32–36). Тот же сигнал → тот же id, поэтому повторная
обработка сигнала упирается в `UNIQUE(venue, client_order_id)` и не даёт второй сделки.
`attempt` растёт только при явном перевыставлении (это новый ордер).
"""

import base64
import hashlib
import re

__all__ = ["CLIENT_ORDER_ID_RE", "LEGS", "make_client_order_id"]

CLIENT_ORDER_ID_RE = re.compile(r"^jn[a-z2-7]{22}$")
# entry — вход/выход по сигналу; sl/tp — защитные выходы; flatten — kill switch / CB.
LEGS = frozenset({"entry", "sl", "tp", "flatten"})


def make_client_order_id(signal_id: str, leg: str = "entry", attempt: int = 0) -> str:
    """Raises: ValueError — неизвестный `leg`, `attempt < 0` или пустой `signal_id`."""
    if not signal_id:
        raise ValueError("signal_id must be non-empty")
    if leg not in LEGS:
        raise ValueError(f"unknown leg: {leg!r}")
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    digest = hashlib.sha256(f"{signal_id}|{leg}|{attempt}".encode()).digest()
    return "jn" + base64.b32encode(digest).decode("ascii").lower()[:22]
