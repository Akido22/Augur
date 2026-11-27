-- 0005_execution.sql
-- compat: expand
-- db_schema_version 5 (Augur 0.6.0)
--
-- Execution (PAPER), ARCHITECTURE.md §7.3:
--   fills — исполнения ордеров; дедуп по (order_id, venue_fill_id) при реконнекте и повторной
--           обработке ответа площадки. Позиция (positions) обновляется по каждому новому fill.
-- DDL — ARCHITECTURE.md §3.2 без изменений. orders, cb_state, mode_state созданы в 0001_core.
-- Деньги — TEXT с десятичной строкой (Decimal), время — INTEGER Unix ms UTC.

CREATE TABLE fills (
    id             TEXT    PRIMARY KEY,
    order_id       TEXT    NOT NULL REFERENCES orders(id),
    venue_fill_id  TEXT    NOT NULL,
    qty            TEXT    NOT NULL,
    price          TEXT    NOT NULL,
    fee            TEXT    NOT NULL DEFAULT '0',
    ts             INTEGER NOT NULL
) STRICT;
CREATE UNIQUE INDEX ux_fills_venue ON fills(order_id, venue_fill_id);  -- дедуп при реконнекте
