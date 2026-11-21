-- 0001_core.sql
-- compat: expand
-- db_schema_version 1 (Augur 0.1.0)
--
-- Источники DDL:
--   schema_migrations, artifacts            — VERSIONING.md §2.2, §2.3
--   signals, orders, audit_log, mode_state,
--   cb_state                                — ARCHITECTURE.md §3.2
--   fused_signals, decision_audit           — ARCHITECTURE.md §3.3 (decision_audit = VERSIONING.md §6.1)
-- ml_models / ml_predictions / llm_calls создаёт миграция ML-контура (0.10.0), здесь их нет.
--
-- Время — INTEGER, Unix ms UTC. Идентификаторы — ULID (TEXT). Все таблицы STRICT.
-- PRAGMA (WAL, foreign_keys=ON, busy_timeout) задаются при открытии соединения, не в миграции.

-- ---------------------------------------------------------------------------
-- Служебные таблицы
-- ---------------------------------------------------------------------------

CREATE TABLE schema_migrations (
    version      INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,                  -- 0001_core
    checksum     TEXT    NOT NULL,                  -- sha256 файла миграции
    compat       TEXT    NOT NULL CHECK (compat IN ('expand', 'contract')),
    app_version  TEXT    NOT NULL,                  -- версия Augur, применившая миграцию
    applied_at   INTEGER NOT NULL DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER))  -- Unix ms UTC
) STRICT;

CREATE TABLE artifacts (
    kind        TEXT NOT NULL CHECK (kind IN ('strategy', 'ml_model', 'llm_prompt')),
    version     TEXT NOT NULL,                     -- lightgbm_v3
    sha256      TEXT NOT NULL,
    path        TEXT NOT NULL,                     -- artifacts/ml/lightgbm_v3.txt
    metadata    TEXT NOT NULL,                     -- JSON: features, train_range, llm_model, temperature…
    created_at  INTEGER NOT NULL DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),  -- Unix ms UTC
    PRIMARY KEY (kind, version)
) STRICT;

-- ---------------------------------------------------------------------------
-- Сигналы
-- ---------------------------------------------------------------------------

CREATE TABLE signals (
    id             TEXT    PRIMARY KEY,    -- ULID
    kind           TEXT    NOT NULL CHECK (kind IN ('strategy','final')),
    parent_ids_json TEXT   NOT NULL DEFAULT '[]', -- для final: strategy signal ids
    strategy       TEXT,                   -- name@version (для kind='strategy')
    params_hash    TEXT,
    symbol         TEXT    NOT NULL,
    timeframe      TEXT,
    action         TEXT    NOT NULL CHECK (action IN ('BUY','SELL','CLOSE','REDUCE','HOLD','NO_SIGNAL')),
    strength       REAL    NOT NULL CHECK (strength BETWEEN 0 AND 1),
    source         TEXT    NOT NULL CHECK (source IN ('classic','fusion','classic_fallback')),
    fused_signal_id TEXT   REFERENCES fused_signals(id),
    risk_flags_json TEXT   NOT NULL DEFAULT '[]',
    explanation_ref TEXT,
    stop_loss      TEXT,
    take_profit    TEXT,
    data_hash      TEXT,
    config_hash    TEXT    NOT NULL,
    code_version   TEXT    NOT NULL,       -- git sha
    bar_ts         INTEGER,                -- ts_open бара-триггера
    created_ts     INTEGER NOT NULL,
    expires_ts     INTEGER,
    status         TEXT    NOT NULL DEFAULT 'NEW'
                   CHECK (status IN ('NEW','SENT_TO_RISK','APPROVED','REJECTED','EXPIRED','EXECUTED'))
) STRICT;
-- идемпотентность: одна стратегия не может выдать два сигнала на один бар
CREATE UNIQUE INDEX ux_signals_strategy_bar ON signals(strategy, params_hash, symbol, timeframe, bar_ts)
    WHERE kind = 'strategy';
CREATE INDEX ix_signals_symbol_ts ON signals(symbol, created_ts);

