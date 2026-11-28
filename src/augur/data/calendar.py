"""Макро-календарь из YAML (ML_LLM_FUSION_SPEC.md §3.1, §3.4, §6.1; ARCHITECTURE.md §8.4; 0.12.0).

Календарь ведёт человек в `config/macro_calendar.yaml` (путь — `macro.calendar_path`). Это
детерминированный источник времени событий: LLM получает события как вход (grounding флага
`high_impact_event_imminent`), Risk Gate сам блокирует входы в окне вокруг событий
(`macro.blackout_before_min` / `blackout_after_min`) — защита не зависит от LLM.

Формат файла::

    events:
      - id: evt_us_cpi_2026_12        # [a-z0-9_]+, уникален
        name: US CPI (Nov)
        time: 2026-12-10T13:30:00Z    # обязательно с часовым поясом
        impact: high                  # low | medium | high
        scope: [USD]                  # валюты/активы; "*" — все инструменты

Нет файла — пустой календарь. Невалидный файл — `CalendarError` (потребители решают сами:
Risk Gate закрывает входы, fail-closed).
"""

import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import yaml  # type: ignore[import-untyped]  # стабов types-PyYAML нет в dev
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "CalendarError",
    "Impact",
    "MacroCalendar",
    "MacroEvent",
    "load_calendar",
    "symbol_currencies",
]

Impact = Literal["low", "medium", "high"]


class CalendarError(ValueError):
    """Файл календаря не читается или не проходит валидацию."""


class MacroEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    name: str = Field(min_length=1, max_length=200)
    time: datetime
    impact: Impact
    scope: list[str] = Field(min_length=1)

    @field_validator("time")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("time must include a timezone (e.g. 2026-12-10T13:30:00Z)")
        return v.astimezone(UTC)

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: list[str]) -> list[str]:
        out = [s.strip().upper() for s in v]
        if any(not s for s in out):
            raise ValueError("scope items must be non-empty")
        return out

    @property
    def time_ms(self) -> int:
        return int(self.time.timestamp() * 1000)

    @property
    def time_iso(self) -> str:
        return self.time.isoformat().replace("+00:00", "Z")

    def affects(self, currencies: frozenset[str]) -> bool:
        return "*" in self.scope or bool(currencies & set(self.scope))


def symbol_currencies(symbol: str, aliases: Mapping[str, str] | None = None) -> frozenset[str]:
    """`BTC/USDT` → {BTC, USDT, BTC/USDT} + алиасы (`USDT` → `USD`)."""
    up = symbol.strip().upper()
    parts = {p for p in up.replace("-", "/").split("/") if p} | {up}
    amap = {k.upper(): v.upper() for k, v in (aliases or {}).items()}
    return frozenset(parts | {amap[p] for p in parts if p in amap})


class MacroCalendar(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    events: list[MacroEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [e.id for e in self.events]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"duplicate event ids: {dup}")
        return self

    def select(
        self,
        *,
        start_ms: int,
        end_ms: int,
        symbol: str | None = None,
        aliases: Mapping[str, str] | None = None,
        impacts: Iterable[str] | None = None,
    ) -> list[MacroEvent]:
        """События с `start_ms <= time <= end_ms`, затронувшие символ, по времени."""
        cur = symbol_currencies(symbol, aliases) if symbol is not None else None
        allowed = set(impacts) if impacts is not None else None
        out = [
            e
            for e in self.events
            if start_ms <= e.time_ms <= end_ms
            and (cur is None or e.affects(cur))
            and (allowed is None or e.impact in allowed)
        ]
        return sorted(out, key=lambda e: (e.time_ms, e.id))


_CACHE: dict[str, tuple[float, int, MacroCalendar]] = {}


def load_calendar(path: str | Path) -> MacroCalendar:
    """Календарь из YAML; нет файла → пустой. Кэш по (mtime, size) — правка файла подхватывается.

    Raises:
        CalendarError: YAML не парсится или не проходит схему.
    """
    p = Path(path)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return MacroCalendar()
    except OSError as e:
        raise CalendarError(f"cannot read calendar {p}: {type(e).__name__}") from e
    key = str(p.resolve())
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        raise CalendarError(f"cannot parse calendar {p}: {type(e).__name__}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CalendarError(f"calendar {p}: top level must be a mapping with 'events'")
    try:
        cal = MacroCalendar.model_validate(raw)
    except ValidationError as e:
        msg = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())[
            :500
        ]
        raise CalendarError(f"calendar {p} invalid: {msg}") from e
    _CACHE[key] = (st.st_mtime, st.st_size, cal)
    return cal
