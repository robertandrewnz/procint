"""
Groundwork QA Audit — data quality report for watchlist notices and pursuit packages.

Usage:
    railway run python3 scripts/qa_audit.py
    python3 scripts/qa_audit.py          (with DATABASE_URL set locally)

Checks all active watchlist notices and recent pursuit packages.
Prints a plain-text report grouped by issue type. Does NOT modify any data.
"""

import re
import sys
import os
from datetime import date, timedelta

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config
from bidders import SECTOR_EXCLUSION_MATRIX

# ── Constants ──────────────────────────────────────────────────────────────────

TODAY = date.today()
STALE_ENRICHMENT_DAYS = 30  # flag if closing within this many days but still unenriched
PURSUIT_LOOKBACK_DAYS = 90  # how far back to check pursuit packages

# Regex to detect date-like patterns in overview text
# Matches: "12 June 2025", "12/06/2025", "12-06-2025", "June 12, 2025", "12th June 2025"
_DATE_PATTERN = re.compile(
    r"\b(\d{1,2}[\s/\-]\w+[\s/\-]\d{4}|\w+\s+\d{1,2}[,\s]+\d{4}|\d{1,2}/\d{1,2}/\d{4})\b",
    re.IGNORECASE,
)

# Date-keyword labels that indicate structured dates are referenced in text
_DATE_LABEL_PATTERN = re.compile(
    r"(briefing|site\s+visit|hui|questions?\s+due|queries?\s+due|registration|expressions?\s+of\s+interest|EOI|close|submission)",
    re.IGNORECASE,
)

# Physical works sectors — should not appear in non-construction notices
_PHYSICAL_WORKS = {"construction", "roading", "civil", "infrastructure", "FM"}
_PHYSICAL_TITLE_SIGNALS = {
    "building", "construct", "infrastructure", "roading", "maintenance",
    "civil", "facility", "upgrade", "installation", "earthworks", "structural",
    "bridge", "pavement", "drainage", "demolition", "fitout",
}

# Phrases in pursuit package HTML that indicate incumbent was not found
_INCUMBENT_NOT_FOUND_PATTERNS = [
    "no current system or provider identified",
    "no incumbent identified",
    "incumbent not identified",
    "no named incumbent",
    "not identifiable",
    "could not be identified",
    "incumbent: unknown",
    "incumbent: none",
]

# Bad client name values for pursuit packages
_BAD_CLIENT_NAMES = {"bidedge admin", "admin", ""}


# ── Findings collector ────────────────────────────────────────────────────────

class Findings:
    def __init__(self):
        self._items: list[dict] = []

    def add(self, check: str, notice_id: str, title: str, description: str):
        self._items.append({
            "check": check,
            "notice_id": notice_id,
            "title": title,
            "description": description,
        })

    def grouped(self) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        for item in self._items:
            result.setdefault(item["check"], []).append(item)
        return result

    def __len__(self):
        return len(self._items)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _truncate(s: str, n: int = 70) -> str:
    s = (s or "").strip()
    return s[:n] + "…" if len(s) > n else s


def _sector_keywords_hit(sector: str, text: str) -> int:
    """Return number of sector keyword hits in text (case-insensitive)."""
    kws = config.SECTOR_KEYWORDS.get(sector, [])
    text_lower = text.lower()
    return sum(1 for kw in kws if kw.lower() in text_lower)


def _text_has_date_references(overview_text: str) -> bool:
    """Return True if overview_text contains date-like strings near date-type labels."""
    if not overview_text:
        return False
    # Look for date labels and date patterns within 200 chars of each other
    label_matches = list(_DATE_LABEL_PATTERN.finditer(overview_text))
    date_matches = list(_DATE_PATTERN.finditer(overview_text))
    if not label_matches or not date_matches:
        return False
    for lm in label_matches:
        for dm in date_matches:
            if abs(lm.start() - dm.start()) < 200:
                return True
    return False


def _incumbent_not_found_in_html(content: str) -> bool:
    """Return True if the pursuit package HTML indicates no incumbent was identified."""
    if not content:
        return False
    content_lower = content.lower()
    return any(pat in content_lower for pat in _INCUMBENT_NOT_FOUND_PATTERNS)


# ── CHECK 1: Likely bidder sector relevance ───────────────────────────────────

