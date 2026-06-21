"""
Direct incumbent insert — no web search, no diagnostic output.
Usage: railway run python3 _store_incumbent.py <notice_id> <firm_name>
Example: railway run python3 _store_incumbent.py 34279032 SHL
"""
import sys
import db

if len(sys.argv) < 3:
    print("Usage: python3 _store_incumbent.py <notice_id> <firm_name>")
    sys.exit(1)

notice_id = sys.argv[1]
firm_name = " ".join(sys.argv[2:])

db.execute(
    """
    INSERT INTO bidder_pool
        (notice_id, firm_name, match_type, relevance_score,
         strategic_importance, intelligence_maturity, reasoning, sector)
    VALUES (%s, %s, 'incumbent_identified', 0.95, 'high', 'strong',
            'Manually stored via _store_incumbent.py', '')
    ON CONFLICT (notice_id, firm_name) DO UPDATE SET
        match_type            = 'incumbent_identified',
        relevance_score       = 0.95,
        strategic_importance  = 'high',
        intelligence_maturity = 'strong',
        reasoning             = EXCLUDED.reasoning
    """,
    (notice_id, firm_name),
)

row = db.fetchone(
    "SELECT firm_name, match_type, relevance_score FROM bidder_pool "
    "WHERE notice_id = %s AND firm_name = %s LIMIT 1",
    (notice_id, firm_name),
)
if row:
    print(f"OK: {row['firm_name']} stored for notice {notice_id} "
          f"(match_type={row['match_type']} score={row['relevance_score']})")
else:
    print(f"FAILED: row not found after INSERT for notice {notice_id} firm {firm_name!r}")
