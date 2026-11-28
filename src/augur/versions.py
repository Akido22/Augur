"""Централизованные версии компонентов Augur (VERSIONING.md §2.2).

APP_VERSION — не здесь: читается через importlib.metadata.version("augur").
DB_SCHEMA_VERSION — последняя миграция, которую ожидает код (0007_ml_llm = 7).
API_VERSION — версия контракта fused_signal (ML_LLM_FUSION_SPEC.md §4.6).
CONFIG_VERSION — формат конфига, который читает этот код.
"""

DB_SCHEMA_VERSION: int = 7
API_VERSION: str = "1.0"
CONFIG_VERSION: int = 6

__all__ = ["DB_SCHEMA_VERSION", "API_VERSION", "CONFIG_VERSION"]
