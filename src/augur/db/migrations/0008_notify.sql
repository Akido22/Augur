-- 0008_notify.sql
-- compat: expand
-- db_schema_version 8 (Augur 0.11.0)
--
-- Алерты (ARCHITECTURE.md §2.11, ML_LLM_FUSION_SPEC.md §5.4):
--   notifications — outbox доставки. Источник — события append-only audit_log: одна строка на
--                   событие (UNIQUE audit_id), отправка с ретраями; сбой канала не блокирует
--                   торговый контур. Статусы: pending → sent | failed (исчерпаны попытки)
--                   | skipped (канал не настроен).
--   notify_cursor — последний просмотренный audit_log.id (события до него уже разобраны).
-- Тексты алертов собираются из payload аудита; секреты в audit_log не пишутся.
-- Время — INTEGER Unix ms UTC.

CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY,
    audit_id        INTEGER NOT NULL UNIQUE REFERENCES audit_log(id),
    created_ts      INTEGER NOT NULL,
    level           TEXT    NOT NULL CHECK (level IN ('INFO','WARN','CRITICAL')),
    kind            TEXT    NOT NULL,                  -- событие аудита: CB_TRIPPED, MODE_CHANGED…
    title           TEXT    NOT NULL,
    body            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sent','failed','skipped')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts INTEGER NOT NULL,
    last_error      TEXT,                              -- тип ошибки / HTTP-код, без URL и токена
    sent_ts         INTEGER
) STRICT;
CREATE INDEX ix_notifications_pending ON notifications(status, next_attempt_ts);

CREATE TABLE notify_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_audit_id   INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL
) STRICT;

-- История до миграции алертами не рассылается: курсор начинается с текущего конца audit_log.
INSERT INTO notify_cursor(id, last_audit_id, updated_ts)
SELECT 1, COALESCE(MAX(id), 0), CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)
FROM audit_log;
