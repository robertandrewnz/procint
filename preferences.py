"""
preferences.py — User preferences storage and retrieval.

User identity key: username string from portal_config.json
(e.g. "robert", "admin"). Not a UUID — the portal uses JSON-file auth.

Tables created by migrations/004_user_preferences.sql.
"""
from __future__ import annotations

import logging
from typing import Optional

import db

logger = logging.getLogger(__name__)

_DEFAULT: dict = {
    "user_id":       None,
    "sectors":       [],
    "agency_focus":  [],
    "min_value_nzd": 0,
}


def get_user_preferences(user_id: str) -> dict:
    """
    Retrieve preferences for *user_id*.

    Returns a dict with keys: user_id, sectors, agency_focus, min_value_nzd.
    If no row exists yet, returns the safe default (empty / neutral).
    """
    if not user_id:
        return dict(_DEFAULT)
    try:
        row = db.fetchone(
            "SELECT user_id, sectors, agency_focus, min_value_nzd "
            "FROM user_preferences WHERE user_id = %s",
            (user_id,),
        )
        if row:
            return {
                "user_id":       row["user_id"],
                "sectors":       list(row["sectors"] or []),
                "agency_focus":  list(row["agency_focus"] or []),
                "min_value_nzd": int(row["min_value_nzd"] or 0),
            }
    except Exception as exc:
        logger.warning("get_user_preferences(%s): %s", user_id, exc)
    return {**_DEFAULT, "user_id": user_id}


def save_user_preferences(
    user_id: str,
    sectors: Optional[list[str]] = None,
    agency_focus: Optional[list[str]] = None,
    min_value_nzd: int = 0,
) -> None:
    """
    Upsert preferences for *user_id*.

    Any parameter left as None keeps its existing DB value (partial update).
    """
    if not user_id:
        raise ValueError("user_id is required")

    # Build a COALESCE-based upsert so callers can update one field at a time.
    try:
        db.execute(
            """
            INSERT INTO user_preferences (user_id, sectors, agency_focus, min_value_nzd, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                sectors       = COALESCE(EXCLUDED.sectors,      user_preferences.sectors),
                agency_focus  = COALESCE(EXCLUDED.agency_focus, user_preferences.agency_focus),
                min_value_nzd = COALESCE(EXCLUDED.min_value_nzd, user_preferences.min_value_nzd),
                updated_at    = NOW()
            """,
            (
                user_id,
                sectors,
                agency_focus,
                min_value_nzd,
            ),
        )
        logger.info("Saved preferences for %s: sectors=%s", user_id, sectors)
    except Exception as exc:
        logger.error("save_user_preferences(%s): %s", user_id, exc)
        raise


def has_preferences(user_id: str) -> bool:
    """Return True if a non-empty preferences row exists for *user_id*."""
    if not user_id:
        return False
    try:
        row = db.fetchone(
            "SELECT sectors FROM user_preferences WHERE user_id = %s",
            (user_id,),
        )
        return bool(row and row.get("sectors"))
    except Exception as exc:
        logger.warning("has_preferences(%s): %s", user_id, exc)
        return False
