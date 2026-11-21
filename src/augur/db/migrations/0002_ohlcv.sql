-- 0002_ohlcv.sql
-- compat: expand
-- db_schema_version 2 (Augur 0.2.0)
--
-- Хранилище OHLCV — DDL из ARCHITECTURE.md §3.2. Время — INTEGER, Unix ms UTC.
-- Цены в ohlcv — REAL (аналитика); деньги в торговых таблицах — TEXT/Decimal.
-- Идемпотентность загрузки: PK (venue, symbol, timeframe, ts_open) + INSERT ... ON CONFLICT.

CREATE TABLE ohlcv (
    venue       TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    ts_open     INTEGER NOT NULL,          -- Unix ms UTC
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    ingested_ts INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, timeframe, ts_open)
) STRICT, WITHOUT ROWID;
-- PK покрывает range-запросы: WHERE venue=? AND symbol=? AND timeframe=? AND ts_open BETWEEN ? AND ?
