"""Augur — гибридная торговая система ML+LLM."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("augur")
except PackageNotFoundError:  # запуск из исходников без `pip install -e .`
    __version__ = "0.0.0+unknown"  # Execution отклоняет ордера с такой версией (VERSIONING §6.2)

__all__ = ["__version__"]
