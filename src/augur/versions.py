"""Централизованные версии компонентов Augur (VERSIONING.md §2.2).

APP_VERSION — не здесь: читается через importlib.metadata.version("augur").
DB_SCHEMA_VERSION — последняя миграция, которую ожидает код (0004_risk_state = 4).
API_VERSION — версия контракта fused_signal (ML_LLM_FUSION_SPEC.md §4.6).
CONFIG_VERSION — формат конфига, который читает этот код.
"""

DB_SCHEMA_VERSION: int = 4
API_VERSION: str = "1.0"
CONFIG_VERSION: int = 2

__all__ = ["DB_SCHEMA_VERSION", "API_VERSION", "CONFIG_VERSION"]
