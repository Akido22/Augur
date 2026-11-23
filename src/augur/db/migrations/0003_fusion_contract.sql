-- 0003_fusion_contract.sql
-- compat: contract
-- db_schema_version 3 (Augur 0.4.0)
--
-- Пересоздание fused_signals для explanation_ref NOT NULL (ML_LLM_FUSION_SPEC.md §4.6).
-- explanation_ref — строка-ссылка ("fusion_stub" | "decision_audit:<ulid>"), НЕ внешний ключ.
-- SQLite не поддерживает ALTER COLUMN ... NOT NULL, поэтому таблица пересоздаётся.
-- contract: ужесточение ограничения колонки (VERSIONING.md §8.1) — BREAKING для писателей с NULL.
--
-- Данных на момент 0.4.0 нет (0 строк). При наличии данных перед DROP нужно:
--   INSERT INTO fused_signals_new SELECT id, signal_id, ..., COALESCE(explanation_ref,
--   'migration:backfill'), ... FROM fused_signals;
--
-- BEGIN/COMMIT здесь нет намеренно: мигратор сам открывает транзакцию и в ней же пишет
-- schema_migrations (migrator.py). DROP родительской таблицы при foreign_keys=ON безопасен,
-- пока в signals.fused_signal_id / decision_audit.fused_signal_id нет ссылок (0 строк);
-- RENAME возвращает имя fused_signals, на которое указывают эти FK.

CREATE TABLE fused_signals_new (
    id                    TEXT    PRIMARY KEY,   -- ULID, выдаётся Signal Engine при приёме
    signal_id             TEXT    NOT NULL UNIQUE, -- UUID, выданный Fusion
    idempotency_key       TEXT    NOT NULL UNIQUE, -- защита от дублей ордеров
    api_version           TEXT    NOT NULL,      -- версия контракта, "1.0"
    asset                 TEXT    NOT NULL,
    timestamp             INTEGER NOT NULL,      -- close_time бара, Unix ms UTC
    expires_at            INTEGER NOT NULL,      -- Unix ms UTC
    action                TEXT    NOT NULL CHECK (action IN ('BUY','SELL','CLOSE','REDUCE','HOLD','NO_SIGNAL')),
    strength              REAL    NOT NULL CHECK (strength BETWEEN 0 AND 1),
    reason                TEXT    NOT NULL,
    explanation_ref       TEXT    NOT NULL,      -- строка-ссылка, НЕ FK
    risk_flags_json       TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(risk_flags_json)),
    source_breakdown_json TEXT    NOT NULL CHECK (json_valid(source_breakdown_json)),  -- {ml, tech, llm, rules_fired}
    fusion_config_version TEXT    NOT NULL,      -- версия конфига весов/порогов
    valid                 INTEGER NOT NULL CHECK (valid IN (0,1)),
    validation_errors_json TEXT   NOT NULL DEFAULT '[]',
    created_at            INTEGER NOT NULL DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER))  -- Unix ms UTC
) STRICT;

DROP TABLE fused_signals;
ALTER TABLE fused_signals_new RENAME TO fused_signals;
CREATE INDEX ix_fused_asset_ts ON fused_signals(asset, timestamp);
CREATE INDEX ix_fused_created ON fused_signals(created_at);
