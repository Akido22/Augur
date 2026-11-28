-- 0007_ml_llm.sql
-- compat: expand
-- db_schema_version 7 (Augur 0.10.0)
--
-- ML+LLM-контур, ML_LLM_FUSION_SPEC.md §7 (DDL оттуда, отличия помечены):
--   ml_models      — жизненный цикл модели (§2.4); артефакт и sha256 — также в artifacts.
--                    + status_changed_at: отсчёт ≥ N дней в staging для промоушена.
--   ml_predictions — прогнозы (§2.5), пишутся до передачи в fuse(); UNIQUE = идемпотентность.
--   llm_calls      — каждый вызов LLM, включая повторы и сэмплы (§3.5).
--                    + task 'news_sentiment' (LLM-скорер новостей, VERSIONING §4).
--   llm_contexts   — (нет в §7) агрегированный результат LLMContextService.get_context:
--                    Fusion читает последний валидный контекст, а не зовёт LLM на критическом
--                    пути (§6.2 п. 2). Строки append-only.
-- Время — INTEGER Unix ms UTC.

CREATE TABLE ml_models (
    id                  INTEGER PRIMARY KEY,
    name                TEXT    NOT NULL,                 -- signal_clf_btcusdt_1h
    version             TEXT    NOT NULL,                 -- lightgbm_v<N>
    artifact_path       TEXT    NOT NULL,
    artifact_sha256     TEXT    NOT NULL,
    feature_set_version TEXT    NOT NULL,
    trained_at          INTEGER NOT NULL,
    train_from          INTEGER,
    train_to            INTEGER,
    metrics_json        TEXT    NOT NULL CHECK (json_valid(metrics_json)),
    status              TEXT    NOT NULL DEFAULT 'candidate'
                        CHECK (status IN ('candidate','staging','production','retired')),
    approved_by         TEXT,
    status_changed_at   INTEGER NOT NULL,
    UNIQUE (name, version),
    UNIQUE (version)                                      -- N монотонен глобально
) STRICT;
CREATE UNIQUE INDEX ux_ml_models_prod ON ml_models(name) WHERE status = 'production';

CREATE TABLE ml_predictions (
    id               TEXT    PRIMARY KEY,                 -- ULID
    model_id         INTEGER NOT NULL REFERENCES ml_models(id),
    asset            TEXT    NOT NULL,
    timestamp        INTEGER NOT NULL,                    -- close_time бара
    direction        TEXT    NOT NULL CHECK (direction IN ('long','flat','short')),
    confidence       REAL    NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    horizon          TEXT    NOT NULL,
    regime_tag       TEXT    NOT NULL,
    class_probs_json TEXT    CHECK (class_probs_json IS NULL OR json_valid(class_probs_json)),
    input_hash       TEXT,
    realized_return  REAL,
    UNIQUE (model_id, asset, timestamp)
) STRICT;
CREATE INDEX ix_ml_pred_asset_ts ON ml_predictions(asset, timestamp);

CREATE TABLE llm_calls (
    id            INTEGER PRIMARY KEY,
    prompt_hash   TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL,
    cost_usd      REAL    NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    task          TEXT    NOT NULL DEFAULT 'context'
                  CHECK (task IN ('context','explain','news_sentiment')),
    template_id   TEXT,                                   -- prompt_context_v1
    asset         TEXT,
    request_group TEXT,
    sample_index  INTEGER,
    temperature   REAL,
    status        TEXT    NOT NULL DEFAULT 'ok'
                  CHECK (status IN ('ok','invalid_schema','ungrounded','variance_reject',
                                    'timeout','http_error','budget_block','secret_block')),
    error         TEXT,
    response_json TEXT
) STRICT;
CREATE INDEX ix_llm_calls_created ON llm_calls(created_at);
CREATE INDEX ix_llm_calls_group   ON llm_calls(request_group);
CREATE INDEX ix_llm_calls_hash    ON llm_calls(prompt_hash);

CREATE TABLE llm_contexts (
    id                 TEXT    PRIMARY KEY,               -- ULID
    asset              TEXT    NOT NULL,
    as_of              INTEGER NOT NULL,
    status             TEXT    NOT NULL CHECK (status IN ('ok','rejected','fallback_ml_only')),
    reject_reason      TEXT,
    prompt_version     TEXT    NOT NULL,
    context_json       TEXT    CHECK (context_json IS NULL OR json_valid(context_json)),
    llm_call_ids_json  TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(llm_call_ids_json)),
    request_group      TEXT    NOT NULL,
    created_at         INTEGER NOT NULL,
    CHECK ((status = 'ok') = (context_json IS NOT NULL))
) STRICT;
CREATE INDEX ix_llm_contexts_asset ON llm_contexts(asset, as_of);

CREATE TRIGGER llm_contexts_no_update BEFORE UPDATE ON llm_contexts
BEGIN SELECT RAISE(ABORT, 'llm_contexts is append-only'); END;