-- Контракт fused_signal — ML_LLM_FUSION_SPEC.md §4.6 + api_version; action хранится после
-- документированного маппинга Fusion → SignalAction (enter_long→BUY, enter_short→SELL,
-- exit→CLOSE, reduce→REDUCE, hold→HOLD, no_signal→NO_SIGNAL).
CREATE TABLE fused_signals (
    id                    TEXT    PRIMARY KEY,   -- ULID, выдаётся Signal Engine при приёме
    signal_id             TEXT    NOT NULL UNIQUE, -- UUID, выданный Fusion
    idempotency_key       TEXT    NOT NULL UNIQUE, -- защита от дублей ордеров
    api_version           TEXT    NOT NULL,      -- версия контракта, "1.0"; другая MAJOR → отклоняется
    asset                 TEXT    NOT NULL,
    timestamp             INTEGER NOT NULL,      -- close_time бара, Unix ms UTC
    expires_at            INTEGER NOT NULL,      -- Unix ms UTC
    action                TEXT    NOT NULL CHECK (action IN ('BUY','SELL','CLOSE','REDUCE','HOLD','NO_SIGNAL')),
    strength              REAL    NOT NULL CHECK (strength BETWEEN 0 AND 1),
    reason                TEXT    NOT NULL,
    explanation_ref       TEXT,
    risk_flags_json       TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(risk_flags_json)),
    source_breakdown_json TEXT    NOT NULL CHECK (json_valid(source_breakdown_json)),  -- {ml, tech, llm, rules_fired}
    fusion_config_version TEXT    NOT NULL,      -- версия конфига весов/порогов, "fusion-2026.11.1"
    valid                 INTEGER NOT NULL CHECK (valid IN (0,1)),
    validation_errors_json TEXT   NOT NULL DEFAULT '[]',
    created_at            INTEGER NOT NULL DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER))  -- Unix ms UTC
) STRICT;
CREATE INDEX ix_fused_asset_ts ON fused_signals(asset, timestamp);
CREATE INDEX ix_fused_created ON fused_signals(created_at);

-- ---------------------------------------------------------------------------
-- Ордера (полная схема ARCHITECTURE.md §3.2; логика исполнения — этап 0.6.0)
-- ---------------------------------------------------------------------------

CREATE TABLE orders (
    id               TEXT    PRIMARY KEY,  -- ULID
    client_order_id  TEXT    NOT NULL,     -- детерминированный, см. ARCHITECTURE.md §7.3
    venue_order_id   TEXT,
    signal_id        TEXT    NOT NULL REFERENCES signals(id),
    risk_verdict_id  TEXT    NOT NULL,
    venue            TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL CHECK (side IN ('BUY','SELL')),
    type             TEXT    NOT NULL CHECK (type IN ('MARKET','LIMIT','STOP','STOP_LIMIT')),
    qty              TEXT    NOT NULL,
    limit_price      TEXT,
    stop_price       TEXT,
    filled_qty       TEXT    NOT NULL DEFAULT '0',
    avg_fill_price   TEXT,
    fee              TEXT    NOT NULL DEFAULT '0',
    fee_ccy          TEXT,
    state            TEXT    NOT NULL CHECK (state IN
                     ('PENDING','SUBMITTED','PARTIAL','FILLED','REJECTED','CANCELLED','UNKNOWN')),
    mode             TEXT    NOT NULL,     -- PAPER | MICRO_LIVE | FULL_AUTO
    reject_reason    TEXT,
    attempt          INTEGER NOT NULL DEFAULT 0,
    created_ts       INTEGER NOT NULL,
    updated_ts       INTEGER NOT NULL,
    version          INTEGER NOT NULL DEFAULT 0  -- optimistic locking
) STRICT;
CREATE UNIQUE INDEX ux_orders_coid ON orders(venue, client_order_id);
CREATE INDEX ix_orders_state ON orders(state) WHERE state IN ('PENDING','SUBMITTED','PARTIAL','UNKNOWN');
CREATE INDEX ix_orders_symbol_ts ON orders(symbol, created_ts);

-- ---------------------------------------------------------------------------
-- Аудит решений (VERSIONING.md §6.1 — единственный источник истины)
-- ---------------------------------------------------------------------------

