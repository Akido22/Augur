"""Подготовка недоверенного текста и сканер секретов (ML_LLM_FUSION_SPEC.md §3.6).

- Текст новостей: вырезаются управляющие символы, `<`/`>` заменяются (нельзя закрыть
  разделитель `<inputs>`), длина обрезается.
- Сканер: известные значения секретов из конфига, типовые префиксы ключей и длинные
  высокоэнтропийные строки смешанного регистра (hex-хэши транзакций не считаются секретом).
"""

import math
import re
from collections import Counter
from collections.abc import Iterable

__all__ = ["clean_text", "find_secret"]

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
_PREFIXES = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{10,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|"
    r"xox[abp]-[A-Za-z0-9\-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_TOKEN = re.compile(r"[A-Za-z0-9+/_\-]{40,}")
_ENTROPY_MIN = 4.0


def clean_text(text: str | None, max_chars: int) -> str:
    s = _CTRL.sub(" ", text or "")
    s = s.replace("<", "‹").replace(">", "›")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_chars]


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def find_secret(text: str, known_secrets: Iterable[str]) -> str | None:
    """Тип найденного секрета (без самого значения) или None."""
    for secret in known_secrets:
        if secret and len(secret) >= 8 and secret in text:
            return "configured_secret"
    if _PREFIXES.search(text):
        return "key_prefix"
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        mixed = any(c.islower() for c in tok) and any(c.isupper() for c in tok)
        if mixed and any(c.isdigit() for c in tok) and _entropy(tok) >= _ENTROPY_MIN:
            return "high_entropy"
    return None
