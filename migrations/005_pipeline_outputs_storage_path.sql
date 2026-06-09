-- Migration 005: add storage_path column to pipeline_outputs
-- Stores the Supabase Storage path alongside DB content so portal.py
-- can locate files via local filesystem → Storage → DB fallback chain.

ALTER TABLE pipeline_outputs
    ADD COLUMN IF NOT EXISTS storage_path TEXT;

CREATE INDEX IF NOT EXISTS idx_pipeline_outputs_storage_path
    ON pipeline_outputs (storage_path)
    WHERE storage_path IS NOT NULL;
