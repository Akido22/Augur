-- 0006_news.sql
-- compat: expand
-- db_schema_version 6 (Augur 0.8.0)
--
-- Новостной контур, ARCHITECTURE.md §8, §3.2:
--   news           — нормализованные новости; дедуп по content_hash и url (§8.2).
--                    DDL §3.2 + confidence и importance (нужны news_event §8.3 и реакциям §8.4).
--   news_reactions — применённые реакции NewsReactionRules (§8.4) со сроком действия.
--                    Источник R16 (pause_symbol / pause_strategy) и news-флагов R15/soft
--                    (add_risk_flag). Строки не удаляются: истекают по expires_ts или снимаются
--                    человеком (lifted_ts, lifted_by; аудит NEWS_REACTION_LIFTED).
-- Время — INTEGER Unix ms UTC.

CREATE TABLE news (
    id               TEXT    PRIMARY KEY,     -- ULID
    source           TEXT    NOT NULL,        -- finnhub | newsapi | alphavantage
    external_id      TEXT,
    url              TEXT,
    content_hash     TEXT    NOT NULL,        -- sha256(normalized title + source domain)
    title            TEXT    NOT NULL,
    summary          TEXT,
    published_ts     INTEGER NOT NULL,
    fetched_ts       INTEGER NOT NULL,
    symbols_json     TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(symbols_json)),
    sentiment        REAL    CHECK (sentiment IS NULL OR sentiment BETWEEN -1 AND 1),
    sentiment_method TEXT,                    -- "rule:v1" | "fusion:<ref>" (0.10.0)
    relevance        REAL,
    confidence       REAL    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    importance       TEXT    CHECK (importance IS NULL OR importance IN ('low','medium','high'))
) STRICT;
CREATE UNIQUE INDEX ux_news_hash ON news(content_hash);
CREATE UNIQUE INDEX ux_news_url ON news(url) WHERE url IS NOT NULL;
CREATE INDEX ix_news_published ON news(published_ts);

CREATE TABLE news_reactions (
    id            TEXT    PRIMARY KEY,        -- ULID
    rule          TEXT    NOT NULL,           -- имя правила из news.rules
    action        TEXT    NOT NULL CHECK (action IN ('pause_symbol','pause_strategy','add_risk_flag')),
    symbol        TEXT,                       -- pause_symbol / add_risk_flag
    strategy      TEXT,                       -- pause_strategy: имя или name@X.Y.Z
    flag_code     TEXT,
    flag_severity TEXT    CHECK (flag_severity IS NULL OR flag_severity IN ('low','medium','high')),
    news_id       TEXT    NOT NULL REFERENCES news(id),
    created_ts    INTEGER NOT NULL,
    expires_ts    INTEGER NOT NULL CHECK (expires_ts > created_ts),
    lifted_ts     INTEGER,
    lifted_by     TEXT,
    CHECK ((action = 'pause_strategy') = (strategy IS NOT NULL)),
    CHECK ((action = 'add_risk_flag') = (flag_code IS NOT NULL AND flag_severity IS NOT NULL)),
    CHECK (action = 'pause_strategy' OR symbol IS NOT NULL)
) STRICT;
CREATE INDEX ix_news_reactions_active ON news_reactions(expires_ts) WHERE lifted_ts IS NULL;
