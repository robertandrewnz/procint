-- Procint Layer 1 schema
-- Run once against your PostgreSQL database before first pipeline execution.

CREATE TABLE IF NOT EXISTS raw_notices (
    id              SERIAL PRIMARY KEY,
    notice_id       TEXT UNIQUE NOT NULL,          -- GETS internal identifier
    source_url      TEXT NOT NULL,
    title           TEXT,
    agency          TEXT,
    category_raw    TEXT,
    estimated_value TEXT,
    open_date       DATE,
    close_date      DATE,
    description     TEXT,
    raw_html        TEXT,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS parsed_notices (
    id                  SERIAL PRIMARY KEY,
    notice_id           TEXT UNIQUE NOT NULL REFERENCES raw_notices(notice_id),
    agency_name         TEXT,
    sector_tag          TEXT,
    value_band          TEXT,
    estimated_value_min NUMERIC,
    estimated_value_max NUMERIC,
    contract_duration   TEXT,
    geographic_scope    TEXT,
    evaluation_criteria TEXT,
    close_date          DATE,
    days_until_close    INTEGER,
    parsed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scored_notices (
    id                  SERIAL PRIMARY KEY,
    notice_id           TEXT UNIQUE NOT NULL REFERENCES raw_notices(notice_id),
    score_value         NUMERIC(4,2),
    score_sector        NUMERIC(4,2),
    score_complexity    NUMERIC(4,2),
    score_urgency       NUMERIC(4,2),
    composite_score     NUMERIC(4,2),
    score_reasoning     TEXT,
    scored_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS enriched_notices (
    id                      SERIAL PRIMARY KEY,
    notice_id               TEXT UNIQUE NOT NULL REFERENCES raw_notices(notice_id),
    summary                 TEXT,
    evaluation_weighting    TEXT,
    red_flags               TEXT,
    strategic_framing       TEXT,
    enriched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bidder_pool (
    id              SERIAL PRIMARY KEY,
    notice_id       TEXT NOT NULL REFERENCES raw_notices(notice_id),
    firm_name       TEXT NOT NULL,
    sector          TEXT,
    size            TEXT,
    strategic_importance TEXT,   -- high / medium / low
    intelligence_maturity TEXT,  -- strong / moderate / weak
    UNIQUE (notice_id, firm_name)
);

CREATE INDEX IF NOT EXISTS idx_raw_notices_notice_id   ON raw_notices(notice_id);
CREATE INDEX IF NOT EXISTS idx_scored_notices_score    ON scored_notices(composite_score DESC);
CREATE INDEX IF NOT EXISTS idx_bidder_pool_notice_id   ON bidder_pool(notice_id);
