-- Migration 003: MBIE GETS open data — historical procurement tables
-- Source: MBIE NZGPP GETS Open Data (2014–2025)
-- Safe to re-run (IF NOT EXISTS throughout).

-- ── mbie_award_notices ────────────────────────────────────────────────────────
-- Merged current + historic award notice files. Primary key is rfx_id which
-- is GETS's own identifier. Deduplication on rfx_id keeps the most recent row.

CREATE TABLE IF NOT EXISTS mbie_award_notices (
    rfx_id              TEXT PRIMARY KEY,
    posting_agency      TEXT,
    rfx_type            TEXT,           -- Request for Tenders / Request for Proposals / etc.
    competition_type    TEXT,           -- Open Competition / Sole Source / etc.
    title               TEXT,
    reference_number    TEXT,
    open_date           DATE,
    close_date          DATE,
    awarded_date        DATE,
    department          TEXT,
    tender_coverage     TEXT,
    overview            TEXT,
    award_type          TEXT,           -- Awarded / Not Awarded
    awarded_amount      NUMERIC,        -- NZD, may be NULL for historic
    is_awarded          BOOLEAN NOT NULL DEFAULT FALSE,  -- computed: amount>0 or award_type=Awarded
    source_file         TEXT NOT NULL,  -- 'current' or 'historic'
    report_date         DATE,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mbie_notices_agency    ON mbie_award_notices(posting_agency);
CREATE INDEX IF NOT EXISTS idx_mbie_notices_awarded   ON mbie_award_notices(is_awarded);
CREATE INDEX IF NOT EXISTS idx_mbie_notices_date      ON mbie_award_notices(awarded_date DESC);


-- ── mbie_award_suppliers ──────────────────────────────────────────────────────
-- Winning / participating suppliers per tender. One rfx_id may have multiple
-- supplier rows (multi-supplier awards or all bidders in some data sets).

CREATE TABLE IF NOT EXISTS mbie_award_suppliers (
    id              SERIAL PRIMARY KEY,
    rfx_id          TEXT NOT NULL REFERENCES mbie_award_notices(rfx_id) ON DELETE CASCADE,
    supplier_nzbn   TEXT,
    business_name   TEXT,
    full_address    TEXT,
    country         TEXT,
    website         TEXT,
    source_file     TEXT NOT NULL,
    UNIQUE (rfx_id, business_name)
);

CREATE INDEX IF NOT EXISTS idx_mbie_suppliers_rfx      ON mbie_award_suppliers(rfx_id);
CREATE INDEX IF NOT EXISTS idx_mbie_suppliers_name     ON mbie_award_suppliers(business_name);
CREATE INDEX IF NOT EXISTS idx_mbie_suppliers_nzbn     ON mbie_award_suppliers(supplier_nzbn);


-- ── mbie_award_categories ─────────────────────────────────────────────────────
-- UNSPSC product category classifications per tender.

CREATE TABLE IF NOT EXISTS mbie_award_categories (
    id              SERIAL PRIMARY KEY,
    rfx_id          TEXT NOT NULL REFERENCES mbie_award_notices(rfx_id) ON DELETE CASCADE,
    unspsc_code     TEXT,
    unspsc_desc     TEXT,
    sector_tag      TEXT,   -- mapped to our taxonomy: FM/infrastructure/ICT/etc.
    UNIQUE (rfx_id, unspsc_code)
);

CREATE INDEX IF NOT EXISTS idx_mbie_categories_rfx    ON mbie_award_categories(rfx_id);
CREATE INDEX IF NOT EXISTS idx_mbie_categories_sector ON mbie_award_categories(sector_tag);


-- ── mbie_award_regions ────────────────────────────────────────────────────────
-- Geographic regions per tender (one tender may cover multiple regions).

CREATE TABLE IF NOT EXISTS mbie_award_regions (
    id      SERIAL PRIMARY KEY,
    rfx_id  TEXT NOT NULL REFERENCES mbie_award_notices(rfx_id) ON DELETE CASCADE,
    region  TEXT,
    UNIQUE (rfx_id, region)
);

CREATE INDEX IF NOT EXISTS idx_mbie_regions_rfx    ON mbie_award_regions(rfx_id);
CREATE INDEX IF NOT EXISTS idx_mbie_regions_region ON mbie_award_regions(region);


-- ── supplier_win_history (materialised view) ──────────────────────────────────
-- Pre-aggregated win history per supplier for fast bidder inference queries.
-- Rebuilt by historical_data.py after each full load.

CREATE MATERIALIZED VIEW IF NOT EXISTS supplier_win_history AS
SELECT
    s.business_name                            AS supplier_name,
    COUNT(DISTINCT n.rfx_id)                   AS total_wins,
    SUM(n.awarded_amount)                      AS total_contract_value,
    AVG(n.awarded_amount)                      AS avg_contract_value,
    MIN(n.awarded_date)                        AS first_win_date,
    MAX(n.awarded_date)                        AS last_win_date,
    -- Sector breakdown as JSON: [{"sector": "infrastructure", "wins": 14}]
    json_agg(DISTINCT jsonb_build_object(
        'sector', COALESCE(c.sector_tag, 'other'),
        'unspsc_desc', c.unspsc_desc
    )) FILTER (WHERE c.sector_tag IS NOT NULL)  AS sectors_json,
    -- Agency breakdown as JSON: [{"agency": "NZTA", "wins": 3}]
    json_agg(DISTINCT jsonb_build_object(
        'agency', n.posting_agency
    ))                                          AS agencies_json,
    -- Region breakdown
    array_agg(DISTINCT r.region) FILTER (
        WHERE r.region IS NOT NULL AND r.region NOT IN ('NULL', 'International')
    )                                           AS regions,
    -- Most recent sector won (for quick sector match)
    (SELECT c2.sector_tag
       FROM mbie_award_notices n2
       JOIN mbie_award_suppliers s2 ON s2.rfx_id = n2.rfx_id
         AND s2.business_name = s.business_name
       JOIN mbie_award_categories c2 ON c2.rfx_id = n2.rfx_id
      WHERE n2.is_awarded
        AND c2.sector_tag IS NOT NULL
      ORDER BY n2.awarded_date DESC NULLS LAST
      LIMIT 1
    )                                           AS primary_sector
FROM mbie_award_suppliers s
JOIN mbie_award_notices n    ON n.rfx_id = s.rfx_id AND n.is_awarded
LEFT JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
LEFT JOIN mbie_award_regions r    ON r.rfx_id = n.rfx_id
WHERE s.business_name IS NOT NULL
  AND s.business_name NOT IN ('', 'NULL')
GROUP BY s.business_name
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS idx_swh_supplier ON supplier_win_history(supplier_name);
CREATE INDEX IF NOT EXISTS idx_swh_sector ON supplier_win_history(primary_sector);
CREATE INDEX IF NOT EXISTS idx_swh_wins ON supplier_win_history(total_wins DESC);
