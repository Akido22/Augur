-- 0004_risk_state.sql
-- compat: expand
-- db_schema_version 4 (Augur 0.5.0)
--
-- Состояние портфеля для Risk Gate (ARCHITECTURE.md §2.8, §7.2):
--   positions           — R11 max_open_positions, R12 concentration, размер CLOSE/REDUCE;
--   portfolio_snapshots — R10 daily_loss (day_pnl, equity), drawdown для CB (0.6.0+).
-- DDL — ARCHITECTURE.md §3.2 без изменений. fills — миграция этапа 0.6.0 (PAPER).
-- Деньги — TEXT с десятичной строкой (Decimal), время — INTEGER Unix ms UTC.
-- Risk Gate эти таблицы только читает; пишут Execution / PostTradeMonitor (0.6.0).

CREATE TABLE positions (
    venue          TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    mode           TEXT    NOT NULL,
    qty            TEXT    NOT NULL,       -- знак = направление
    avg_price      TEXT    NOT NULL,
    realized_pnl   TEXT    NOT NULL DEFAULT '0',
    unrealized_pnl TEXT    NOT NULL DEFAULT '0',
    stop_loss      TEXT,
    take_profit    TEXT,
    opened_ts      INTEGER,
    updated_ts     INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, mode)
) STRICT;

CREATE TABLE portfolio_snapshots (
    ts             INTEGER NOT NULL,
    mode           TEXT    NOT NULL,
    equity         TEXT    NOT NULL,
    cash           TEXT    NOT NULL,
    margin_used    TEXT    NOT NULL DEFAULT '0',
    day_pnl        TEXT    NOT NULL,
    peak_equity    TEXT    NOT NULL,
    drawdown_pct   TEXT    NOT NULL,
    positions_json TEXT    NOT NULL,
    PRIMARY KEY (mode, ts)
) STRICT, WITHOUT ROWID;
