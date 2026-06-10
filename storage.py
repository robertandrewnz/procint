"""
Supabase Storage client — upload, download, and list generated artefacts.

All functions log errors and return None / False / [] on failure so that
file generation never fails due to storage errors.

Storage path structure within the bucket:
  pursuits/<client_slug>/<filename>.html
  competitors/<client_slug>/<filename>.html
  briefs/<client_slug>/<filename>.html
  watchlist/<filename>.html
  demo/<filename>.html  (or .pdf)

Set SUPABASE_URL, SUPABASE_SERVICE_KEY, and STORAGE_BUCKET in .env.
If those vars are absent, all functions are no-ops that return
None / False / [] and log at DEBUG level — no exceptions raised.
"""
import logging
from typing import List, Optional

import config

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    """Return a cached Supabase client, or None if credentials are missing."""
    global _client
    if _client is not None:
        return _client
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        logger.debug(
            "Supabase Storage not configured — set SUPABASE_URL and "
            "SUPABASE_SERVICE_KEY to enable persistent file storage"
        )
        return None
    try:
        from supabase import create_client  # type: ignore
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
        logger.debug("Supabase client created for bucket '%s'", config.STORAGE_BUCKET)
        return _client
    except Exception as exc:
        logger.error("Failed to create Supabase client: %s", exc)
        return None


def upload_file(
    local_path: str,
    storage_path: str,
    content_type: str = "text/html",
) -> Optional[str]:
    """
    Upload a file to Supabase Storage.
    storage_path is the path within the bucket,
    e.g. 'pursuits/my_client/34146041_pursuit_package.html'.
    Returns the storage path on success, None on failure.
    Logs errors but does not raise — file generation should not fail
    due to storage errors.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        with open(local_path, "rb") as f:
            file_bytes = f.read()
        client.storage.from_(config.STORAGE_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        logger.info(
            "Uploaded to Storage: %s (%d bytes)", storage_path, len(file_bytes)
        )
        return storage_path
    except Exception as exc:
        logger.error("Storage upload failed for %s: %s", storage_path, exc)
        return None


def upload_bytes(
    data: bytes,
    storage_path: str,
    content_type: str = "application/octet-stream",
) -> Optional[str]:
    """
    Upload raw bytes to Supabase Storage without a local file.
    Returns the storage path on success, None on failure.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        client.storage.from_(config.STORAGE_BUCKET).upload(
            path=storage_path,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        logger.info("Uploaded bytes to Storage: %s (%d bytes)", storage_path, len(data))
        return storage_path
    except Exception as exc:
        logger.error("Storage upload_bytes failed for %s: %s", storage_path, exc)
        return None


def download_file(storage_path: str) -> Optional[bytes]:
    """
    Download a file from Supabase Storage.
    Returns file bytes on success, None if not found.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        data = client.storage.from_(config.STORAGE_BUCKET).download(storage_path)
        return data
    except Exception as exc:
        logger.error("Storage download failed for %s: %s", storage_path, exc)
        return None


def file_exists(storage_path: str) -> bool:
    """
    Check if a file exists in Supabase Storage.
    Returns False on any error.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        parts = storage_path.rsplit("/", 1)
        prefix = parts[0] if len(parts) == 2 else ""
        name = parts[-1]
        files = client.storage.from_(config.STORAGE_BUCKET).list(prefix) or []
        return any(f.get("name") == name for f in files)
    except Exception as exc:
        logger.error("Storage file_exists check failed for %s: %s", storage_path, exc)
        return False


def get_signed_url(
    storage_path: str,
    expires_in: int = 3600,
) -> Optional[str]:
    """
    Generate a signed URL for temporary file access.
    expires_in is seconds. Default 1 hour.
    Returns URL string or None on failure.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        result = client.storage.from_(config.STORAGE_BUCKET).create_signed_url(
            storage_path, expires_in
        )
        if isinstance(result, dict):
            return result.get("signedURL") or result.get("signed_url")
        return str(result) if result else None
    except Exception as exc:
        logger.error(
            "Storage signed URL failed for %s: %s", storage_path, exc
        )
        return None


def list_files(prefix: str = "") -> List[str]:
    """
    List files in the bucket at the given prefix (one directory level).
    e.g. list_files('pursuits/') returns immediate children of pursuits/.
    Returns full paths (prefix + name) for each item. Returns empty list on error.
    """
    client = _get_client()
    if client is None:
        return []
    try:
        prefix = prefix.rstrip("/")
        items = client.storage.from_(config.STORAGE_BUCKET).list(prefix) or []
        results = []
        for item in items:
            name = item.get("name", "")
            if name:
                full_path = f"{prefix}/{name}" if prefix else name
                results.append(full_path)
        return results
    except Exception as exc:
        logger.error(
            "Storage list_files failed for prefix '%s': %s", prefix, exc
        )
        return []
