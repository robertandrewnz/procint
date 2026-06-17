"""
Extended diagnostic for notice 34279032 — answers all four questions in one run.
Run with: railway run python3 diag_34279032.py
"""
import db
from bidder_intelligence import _ach_relevance_gate, _gate_title_keywords

NOTICE_ID = "34279032"
SEP = "=" * 70


# ── Fetch notice details ───────────────────────────────────────────────────────
notice_row = db.fetchone(
    """
    SELECT r.notice_id, r.title, r.agency, r.description, r.category_raw,
           p.sector_tag, p.value_band, p.geographic_scope
      FROM raw_notices r
      JOIN parsed_notices p ON p.notice_id = r.notice_id
     WHERE r.notice_id = %s
    """,
    (NOTICE_ID,),
) or {}

print(SEP)
print(f"NOTICE CONTEXT")
print(SEP)
print(f"  notice_id  : {notice_row.get('notice_id')}")
print(f"  title      : {notice_row.get('title')}")
print(f"  agency     : {notice_row.get('agency')}")
print(f"  sector_tag : {notice_row.get('sector_tag')}")
print(f"  value_band : {notice_row.get('value_band')}")
print(f"  category_raw: {notice_row.get('category_raw')}")
print()


# ── Q1 & Q2: Every bidder_pool row, all columns ───────────────────────────────
print(SEP)
print(f"Q1 + Q2: ALL bidder_pool rows for notice {NOTICE_ID}")
print(SEP)

rows = db.fetchall(
    """
    SELECT match_type, firm_name, relevance_score, strategic_importance,
           intelligence_maturity, size, sector, reasoning,
           company_context, context_confidence
      FROM bidder_pool
     WHERE notice_id = %s
     ORDER BY
        CASE match_type
            WHEN 'ach_analysis'  THEN 0
            WHEN 'mbie_evidence' THEN 1
            WHEN 'web_inferred'  THEN 2
            ELSE 3
        END,
        relevance_score DESC NULLS LAST
    """,
    (NOTICE_ID,),
)

if not rows:
    print("  (NO ROWS in bidder_pool for this notice)")
else:
    for i, r in enumerate(rows, 1):
        print(f"\n  Row {i}:")
        for k, v in r.items():
            print(f"    {k:<25}: {v!r}")

print()


# ── Q3: Web search reconstruction ─────────────────────────────────────────────
web_rows = [r for r in rows if r.get("match_type") == "web_inferred"]

print(SEP)
print("Q3: web_inferred rows and reconstructed web search query")
print(SEP)

if not web_rows:
    print("  No web_inferred rows in bidder_pool for this notice.")
else:
    title      = notice_row.get("title") or ""
    agency     = notice_row.get("agency") or ""
    sector     = notice_row.get("sector_tag") or "other"

    # Show what the CURRENT web search prompt would be (post-fix)
    current_prompt = (
        f"Search for New Zealand companies that provide this specific service:\n"
        f"'{title}'\n\n"
        f"This is for a New Zealand government procurement contract. "
        f"Find commercial providers that deliver this exact type of service — "
        f"anchor your search on what the service IS, not the sector or agency.\n\n"
        f"Search for: '{title} New Zealand providers', "
        f"'{title} companies New Zealand government'.\n\n"
        f"Return up to 5 named commercial providers operating in New Zealand. "
        f"For each, provide the exact trading name and a brief description of their capability.\n\n"
        f"Format each entry as: '[Company Name] — [capability description]'\n\n"
        f"IMPORTANT: Only include commercial firms that deliver THIS specific service — "
        f"NOT government agencies, councils, ministries, crown entities, or public sector "
        f"organisations, and NOT firms from adjacent sectors that don't provide this service. "
        f"If you cannot find credible commercial providers, respond with: "
        f"'No providers identified.'"
    )

    # Show what the OLD web search prompt looked like (pre-fix, sector-anchored)
    old_prompt = (
        f"Search for New Zealand companies that provide the following service:\n"
        f"'{title}'\n\n"
        f"This is for a New Zealand government procurement context. "
        f"Identify commercial providers that would realistically bid on a government contract "
        f"for this type of service.\n\n"
        f"Search for: 'companies providing {title} New Zealand', "
        f"'New Zealand {sector} service providers government'.\n\n"  # ← sector-anchored
        f"Return up to 5 named commercial providers operating in New Zealand."
    )

    print(f"\n  web_inferred firm(s) stored:")
    for r in web_rows:
        print(f"    firm_name       : {r['firm_name']!r}")
        print(f"    company_context : {r['company_context']!r}")
        print(f"    reasoning       : {r['reasoning']!r}")
        print()

    print(f"  OLD web search prompt (sector-anchored, the one that stored these rows):")
    print(f"  --- START ---")
    print(old_prompt)
    print(f"  --- END ---")
    print()
    print(f"  KEY: notice sector_tag = {sector!r}")
    print(f"  The old prompt included: 'New Zealand {sector} service providers government'")
    print(f"  That query would return {sector} firms regardless of the title.")
    print()
    print(f"  CURRENT web search prompt (title-only, post-fix):")
    print(f"  --- START ---")
    print(current_prompt)
    print(f"  --- END ---")