def check_bidder_relevance(findings: Findings):
    """Flag notices where MBIE bidders are from a clearly wrong sector."""
    rows = db.fetchall(
        """
        SELECT
            bp.notice_id,
            r.title          AS notice_title,
            r.agency,
            p.sector_tag     AS notice_sector,
            r.title || ' ' || COALESCE(r.description, '') AS combined_text,
            bp.firm_name,
            bp.match_type,
            wh.primary_sector AS firm_sector
        FROM bidder_pool bp
        JOIN parsed_notices p  ON p.notice_id = bp.notice_id
        JOIN raw_notices r     ON r.notice_id = bp.notice_id
        LEFT JOIN supplier_win_history wh ON wh.supplier_name = bp.firm_name
        WHERE bp.match_type = 'mbie_evidence'
          AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
          AND EXISTS (
              SELECT 1 FROM scored_notices s
               WHERE s.notice_id = bp.notice_id
                 AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
          )
        ORDER BY bp.notice_id, bp.firm_name
        """,
        (config.PRIORITY_THRESHOLD,),
    )

    for row in rows:
        notice_id = row["notice_id"]
        firm_name = row["firm_name"]
        firm_sector = (row.get("firm_sector") or "").lower().strip()
        notice_sector = (row.get("notice_sector") or "other").lower().strip()
        notice_title = row.get("notice_title") or ""
        combined_text = (row.get("combined_text") or "").lower()

        bad = False
        reason = ""

        # Rule 1: sector exclusion matrix
        if firm_sector and firm_sector in SECTOR_EXCLUSION_MATRIX.get(notice_sector, set()):
            bad = True
            reason = (
                f"Firm sector '{firm_sector}' is excluded from notice sector "
                f"'{notice_sector}' by exclusion matrix"
            )

        # Rule 2: physical works firm in 'other' sector with no construction title keywords
        if (
            not bad
            and notice_sector in ("other", "unknown", "")
            and firm_sector in _PHYSICAL_WORKS
            and not any(sig in combined_text for sig in _PHYSICAL_TITLE_SIGNALS)
        ):
            bad = True
            reason = (
                f"Physical works firm (sector '{firm_sector}') in unclassified "
                f"notice with no construction keywords in title/description"
            )

        if bad:
            findings.add(
                "Bidder sector mismatch",
                notice_id,
                notice_title,
                f"{firm_name} — {reason}",
            )


# ── CHECK 2: Overview text null ───────────────────────────────────────────────

def check_overview_text(findings: Findings):
    """Flag watchlist notices where overview_text is null or empty."""
    rows = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency
          FROM raw_notices r
          JOIN scored_notices s ON s.notice_id = r.notice_id
         WHERE (r.overview_text IS NULL OR TRIM(r.overview_text) = '')
           AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
           AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
         ORDER BY r.notice_id
        """,
        (config.PRIORITY_THRESHOLD,),
    )
    for row in rows:
        findings.add(
            "Overview text missing",
            row["notice_id"],
            row["title"] or "",
            f"overview_text is null/empty — enrichment and date extraction will fail",
        )


# ── CHECK 3: Key dates in text but fields null ────────────────────────────────

def check_key_dates(findings: Findings):
    """Flag notices where overview_text references dates but date fields are all null."""
    rows = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency, r.overview_text,
               p.briefing_date, p.questions_deadline, p.registration_deadline
          FROM raw_notices r
          JOIN parsed_notices p    ON p.notice_id = r.notice_id
          JOIN scored_notices s    ON s.notice_id = r.notice_id
         WHERE (r.overview_text IS NOT NULL AND TRIM(r.overview_text) != '')
           AND p.briefing_date IS NULL
           AND p.questions_deadline IS NULL
           AND p.registration_deadline IS NULL
           AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
           AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
         ORDER BY r.notice_id
        """,
        (config.PRIORITY_THRESHOLD,),
    )
    for row in rows:
        ov = row.get("overview_text") or ""
        if _text_has_date_references(ov):
            # Show a snippet of where the date reference is
            m = _DATE_LABEL_PATTERN.search(ov)
            snippet = ov[max(0, m.start()-20): m.start()+80].replace("\n", " ").strip() if m else ""
            findings.add(
                "Key dates in text but fields null",
                row["notice_id"],
                row["title"] or "",
                f"overview_text mentions dates (e.g. «{_truncate(snippet, 60)}») "
                f"but briefing_date/questions_deadline/registration_deadline all null",
            )


# ── CHECK 4: Sector classification mismatch ───────────────────────────────────

