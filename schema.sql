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
    id                   SERIAL PRIMARY KEY,
    notice_id            TEXT NOT NULL REFERENCES raw_notices(notice_id),
    firm_name            TEXT NOT NULL,
    sector               TEXT,
    size                 TEXT,
    strategic_importance TEXT,          -- high / medium / low
    intelligence_maturity TEXT,         -- strong / moderate / weak
    relevance_score      NUMERIC(5,4),  -- 0.0000–1.0000 keyword cosine similarity
    match_type           TEXT,          -- exact / cross_sector
    reasoning            TEXT,          -- pipe-separated display bullets
    company_context      TEXT,          -- Claude 2-sentence company profile
    context_confidence   TEXT,          -- high / medium / low / unknown
    UNIQUE (notice_id, firm_name)
);

CREATE INDEX IF NOT EXISTS idx_raw_notices_notice_id   ON raw_notices(notice_id);
CREATE INDEX IF NOT EXISTS idx_scored_notices_score    ON scored_notices(composite_score DESC);
CREATE INDEX IF NOT EXISTS idx_bidder_pool_notice_id   ON bidder_pool(notice_id);
-- Migration 002: Layer 2 knowledge graph tables
-- Run against an existing database after schema.sql has been applied.
-- Safe to re-run (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS throughout).

-- ── organisations ─────────────────────────────────────────────────────────────
-- Canonical entity record for every organisation that appears anywhere in the
-- system: procuring agencies, known bidders, award winners, and inferred
-- participants. Grows automatically as Layer 1 runs each day.

CREATE TABLE IF NOT EXISTS organisations (
    org_id               SERIAL PRIMARY KEY,
    name                 TEXT UNIQUE NOT NULL,          -- canonical display name
    org_type             TEXT NOT NULL DEFAULT 'unknown', -- agency / bidder / both / unknown
    sector_tags          TEXT,                          -- pipe-separated sectors
    size                 TEXT,                          -- micro/small/medium/large/major
    headquarters         TEXT,
    nzbn                 TEXT,                          -- NZ Business Number (Layer 2 enrichment)
    website              TEXT,
    first_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notice_count         INTEGER NOT NULL DEFAULT 0,   -- Layer 1 tender notices
    award_count          INTEGER NOT NULL DEFAULT 0,   -- awards won as supplier
    total_awarded_value  NUMERIC,                       -- sum of won contract values
    profile_text         TEXT,                          -- Claude-generated narrative
    profile_generated_at TIMESTAMPTZ,
    discovery_confidence TEXT NOT NULL DEFAULT 'medium', -- high/medium/low
    metadata             JSONB                          -- extensible Layer 2 fields
);

CREATE INDEX IF NOT EXISTS idx_organisations_name       ON organisations(name);
CREATE INDEX IF NOT EXISTS idx_organisations_org_type   ON organisations(org_type);
CREATE INDEX IF NOT EXISTS idx_organisations_last_seen  ON organisations(last_seen DESC);


-- ── name_aliases ──────────────────────────────────────────────────────────────
-- Maps every raw name variant seen in GETS, bidder CSV, or award notices to a
-- canonical org_id. One organisation can have many aliases.

