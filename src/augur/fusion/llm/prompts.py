"""Версионированные промпты `prompts/<task>/<version>.j2` (FUSION SPEC §3.3; VERSIONING §2.3).

`llm_prompt_version` (`prompt_<назначение>_v<N>`) = текст шаблона + модели провайдеров +
параметры генерации. Отпечаток пишется в `artifacts(kind='llm_prompt')`; если та же версия уже
записана с другим отпечатком (правка на месте, смена модели), `ensure_registered` отказывает:
нужна новая версия шаблона.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from augur.config.settings import LLMSettings

__all__ = ["PromptTemplate", "PromptVersionConflict"]

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_SPLIT = "---USER---"
_ENV = Environment(autoescape=False, undefined=StrictUndefined, keep_trailing_newline=False)  # noqa: S701 — не HTML; текст очищается clean_text


class PromptVersionConflict(RuntimeError):
    """Версия промпта уже зарегистрирована с другим содержимым или моделью."""


class PromptTemplate:
    def __init__(self, task: str, version: str) -> None:
        path = PROMPTS_DIR / task / f"{version}.j2"
        self.task = task
        self.version = version
        self.text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        if _SPLIT not in self.text:
            raise ValueError(f"template {path.name} has no {_SPLIT} separator")
        system, user = self.text.split(_SPLIT, 1)
        self._system = _ENV.from_string(system.strip())
        self._user = _ENV.from_string(user.strip())

    def render(self, **values: Any) -> tuple[str, str]:
        return self._system.render(**values), self._user.render(**values)

    def prompt_hash(self, system: str, user: str) -> str:
        """sha256(template_id@version + отрендеренный промпт) (§3.5)."""
        src = f"{self.task}@{self.version}\n{system}\n{_SPLIT}\n{user}"
        return hashlib.sha256(src.encode("utf-8")).hexdigest()

    def fingerprint(
        self, cfg: LLMSettings, temperature: float | None
    ) -> tuple[str, dict[str, Any]]:
        meta = {
            "template_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            "providers": [[p.name, p.model, p.send_temperature] for p in cfg.providers],
            "temperature": temperature,
            "max_tokens": cfg.max_tokens,
            "samples": cfg.variance.samples,
        }
        canon = json.dumps(meta, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest(), meta

    def ensure_registered(
        self, conn: sqlite3.Connection, cfg: LLMSettings, temperature: float | None, now: int
    ) -> None:
        sha, meta = self.fingerprint(cfg, temperature)
        row = conn.execute(
            "SELECT sha256 FROM artifacts WHERE kind='llm_prompt' AND version=?", (self.version,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO artifacts(kind, version, sha256, path, metadata, created_at) "
                "VALUES ('llm_prompt', ?, ?, ?, ?, ?)",
                (
                    self.version,
                    sha,
                    f"prompts/{self.task}/{self.version}.j2",
                    json.dumps(meta, sort_keys=True),
                    now,
                ),
            )
            conn.commit()
        elif row[0] != sha:
            raise PromptVersionConflict(
                f"{self.version} is registered with another template/model/params; "
                "create a new prompt version (VERSIONING §2.3)"
            )
