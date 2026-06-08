"""
win_position.py — Multi-factor competitive position assessment.

Replaces the single win_probability_pct percentage with a scored band
backed by explicit factor reasoning.  Called from pursuit_package.py
and demo_package.py before rendering HTML.

Bands:
  Strong position  (sum ≥  2) — teal
  Competitive      (sum 0–1)  — amber
  Challenging      (sum −1 to −3) — orange
  Not recommended  (sum ≤ −4) — red

Each of 8 factors is scored independently from live DB data and returns
a numeric contribution.  The top-3 influencing factors (by absolute
contribution) are surfaced as the "reasoning summary".
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import db

logger = logging.getLogger(__name__)


# ── Band definitions ──────────────────────────────────────────────────────────

BANDS = [
    ("Strong position",   2,    None,  "#2a9d8f", "strong"),
    ("Competitive",       0,    1,     "#d4a017", "competitive"),
    ("Challenging",      -3,   -1,     "#e07b39", "challenging"),
    ("Not recommended",  None, -4,     "#e05555", "not_recommended"),
]


def _score_to_band(score: int) -> dict:
    for label, lo, hi, colour, css_key in BANDS:
        if (lo is None or score >= lo) and (hi is None or score <= hi):
            return {"label": label, "colour": colour, "css_key": css_key}
    # fallback
    return {"label": "Challenging", "colour": "#e07b39", "css_key": "challenging"}


# ── Factor scorers ────────────────────────────────────────────────────────────

def _f1_incumbent_strength(agency: str, sector: str) -> tuple[int, str]:
    """
    How many times has the same/related supplier won with this specific agency
    in this category.
    Score: 0 (no incumbent) → −3 (5+ wins with this agency).
    """
    agency_word = agency.split()[0] if agency else ""
    row = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS wins
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
           AND c.sector_tag = %s
           AND s.business_name NOT IN ('', 'NULL')
        """,
        (f"%{agency_word}%", sector),
    )
    wins = int((row or {}).get("wins") or 0)
    if wins == 0:
        return (0, "No incumbent detected for this agency/sector")
    if wins >= 5:
        return (-3, f"Strong incumbent: {wins} wins with this buyer in {sector}")
    if wins >= 3:
        return (-2, f"Established incumbent: {wins} wins with this buyer in {sector}")
    return (-1, f"Possible incumbent: {wins} prior win(s) with this buyer in {sector}")


def _f2_agency_loyalty(agency: str) -> tuple[int, str]:
    """
    Percentage of this agency's contracts that go to repeat suppliers.
    Score: 0 (<40% repeat) → −2 (>70% repeat).
    """
    agency_word = agency.split()[0] if agency else ""
    row = db.fetchone(
        """
        SELECT
            COUNT(DISTINCT n.rfx_id) AS total,
            COUNT(DISTINCT n.rfx_id) FILTER (
                WHERE s.business_name IN (
                    SELECT s2.business_name
                      FROM mbie_award_notices n2
                      JOIN mbie_award_suppliers s2 ON s2.rfx_id = n2.rfx_id
                     WHERE n2.is_awarded
                       AND LOWER(n2.posting_agency) LIKE LOWER(%s)
                     GROUP BY s2.business_name
                    HAVING COUNT(*) > 1
                )
            ) AS repeat_wins
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
        """,
        (f"%{agency_word}%", f"%{agency_word}%"),
    )
    total = int((row or {}).get("total") or 0)
    repeat = int((row or {}).get("repeat_wins") or 0)
    if total == 0:
        return (0, "No contract award history recorded for this agency")
    pct = round(repeat / total * 100)
    if pct > 70:
        return (-2, f"Highly loyal agency — {pct}% of contracts go to repeat suppliers")
    if pct > 40:
        return (-1, f"Moderately loyal agency — {pct}% repeat supplier rate")
    return (0, f"Open procurement agency — {pct}% repeat supplier rate")


def _f3_market_concentration(sector: str) -> tuple[int, str]:
    """
    How many distinct suppliers win in this sector nationally.
    Score: +1 (10+ distinct suppliers) → −2 (≤3 dominant).
    """
    three_years_ago = (date.today() - timedelta(days=3 * 365)).isoformat()
    row = db.fetchone(
        """
        SELECT COUNT(DISTINCT s.business_name) AS suppliers
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND c.sector_tag = %s
           AND n.awarded_date >= %s
        """,
        (sector, three_years_ago),
    )
    suppliers = int((row or {}).get("suppliers") or 0)
    if suppliers == 0:
        return (0, "Insufficient market data for concentration analysis")
    if suppliers >= 10:
        return (+1, f"Fragmented market — {suppliers} distinct suppliers nationally (3yr)")
    if suppliers <= 3:
        return (-2, f"Concentrated market — only {suppliers} distinct suppliers nationally (3yr)")
    return (0, f"Moderately concentrated market — {suppliers} suppliers nationally (3yr)")