print()


# ── Q4: MBIE evidence reconstruction ──────────────────────────────────────────
mbie_rows = [r for r in rows if r.get("match_type") == "mbie_evidence"]

print(SEP)
print("Q4: mbie_evidence rows and MBIE query reconstruction")
print(SEP)

if not mbie_rows:
    print("  No mbie_evidence rows in bidder_pool for this notice.")
else:
    title      = notice_row.get("title") or ""
    agency     = notice_row.get("agency") or ""
    sector     = notice_row.get("sector_tag") or "other"

    # Reconstruct what _title_keywords() would extract
    try:
        from bidders import _title_keywords
        title_kws = _title_keywords(title)
    except Exception as e:
        title_kws = f"(could not extract: {e})"

    print(f"\n  mbie_evidence firm(s) stored:")
    for r in mbie_rows:
        print(f"    firm_name       : {r['firm_name']!r}")
        print(f"    sector          : {r['sector']!r}")
        print(f"    reasoning       : {r['reasoning']!r}")
        print(f"    context_conf    : {r['context_confidence']!r}")
        print()

    print(f"  MBIE query 1 — get_suppliers_by_category (UNSPSC keyword match):")
    print(f"    unspsc_desc_keywords = {title_kws!r}")
    print(f"    agency_name          = {agency!r}")
    print(f"    SQL ILIKE pattern(s) : {['%' + kw.lower() + '%' for kw in (title_kws if isinstance(title_kws, list) else [])]}")
    print()
    print(f"  MBIE query 2 — get_suppliers_by_sector_and_agency (sector match):")
    print(f"    sector_tag  = {sector!r}")
    print(f"    agency_name = {agency!r}")
    print(f"    SQL filter  : wh.primary_sector = '{sector}' OR sectors_json LIKE '%\"{sector}\"%'")
    print()

    # Run the actual MBIE queries live to show what they return for this notice
    print("  --- LIVE MBIE QUERY 1 RESULTS (category match) ---")
    try:
        from historical_data import get_suppliers_by_category
        cat_results = get_suppliers_by_category(
            unspsc_desc_keywords=title_kws if isinstance(title_kws, list) else [],
            agency_name=agency,
            limit=10,
        )
        if not cat_results:
            print("  (no results)")
        for cr in cat_results:
            print(f"    supplier={cr.get('business_name')!r}  "
                  f"category_wins={cr.get('category_wins')}  "
                  f"matched_categories={cr.get('matched_categories')!r}  "
                  f"primary_sector={cr.get('primary_sector')!r}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print()
    print("  --- LIVE MBIE QUERY 2 RESULTS (sector match) ---")
    try:
        from historical_data import get_suppliers_by_sector_and_agency
        sec_results = get_suppliers_by_sector_and_agency(
            sector_tag=sector,
            agency_name=agency,
            limit=10,
        )
        if not sec_results:
            print("  (no results)")
        for sr in sec_results:
            print(f"    supplier={sr.get('supplier_name')!r}  "
                  f"total_wins={sr.get('total_wins')}  "
                  f"agency_wins={sr.get('agency_wins')}  "
                  f"primary_sector={sr.get('primary_sector')!r}")
    except Exception as e:
        print(f"  ERROR: {e}")

print()


# ── Q5: Gate trace ─────────────────────────────────────────────────────────────
print(SEP)
print("Q5: Gate trace")
print(SEP)

notice_title = notice_row.get("title") or ""
kws = _gate_title_keywords(notice_title)
print(f"\n  Notice title : {notice_title!r}")
print(f"  Gate keywords: {kws}")
print()

ach_rows_for_gate = [r for r in rows if r.get("match_type") == "ach_analysis"]
if not ach_rows_for_gate:
    print("  No ach_analysis rows — gate does not fire.")
else:
    for b in ach_rows_for_gate:
        name_text = (b.get("firm_name") or "").lower()
        reasoning_raw = b.get("reasoning") or ""
        parts = [p.strip() for p in reasoning_raw.split("|") if p.strip()
                 and not p.strip().startswith("CAPMATCH:")]
        all_text = " ".join([name_text, " ".join(parts)]).lower()
        hits = [kw for kw in kws if kw in all_text]
        verdict = "PASS" if hits else "FAIL (no keyword overlap)"
        print(f"  {b['firm_name']!r}")
        print(f"    keyword hits : {hits}")
        print(f"    gate verdict : {verdict}")
        print()

    gate_result = _ach_relevance_gate(ach_rows_for_gate, notice_title)
    print(f"  _ach_relevance_gate overall result: {gate_result}  "
          f"(True=rows used, False=rows blocked)")

print()
print(SEP)
print("END OF DIAGNOSTIC")
print(SEP)