def check_sector_classification(findings: Findings):
    """Flag notices where the assigned sector_tag has zero keyword hits in title+description."""
    rows = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency, r.description,
               p.sector_tag
          FROM raw_notices r
          JOIN parsed_notices p  ON p.notice_id = r.notice_id
          JOIN scored_notices s  ON s.notice_id = r.notice_id
         WHERE p.sector_tag IS NOT NULL
           AND p.sector_tag NOT IN ('other', 'unknown')
           AND (r.close_date IS NULL OR r.close_date >= CURRENT_DATE)
           AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
         ORDER BY r.notice_id
        """,
        (config.PRIORITY_THRESHOLD,),
    )
    for row in rows:
        sector = row["sector_tag"]
        if sector not in config.SECTOR_KEYWORDS:
            continue
        text = (row.get("title") or "") + " " + (row.get("description") or "")
        hits = _sector_keywords_hit(sector, text)
        if hits == 0:
            # Also check if a different sector has strong keyword hits
            best_other = max(
                (
                    (s, _sector_keywords_hit(s, text))
                    for s in config.SECTOR_KEYWORDS
                    if s != sector
                ),
                key=lambda x: x[1],
                default=("none", 0),
            )
            other_note = (
                f"; '{best_other[0]}' has {best_other[1]} keyword hit(s)"
                if best_other[1] >= 2
                else ""
            )
            findings.add(
                "Sector classification suspect",
                row["notice_id"],
                row["title"] or "",
                f"Tagged '{sector}' but 0 sector keywords found in title/description{other_note}",
            )


# ── CHECK 5: Stale enrichment ─────────────────────────────────────────────────

def check_stale_enrichment(findings: Findings):
    """Flag notices closing within STALE_ENRICHMENT_DAYS that have not been enriched."""
    cutoff = TODAY + timedelta(days=STALE_ENRICHMENT_DAYS)
    rows = db.fetchall(
        """
        SELECT r.notice_id, r.title, r.agency, r.close_date,
               p.days_until_close, s.composite_score
          FROM raw_notices r
          JOIN parsed_notices p    ON p.notice_id = r.notice_id
          JOIN scored_notices s    ON s.notice_id = r.notice_id
          LEFT JOIN enriched_notices e ON e.notice_id = r.notice_id
         WHERE e.notice_id IS NULL
           AND r.close_date IS NOT NULL
           AND r.close_date >= CURRENT_DATE
           AND r.close_date <= %s
           AND (s.composite_score >= %s OR r.category_raw ILIKE '%%advance%%')
         ORDER BY r.close_date ASC
        """,
        (cutoff, config.PRIORITY_THRESHOLD),
    )
    for row in rows:
        dtc = row.get("days_until_close")
        close = str(row.get("close_date") or "")
        findings.add(
            "Stale enrichment",
            row["notice_id"],
            row["title"] or "",
            f"Closes {close} ({dtc} days) — not yet enriched. Score: {row.get('composite_score', '?')}",
        )


# ── CHECK 6: Pursuit package client name ─────────────────────────────────────

def check_pursuit_client_names(findings: Findings):
    """Flag pursuit packages where client_name is null or an admin placeholder."""
    cutoff = TODAY - timedelta(days=PURSUIT_LOOKBACK_DAYS)
    rows = db.fetchall(
        """
        SELECT id, filename, run_date, client_slug, notice_id, client_name, output_type
          FROM pipeline_outputs
         WHERE output_type IN ('pursuit_package', 'pursuit_package_full')
           AND run_date >= %s
           AND content IS NOT NULL
         ORDER BY run_date DESC
        """,
        (cutoff,),
    )
    for row in rows:
        cname = (row.get("client_name") or "").strip()
        if cname.lower() in _BAD_CLIENT_NAMES:
            findings.add(
                "Pursuit: bad client name",
                row.get("notice_id") or row["filename"],
                row["filename"],
                f"client_name='{cname}' (slug: {row.get('client_slug', '?')}) — "
                f"appears to be admin-generated placeholder",
            )


# ── CHECK 7: Pursuit package incumbent not found ──────────────────────────────

def check_pursuit_incumbent(findings: Findings):
    """Flag pursuit packages where the incumbent assessment indicates no data was found."""
    cutoff = TODAY - timedelta(days=PURSUIT_LOOKBACK_DAYS)
    rows = db.fetchall(
        """
        SELECT id, filename, run_date, client_slug, notice_id, client_name,
               output_type, content
          FROM pipeline_outputs
         WHERE output_type IN ('pursuit_package', 'pursuit_package_full')
           AND run_date >= %s
           AND content IS NOT NULL
         ORDER BY run_date DESC
        """,
        (cutoff,),
    )
    for row in rows:
        content = row.get("content") or ""
        if _incumbent_not_found_in_html(content):
            cname = row.get("client_name") or row.get("client_slug") or "?"
            findings.add(
                "Pursuit: incumbent not identified",
                row.get("notice_id") or row["filename"],
                row["filename"],
                f"Package for '{cname}' — incumbent assessment shows no system/provider identified",
            )


# ── CHECK 8: Pursuit package public vs full labelling ────────────────────────

def check_pursuit_labels(findings: Findings):
    """Flag pursuit packages where output_type is unexpected or missing."""
    cutoff = TODAY - timedelta(days=PURSUIT_LOOKBACK_DAYS)
    rows = db.fetchall(
        """
        SELECT id, filename, run_date, client_slug, notice_id, client_name, output_type
          FROM pipeline_outputs
         WHERE output_type IN ('pursuit_package', 'pursuit_package_full')
           AND run_date >= %s
           AND content IS NOT NULL
         ORDER BY run_date DESC
        """,
        (cutoff,),
    )
    valid_types = {"pursuit_package", "pursuit_package_full"}
    for row in rows:
        ot = row.get("output_type") or ""
        fn = row.get("filename") or ""
        cname = row.get("client_name") or row.get("client_slug") or "?"
        if ot not in valid_types:
            findings.add(
                "Pursuit: unexpected output_type",
                row.get("notice_id") or fn,
                fn,
                f"output_type='{ot}' for '{cname}' — expected pursuit_package or pursuit_package_full",
            )
        # Check filename-type consistency
        is_full_filename = "_full" in fn.lower() or "full_analysis" in fn.lower()
        if ot == "pursuit_package_full" and not is_full_filename:
            findings.add(
                "Pursuit: type/filename mismatch",
                row.get("notice_id") or fn,
                fn,
                f"output_type=pursuit_package_full but filename has no 'full' marker: '{fn}'",
            )
        elif ot == "pursuit_package" and is_full_filename:
            findings.add(
                "Pursuit: type/filename mismatch",
                row.get("notice_id") or fn,
                fn,
                f"output_type=pursuit_package but filename suggests full analysis: '{fn}'",
            )


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(findings: Findings):
    grouped = findings.grouped()
    if not grouped:
        print("\n✓ No issues found.\n")
        return

    print(f"\n{'='*72}")
    print(f"  GROUNDWORK QA AUDIT — {TODAY.isoformat()}")
    print(f"{'='*72}\n")

    for check_name, items in grouped.items():
        print(f"── {check_name.upper()} ({len(items)} issue{'s' if len(items)!=1 else ''}) {'─'*max(1, 60-len(check_name))}")
        for item in items:
            nid = item["notice_id"]
            title = _truncate(item["title"], 55)
            desc = _truncate(item["description"], 90)
            print(f"  [{nid}] {title}")
            print(f"          → {desc}")
        print()

    print(f"{'='*72}")
    print("SUMMARY")
    print(f"{'='*72}")
    total = 0
    for check_name, items in grouped.items():
        n = len(items)
        total += n
        print(f"  {n:>4}  {check_name}")
    print(f"  ────")
    print(f"  {total:>4}  TOTAL issues\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    findings = Findings()

    checks = [
        ("Checking likely bidder sector relevance …",      check_bidder_relevance),
        ("Checking overview_text population …",             check_overview_text),
        ("Checking key dates in text vs. null fields …",   check_key_dates),
        ("Checking sector classification accuracy …",       check_sector_classification),
        ("Checking for stale enrichment (close ≤30d) …",   check_stale_enrichment),
        ("Checking pursuit package client names …",         check_pursuit_client_names),
        ("Checking pursuit package incumbent fields …",     check_pursuit_incumbent),
        ("Checking pursuit package type/label consistency …", check_pursuit_labels),
    ]

    for label, fn in checks:
        print(label, end=" ", flush=True)
        try:
            fn(findings)
            print("done.")
        except Exception as exc:
            print(f"ERROR: {exc}")

    print_report(findings)


if __name__ == "__main__":
    main()
