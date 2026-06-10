-- Migration 012: package_documents — uploaded tender documents for Full Analysis upgrade
-- Stores metadata for files uploaded by clients to augment pursuit packages.
-- Files are stored in Supabase Storage at uploads/<client_slug>/<notice_id>/<filename>.

CREATE TABLE IF NOT EXISTS package_documents (
    id           SERIAL PRIMARY KEY,
    notice_id    TEXT        NOT NULL,
    client_slug  TEXT        NOT NULL,
    file_path    TEXT        NOT NULL,   -- Supabase Storage path
    file_name    TEXT        NOT NULL,
    file_size    INTEGER,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_package_documents_notice
    ON package_documents (notice_id);

CREATE INDEX IF NOT EXISTS idx_package_documents_client
    ON package_documents (client_slug, notice_id);