def _f4_client_history(client_name: str, sector: str, agency: str) -> tuple[int, str]:
    """
    Prior wins by the client in this sector and with this specific agency.
    Score: +2 (wins with this agency) → 0 (no relevant history).
    """
    agency_word = agency.split()[0] if agency else ""
    client_word = client_name.split()[0] if client_name else ""

    agency_wins = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS wins
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(s.business_name) LIKE LOWER(%s)
           AND LOWER(n.posting_agency) LIKE LOWER(%s)
        """,
        (f"%{client_word}%", f"%{agency_word}%"),
    )
    a_wins = int((agency_wins or {}).get("wins") or 0)
    if a_wins > 0:
        return (+2, f"Client has {a_wins} prior win(s) with this buyer")

    sector_wins = db.fetchone(
        """
        SELECT COUNT(DISTINCT n.rfx_id) AS wins
          FROM mbie_award_notices n
          JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
          JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
         WHERE n.is_awarded
           AND LOWER(s.business_name) LIKE LOWER(%s)
           AND c.sector_tag = %s
        """,
        (f"%{client_word}%", sector),
    )
    s_wins = int((sector_wins or {}).get("wins") or 0)
    if s_wins > 0:
        return (+1, f"Client has {s_wins} sector win(s) nationally (different agencies)")
    return (0, "No relevant contract award history found for client in this sector/agency")


def _f5_timing(days_until_close: Optional[int]) -> tuple[int, str]:
    """
    Days to close relative to contract complexity.
    Score: +1 (>21d) → −2 (<7d).
    """
    if days_until_close is None:
        return (0, "Closing date unknown — timing factor neutral")
    if days_until_close > 21:
        return (+1, f"{days_until_close} days to close — adequate preparation time")
    if days_until_close >= 8:
        return (0, f"{days_until_close} days to close — tight but workable")
    return (-2, f"Only {days_until_close} days to close — insufficient for new entrant")


def _f6_complexity(value_band: str, evaluation_criteria: Optional[str]) -> tuple[int, str]:
    """
    Contract complexity from value band + evaluation criteria.
    Score: +1 (simple, value-weighted) → −1 (complex, relationship-weighted).
    """
    crit = (evaluation_criteria or "").lower()
    relationship_signals = [
        "experience", "track record", "references", "relationship",
        "incumbent", "local knowledge", "cultural fit",
    ]
    complexity_signals = [
        "technical", "multi-criteria", "weighted", "methodology",
        "rfp", "bafo", "best and final", "two-stage",
    ]
    rel_hits = sum(1 for s in relationship_signals if s in crit)
    cplx_hits = sum(1 for s in complexity_signals if s in crit)

    high_value = value_band in ("2m_10m", "10m_plus")

    if cplx_hits >= 2 or rel_hits >= 2 or high_value:
        return (-1, "Complex/relationship-weighted evaluation criteria detected")
    if cplx_hits == 0 and rel_hits == 0:
        return (+1, "Simple evaluation criteria — likely value-weighted")
    return (0, "Balanced evaluation criteria")


def _f7_subcontractor_jv(client_name: str, sector: str) -> tuple[int, str]:
    """
    Do any known sub/JV partners of the client appear as winners?
    Simple heuristic: check bidder_pool for client + any shared sector wins.
    Score: +1 if a sub/JV partner has relevant wins, else 0.
    """
    client_word = client_name.split()[0] if client_name else ""
    # Look for co-bidders in the same notices as the client in bidder_pool
    rows = db.fetchall(
        """
        SELECT DISTINCT bp2.firm_name
          FROM bidder_pool bp1
          JOIN bidder_pool bp2 ON bp2.notice_id = bp1.notice_id
                              AND bp2.firm_name <> bp1.firm_name
          JOIN parsed_notices p ON p.notice_id = bp1.notice_id
         WHERE LOWER(bp1.firm_name) LIKE LOWER(%s)
           AND p.sector_tag = %s
         LIMIT 10
        """,
        (f"%{client_word}%", sector),
    )
    if not rows:
        return (0, "No subcontractor/JV partner data available")

    partner_names = [r["firm_name"] for r in rows]
    # Check if any of those names have wins in MBIE
    for pname in partner_names[:5]:
        pword = pname.split()[0]
        row = db.fetchone(
            """
            SELECT COUNT(*) AS wins
              FROM mbie_award_notices n
              JOIN mbie_award_suppliers s ON s.rfx_id = n.rfx_id
              JOIN mbie_award_categories c ON c.rfx_id = n.rfx_id
             WHERE n.is_awarded
               AND LOWER(s.business_name) LIKE LOWER(%s)
               AND c.sector_tag = %s
            """,
            (f"%{pword}%", sector),
        )
        if row and int(row.get("wins") or 0) > 0:
            return (+1, f"Known co-bidder '{pname}' has sector wins — JV pathway viable")
    return (0, "No co-bidder partners identified with relevant sector wins")


def _f8_strategic_alignment(sector: str, agency: str) -> tuple[int, str]:
    """
    Does Budget 2026 / a sector strategy signal active growth investment?
    Score: +1 if positive intel signal, else 0.
    """
    try:
        from intel_library.scoring_integration import get_strategic_score_boost
        notice_proxy = {"notice_id": "", "sector_tag": sector, "agency": agency}
        boost = get_strategic_score_boost(notice_proxy, record_usage=False)
        if boost.get("modifier", 0.0) > 0:
            sources = ", ".join(boost.get("source_names", [])[:2])
            return (+1, f"Active investment signal from {sources or 'intel library'}")
    except Exception:
        pass
    return (0, "No positive strategic investment signal detected")


# ── Main function ─────────────────────────────────────────────────────────────

def calculate_win_position(notice: dict, client_profile: dict) -> dict:
    """
    Score all 8 factors and return a win position assessment.

    Args:
        notice:         Dict with at minimum: sector_tag, agency, days_until_close,
                        value_band, evaluation_criteria.
        client_profile: Dict with at minimum: name (client firm name).

    Returns:
        {
          "band":     str  — e.g. "Competitive"
          "colour":   str  — hex colour for the pill
          "css_key":  str  — CSS class key
          "score":    int  — raw numeric sum
          "factors":  list of {"label": str, "score": int, "reason": str}
          "top3":     list of the 3 highest-|score| factors (the headline drivers)
          "summary":  str  — e.g. "Challenging — Agency has 78% repeat rate, ..."
        }
    """
    sector  = (notice.get("sector_tag") or "other").lower()
    agency  = notice.get("agency") or ""
    dtc     = notice.get("days_until_close")
    vband   = notice.get("value_band") or "unknown"
    crit    = notice.get("evaluation_criteria") or ""
    client  = client_profile.get("name") or ""

    factor_fns = [
        ("Incumbent strength",     lambda: _f1_incumbent_strength(agency, sector)),
        ("Agency loyalty",         lambda: _f2_agency_loyalty(agency)),
        ("Market concentration",   lambda: _f3_market_concentration(sector)),
        ("Client history",         lambda: _f4_client_history(client, sector, agency)),
        ("Timing",                 lambda: _f5_timing(dtc)),
        ("Contract complexity",    lambda: _f6_complexity(vband, crit)),
        ("Subcontractor/JV",       lambda: _f7_subcontractor_jv(client, sector)),
        ("Strategic alignment",    lambda: _f8_strategic_alignment(sector, agency)),
    ]

    factors = []
    total_score = 0
    for label, fn in factor_fns:
        try:
            score, reason = fn()
        except Exception as exc:
            logger.warning("Win position factor '%s' failed: %s", label, exc)
            score, reason = 0, "Data unavailable"
        factors.append({"label": label, "score": score, "reason": reason})
        total_score += score

    # Top 3 by absolute contribution (most influential factors)
    top3 = sorted(factors, key=lambda f: abs(f["score"]), reverse=True)[:3]

    band = _score_to_band(total_score)
    summary_reasons = "; ".join(f["reason"] for f in top3 if f["score"] != 0)
    summary = f"{band['label']} — {summary_reasons}" if summary_reasons else band["label"]

    return {
        "band":    band["label"],
        "colour":  band["colour"],
        "css_key": band["css_key"],
        "score":   total_score,
        "factors": factors,
        "top3":    top3,
        "summary": summary,
    }
