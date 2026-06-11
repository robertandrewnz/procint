"""
One-shot: apply migration 012 and regenerate the Speech to Text pursuit package.

Run on Railway:
    python3 _regen_34118228.py
"""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NOTICE_ID    = "34118228"
CLIENT_NAME  = "Pacific Transcription NZ"

# ── 1. Apply migration 012 (package_documents) ────────────────────────────────
import db

logger.info("Ensuring package_documents table exists...")
try:
    db.execute("""
        CREATE TABLE IF NOT EXISTS package_documents (
            id           SERIAL PRIMARY KEY,
            notice_id    TEXT        NOT NULL,
            client_slug  TEXT        NOT NULL,
            file_path    TEXT        NOT NULL,
            file_name    TEXT        NOT NULL,
            file_size    INTEGER,
            uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_package_documents_notice ON package_documents (notice_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_package_documents_client ON package_documents (client_slug, notice_id)")
    logger.info("package_documents table OK")
except Exception as e:
    logger.warning("package_documents DDL: %s", e)

# ── 2. Verify notice is in DB ─────────────────────────────────────────────────
row = db.fetchone("SELECT notice_id, title, agency FROM raw_notices WHERE notice_id = %s", (NOTICE_ID,))
if not row:
    logger.error("Notice %s not found in raw_notices — run ingestion first", NOTICE_ID)
    sys.exit(1)
logger.info("Notice: %s — %s (%s)", row["notice_id"], row.get("title","")[:60], row.get("agency",""))

# ── 3. Regenerate pursuit package ─────────────────────────────────────────────
from pursuit_package import generate_pursuit_package, _artefact_dir

output_dir = _artefact_dir(CLIENT_NAME)
logger.info("Output dir: %s", output_dir)

try:
    path = generate_pursuit_package(
        notice_id=NOTICE_ID,
        client_name=CLIENT_NAME,
        output_dir=output_dir,
        preferred_sectors=["ICT"],
    )
    logger.info("SUCCESS — package written to %s", path)
    logger.info("File size: %d bytes", path.stat().st_size)
except Exception as exc:
    logger.exception("Generation failed: %s", exc)
    sys.exit(1)

logger.info("Done. Check portal pursuits page for '%s'", CLIENT_NAME)
