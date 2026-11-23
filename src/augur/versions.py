"""Централизованные версии компонентов Augur (VERSIONING.md §2.2).

APP_VERSION — не здесь: читается через importlib.metadata.version("augur").
DB_SCHEMA_VERSION — последняя применённая миграция (0002_ohlcv = 2).
API_VERSION — версия контракта fused_signal (используется с 0.4.0).
"""

DB_SCHEMA_VERSION: int = 2
API_VERSION: str = "1.0"

__all__ = ["API_VERSION", "DB_SCHEMA_VERSION"]
