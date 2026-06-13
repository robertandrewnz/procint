"""
Audit supplier_win_history for obvious firm sector misclassifications.

Finds known IT/advisory/health firms that supplier_win_history has classified
as infrastructure/construction, and vice versa.  Use this to discover firms
that should be added to FIRM_SECTOR_OVERRIDES in bidders.py.

Run:
    railway run python3 _audit_firm_sectors.py
"""

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import db

# Known IT companies whose MBIE primary_sector should be ICT
_KNOWN_ICT_FIRMS = {
    "fusion5", "empired", "revolent", "datacom", "spark nz", "gen-i",
    "unisys", "hewlett packard", "hp", "microsoft", "ibm nz", "ibm",
    "cisco", "oracle", "sap", "accenture", "wipro", "infosys",
    "theta", "provoke", "solnet", "jade software", "intergen",
    "dimension data", "ntt", "computacenter", "logicalis",
    "axon networks", "psi", "tait communications",
    "dxc", "dxc technology", "fujitsu", "tata",
    "assurity", "beca ict", "pricewaterhousecoopers ict",
    "kpmg ict", "deloitte digital",
}

# Known construction/civil firms whose sector should NOT be ICT/advisory
_KNOWN_PHYSICAL_FIRMS = {
    "fulton hogan", "downer", "heb construction", "higgins",
    "mcconnell dowell", "fletcher construction", "cpb contractors",
    "laing o'rourke", "naylor love", "arrow international",
    "hawkins", "leighs construction", "citycare", "mwh",
    "jacobs", "beca infrastructure", "stantec", "aecom",
}

print("\n" + "=" * 70)
print("Checking known IT firms classified as non-ICT in supplier_win_history")
print("=" * 70)

ict_rows = db.fetchall(
    """
    SELECT supplier_name, primary_sector, total_wins, sectors_json
      FROM supplier_win_history
     WHERE primary_sector NOT IN ('ICT', 'cybersecurity', 'advisory', 'other')
       AND total_wins >= 1
     ORDER BY total_wins DESC
    """
)

misclassified_ict = []
for r in ict_rows:
    name_lower = (r["supplier_name"] or "").lower()
    for known in _KNOWN_ICT_FIRMS:
        if known in name_lower:
            misclassified_ict.append(r)
            break

print(f"\nFound {len(misclassified_ict)} known IT firms with non-ICT primary_sector:\n")
for r in misclassified_ict:
    print(f"  {r['supplier_name']:<40} primary_sector={r['primary_sector']:<15} wins={r['total_wins']}")

print("\n" + "=" * 70)
print("Checking known construction firms classified as ICT/advisory")
print("=" * 70)

construction_rows = db.fetchall(
    """
    SELECT supplier_name, primary_sector, total_wins
      FROM supplier_win_history
     WHERE primary_sector IN ('ICT', 'cybersecurity', 'advisory', 'health')
       AND total_wins >= 2
     ORDER BY total_wins DESC
     LIMIT 100
    """
)

misclassified_physical = []
for r in construction_rows:
    name_lower = (r["supplier_name"] or "").lower()
    for known in _KNOWN_PHYSICAL_FIRMS:
        if known in name_lower:
            misclassified_physical.append(r)
            break

print(f"\nFound {len(misclassified_physical)} known physical-works firms with ICT/advisory primary_sector:\n")
for r in misclassified_physical:
    print(f"  {r['supplier_name']:<40} primary_sector={r['primary_sector']:<15} wins={r['total_wins']}")

print("\n" + "=" * 70)
print("Top 30 firms per sector in supplier_win_history (spot-check)")
print("=" * 70)

for sector in ("ICT", "infrastructure", "advisory", "FM", "health"):
    top = db.fetchall(
        """
        SELECT supplier_name, total_wins
          FROM supplier_win_history
         WHERE primary_sector = %s
         ORDER BY total_wins DESC
         LIMIT 10
        """,
        (sector,),
    )
    print(f"\n  {sector} (top 10):")
    for r in top:
        print(f"    {r['supplier_name']:<45} {r['total_wins']} wins")

print("\n" + "=" * 70)
print("RECOMMENDATIONS")
print("=" * 70)
print("\nFor any firm listed above that should be in a different sector,")
print("add it to FIRM_SECTOR_OVERRIDES in bidders.py.")
print("Example entry (in bidders.py):")
print('    "firm canonical name lowercase": "ICT",')
print("\nThen re-run: railway run python3 _fix_bidder_mismatches.py --fix")
