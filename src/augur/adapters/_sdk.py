"""Ленивая загрузка SDK площадок (они — опциональные extras в pyproject)."""

import importlib
from typing import Any

from augur.adapters.base import FatalAdapterError


def import_sdk(module: str, extra: str) -> Any:
    """Импортирует модуль SDK или сообщает, какой extra установить.

    Raises:
        FatalAdapterError: SDK не установлен.
    """
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise FatalAdapterError(
            f"SDK '{module}' не установлен: pip install -e \".[{extra}]\""
        ) from e


def exc_names(e: BaseException) -> set[str]:
    """Имена классов в MRO исключения — для классификации ошибок без импорта SDK."""
    return {cls.__name__ for cls in type(e).__mro__}