CREATE TABLE IF NOT EXISTS name_aliases (
    alias_id   SERIAL PRIMARY KEY,
    org_id     INTEGER NOT NULL REFERENCES organisations(org_id) ON DELETE CASCADE,
    alias      TEXT UNIQUE NOT NULL,
    source     TEXT NOT NULL,   -- gets_agency / bidder_csv / award_notice / inferred
    confidence TEXT NOT NULL DEFAULT 'high',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_name_aliases_org_id ON name_aliases(org_id);
CREATE INDEX IF NOT EXISTS idx_name_aliases_alias  ON name_aliases(alias);


-- ── contract_awards ───────────────────────────────────────────────────────────
-- Awarded contracts scraped from GETS contract award notices.

CREATE TABLE IF NOT EXISTS contract_awards (
    award_id             SERIAL PRIMARY KEY,
    gets_notice_id       TEXT,                          -- GETS notice ID of award notice
    tender_notice_id     TEXT REFERENCES raw_notices(notice_id), -- linked tender (nullable)
    source_url           TEXT,
    title                TEXT,
    agency_org_id        INTEGER REFERENCES organisations(org_id),
    supplier_org_id      INTEGER REFERENCES organisations(org_id),
    agency_name_raw      TEXT,                          -- raw name before normalisation
    supplier_name_raw    TEXT,
    award_date           DATE,
    contract_value       NUMERIC,
    contract_value_raw   TEXT,
    duration_months      INTEGER,
    start_date           DATE,
    end_date             DATE,                          -- computed: start_date + duration
    sector_tag           TEXT,
    description          TEXT,
    raw_html             TEXT,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (gets_notice_id)
);

CREATE INDEX IF NOT EXISTS idx_contract_awards_agency     ON contract_awards(agency_org_id);
CREATE INDEX IF NOT EXISTS idx_contract_awards_supplier   ON contract_awards(supplier_org_id);
CREATE INDEX IF NOT EXISTS idx_contract_awards_end_date   ON contract_awards(end_date);
CREATE INDEX IF NOT EXISTS idx_contract_awards_sector     ON contract_awards(sector_tag);


-- ── agency_profiles ───────────────────────────────────────────────────────────
-- Computed intelligence about each procuring agency. Rebuilt on each Layer 2 run.

CREATE TABLE IF NOT EXISTS agency_profiles (
    profile_id            SERIAL PRIMARY KEY,
    org_id                INTEGER UNIQUE NOT NULL REFERENCES organisations(org_id),
    total_notices         INTEGER NOT NULL DEFAULT 0,
    total_awards          INTEGER NOT NULL DEFAULT 0,
    total_awarded_value   NUMERIC,
    avg_contract_value    NUMERIC,
    dominant_sectors      TEXT,   -- JSON: [{"sector": "infrastructure", "count": 12}]
    preferred_suppliers   TEXT,   -- JSON: [{"name": "Downer NZ", "award_count": 3}]
    avg_days_to_close     NUMERIC,
    typical_notice_types  TEXT,   -- JSON: [{"type": "RFP", "count": 8}]
    eval_criteria_patterns TEXT,
    renewal_tendency      TEXT,   -- high / medium / low / unknown
    procurement_cadence   TEXT,   -- narrative: "Heavy Q4, light Q1"
    profile_summary       TEXT,   -- Claude-generated 3-sentence narrative
    generated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agency_profiles_org_id ON agency_profiles(org_id);


-- ── relationships ─────────────────────────────────────────────────────────────
-- Connections between organisations: JV partners, known subcontractors,
-- agency→supplier relationships, known competitors.

CREATE TABLE IF NOT EXISTS relationships (
    rel_id             SERIAL PRIMARY KEY,
    org_id_a           INTEGER NOT NULL REFERENCES organisations(org_id),
    org_id_b           INTEGER NOT NULL REFERENCES organisations(org_id),
    relationship_type  TEXT NOT NULL,  -- jv_partner/subcontractor/agency_supplier/competitor
    evidence_notice_id TEXT,
    evidence_award_id  INTEGER REFERENCES contract_awards(award_id),
    strength           TEXT NOT NULL DEFAULT 'inferred', -- confirmed/inferred/weak
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id_a, org_id_b, relationship_type)
);

CREATE INDEX IF NOT EXISTS idx_relationships_org_a ON relationships(org_id_a);
CREATE INDEX IF NOT EXISTS idx_relationships_org_b ON relationships(org_id_b);


-- ── pattern_flags ─────────────────────────────────────────────────────────────
-- Longitudinal alerts generated by the pattern detection module.

CREATE TABLE IF NOT EXISTS pattern_flags (
    flag_id     SERIAL PRIMARY KEY,
    flag_type   TEXT NOT NULL, -- approaching_renewal/procurement_surge/win_streak/sector_spike/loss_streak
    org_id      INTEGER REFERENCES organisations(org_id),
    sector_tag  TEXT,
    notice_id   TEXT,
    award_id    INTEGER REFERENCES contract_awards(award_id),
    description TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'medium', -- high / medium / low
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pattern_flags_type       ON pattern_flags(flag_type);
CREATE INDEX IF NOT EXISTS idx_pattern_flags_org_id     ON pattern_flags(org_id);
CREATE INDEX IF NOT EXISTS idx_pattern_flags_detected   ON pattern_flags(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_pattern_flags_expires    ON pattern_flags(expires_at);
