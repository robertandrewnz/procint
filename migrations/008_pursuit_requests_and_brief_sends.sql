-- Migration 008: pursuit_requests and brief_sends tables
-- Run once against the production database before deploying this release.

-- ── Pursuit package requests submitted via the portal ────────────────────────
CREATE TABLE IF NOT EXISTS pursuit_requests (
    id               SERIAL PRIMARY KEY,
    client_id        TEXT        NOT NULL,          -- portal username
    notice_id        TEXT        NOT NULL,
    request_type     TEXT        NOT NULL DEFAULT 'pursuit',
    details          TEXT,
    priority         TEXT        NOT NULL DEFAULT 'normal',
    status           TEXT        NOT NULL DEFAULT 'pending',
    -- status values: pending | generating | complete | failed
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    output_path      TEXT,                          -- relative path under ARTEFACTS_DIR
    error_message    TEXT
);

CREATE INDEX IF NOT EXISTS ix_pursuit_requests_client_id
    ON pursuit_requests (client_id);
CREATE INDEX IF NOT EXISTS ix_pursuit_requests_status
    ON pursuit_requests (status);
CREATE INDEX IF NOT EXISTS ix_pursuit_requests_requested_at
    ON pursuit_requests (requested_at DESC);

-- ── Watch brief send log ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS brief_sends (
    id          SERIAL PRIMARY KEY,
    client_id   TEXT        NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sectors     TEXT[],                             -- sectors used to generate
    status      TEXT        NOT NULL DEFAULT 'sent',
    -- status values: sent | failed
    error_msg   TEXT
);

CREATE INDEX IF NOT EXISTS ix_brief_sends_client_id
    ON brief_sends (client_id);
CREATE INDEX IF NOT EXISTS ix_brief_sends_sent_at
    ON brief_sends (sent_at DESC);
