-- Migration 009: leads table + plan/billing fields
-- Run once against the production database.

-- ── Signup leads ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leads (
    id           SERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    organisation TEXT,
    role         TEXT,
    email        TEXT        NOT NULL,
    phone        TEXT,
    sectors      TEXT,                     -- free text from form
    plan         TEXT        NOT NULL DEFAULT 'watch',
    source       TEXT        NOT NULL DEFAULT 'signup_form',
    status       TEXT        NOT NULL DEFAULT 'enquiry',
    -- status: enquiry | approved | rejected | duplicate
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes        TEXT,
    portal_username TEXT      -- set on approval
);

CREATE INDEX IF NOT EXISTS ix_leads_status     ON leads (status);
CREATE INDEX IF NOT EXISTS ix_leads_created_at ON leads (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_leads_email      ON leads (email);

-- ── Pipeline run log ─────────────────────────────────────────────────────────
-- Lightweight table for the admin Pipeline page; scheduler writes here.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id          SERIAL PRIMARY KEY,
    stage       TEXT        NOT NULL,  -- layer1 | layer2 | watch_brief
    triggered_by TEXT       NOT NULL DEFAULT 'scheduler',  -- scheduler | admin
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL DEFAULT 'running',    -- running | complete | failed
    summary     TEXT
);

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_started ON pipeline_runs (started_at DESC);