CREATE TABLE decision_audit (
    id                  TEXT    PRIMARY KEY,                 -- ULID
    signal_id           TEXT    NOT NULL REFERENCES signals(id),
    fused_signal_id     TEXT    REFERENCES fused_signals(id),  -- NULL при fusion.enabled=false (classic / classic_fallback)
    order_id            TEXT    REFERENCES orders(id),       -- NULL до создания ордера и для решений без ордера
    app_version         TEXT    NOT NULL CHECK (length(app_version) > 0
                                                AND app_version NOT LIKE '%+unknown'),
    strategy_version    TEXT    NOT NULL CHECK (length(strategy_version) > 0),
    ml_model_version    TEXT    NOT NULL CHECK (length(ml_model_version) > 0),
    llm_prompt_version  TEXT    NOT NULL CHECK (length(llm_prompt_version) > 0),
    ml_prediction_id    TEXT,                                -- ULID ml_predictions.id; логическая ссылка без FK (таблица — 0.10.0)
    llm_status          TEXT,                                -- ok | rejected | fallback_ml_only; NULL, если LLM не участвовал
    risk_verdict        TEXT    NOT NULL CHECK (risk_verdict IN ('APPROVED', 'REDUCED', 'REJECTED')),
    risk_reason         TEXT,                                -- причина REJECTED / REDUCED
    config_hash         TEXT    NOT NULL,
    mode                TEXT    NOT NULL CHECK (mode IN ('ANALYSIS_ONLY','SIGNAL_ONLY','PAPER','MICRO_LIVE','FULL_AUTO')),
    execution_result    TEXT,                                -- JSON; NULL до отправки, заполняется один раз
    explanation         TEXT,                                -- асинхронный LLM explain; заполняется один раз
    created_at          INTEGER NOT NULL DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER))  -- Unix ms UTC
) STRICT;

-- Append-only: удаление запрещено; order_id, execution_result, explanation — write-once (NULL → значение);
-- остальные поля неизменяемы
CREATE TRIGGER decision_audit_no_delete BEFORE DELETE ON decision_audit
BEGIN SELECT RAISE(ABORT, 'decision_audit is append-only'); END;

CREATE TRIGGER decision_audit_immutable BEFORE UPDATE ON decision_audit
WHEN NEW.signal_id          IS NOT OLD.signal_id
  OR NEW.fused_signal_id    IS NOT OLD.fused_signal_id
  OR NEW.app_version        IS NOT OLD.app_version
  OR NEW.strategy_version   IS NOT OLD.strategy_version
  OR NEW.ml_model_version   IS NOT OLD.ml_model_version
  OR NEW.llm_prompt_version IS NOT OLD.llm_prompt_version
  OR NEW.ml_prediction_id   IS NOT OLD.ml_prediction_id
  OR NEW.llm_status         IS NOT OLD.llm_status
  OR NEW.risk_verdict       IS NOT OLD.risk_verdict
  OR NEW.risk_reason        IS NOT OLD.risk_reason
  OR NEW.config_hash        IS NOT OLD.config_hash
  OR NEW.mode               IS NOT OLD.mode
  OR NEW.created_at         IS NOT OLD.created_at
  OR (OLD.order_id         IS NOT NULL AND NEW.order_id         IS NOT OLD.order_id)
  OR (OLD.execution_result IS NOT NULL AND NEW.execution_result IS NOT OLD.execution_result)
  OR (OLD.explanation      IS NOT NULL AND NEW.explanation      IS NOT OLD.explanation)
BEGIN SELECT RAISE(ABORT, 'decision_audit row is immutable'); END;

-- ---------------------------------------------------------------------------
-- Журнал событий и состояние
-- ---------------------------------------------------------------------------

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    actor       TEXT    NOT NULL,          -- "system" | "cli:<user>" | "api:<token_id>" | module name
    event       TEXT    NOT NULL,          -- перечень — ARCHITECTURE.md §3.2
    severity    TEXT    NOT NULL CHECK (severity IN ('DEBUG','INFO','WARN','ERROR','CRITICAL')),
    ref_type    TEXT,                      -- signal | order | fused_signal | mode | cb | news
    ref_id      TEXT,
    payload_json TEXT   NOT NULL DEFAULT '{}',
    prev_hash   TEXT,                      -- hash-chain для обнаружения подмены
    row_hash    TEXT    NOT NULL
) STRICT;
CREATE INDEX ix_audit_ts ON audit_log(ts);
CREATE INDEX ix_audit_ref ON audit_log(ref_type, ref_id);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE mode_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    mode            TEXT    NOT NULL,
    changed_ts      INTEGER NOT NULL,
    changed_by      TEXT    NOT NULL,
    micro_first_order_confirmed INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE cb_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    state           TEXT    NOT NULL CHECK (state IN ('ARMED','TRIPPED','KILLED')),
    reason          TEXT,
    tripped_ts      INTEGER,
    reset_by        TEXT,
    reset_ts        INTEGER
) STRICT;
