"""
canonical_suppliers.py — supplier name normalisation and deduplication.

Collapses MBIE supplier name variants to a canonical company name:
  "Fulton Hogan Canterbury", "FULTON HOGAN LIMITED", "Fulton Hogan Ltd"
  → all map to "Fulton Hogan"

Used by bidders.py to:
  1. Deduplicate MBIE results so variants of the same parent don't fill
     all top-3 bidder slots.
  2. Match CSV bidders.csv entries against MBIE win history rows that use
     different name spellings.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Suffix / noise patterns to strip (applied iteratively until stable) ───────

_STRIP_PATTERNS: list[re.Pattern] = [p for p in (re.compile(pat, re.IGNORECASE) for pat in [
    # Legal suffixes
    r"\bLimited\b", r"\bLtd\.?\b", r"\bL\.L\.C\.?\b", r"\bLLC\b",
    r"\bInc\.?\b", r"\bIncorporated\b", r"\bCorp\.?\b", r"\bCorporation\b",
    r"\bPty\.?\b",
    # Ownership/structure
    r"\bGroup\b", r"\bHoldings?\b", r"\bPartnership\b",
    # Geography — country
    r"\bNew\s+Zealand\b", r"\bAustralia\b", r"\bAustralasia\b",
    r"\b(?<!One\s)NZ\b",          # "NZ" but not "One NZ"
    # Geography — NZ regions (MBIE often appends these)
    r"\bCanterbury\b", r"\bAuckland\b", r"\bWellington\b", r"\bOtago\b",
    r"\bSouthland\b", r"\bManawatu\b", r"\bWaikato\b", r"\bBay\s+of\s+Plenty\b",
    r"\bHawke'?s?\s+Bay\b", r"\bTaranaki\b", r"\bNorthland\b", r"\bNelson\b",
    r"\bMarlborough\b", r"\bWestland\b", r"\bGisborne\b",
    r"\bNorth\s+Island\b", r"\bSouth\s+Island\b",
    # MBIE data artefacts
    r"\s*-?\s*All\s+Offices\b.*$",
    r"\s*-?\s*Main\s+Account\s+User\b.*$",
    r"\s*\([^)]*\)\s*$",           # trailing parenthetical e.g. "(NZ)"
    # Trailing punctuation/noise
    r"[,\.]+\s*$",
])]


def _strip_noise(name: str) -> str:
    """Strip legal/regional suffixes iteratively until stable."""
    prev: Optional[str] = None
    while prev != name:
        prev = name
        for pat in _STRIP_PATTERNS:
            name = pat.sub("", name).strip()
        name = re.sub(r"\s{2,}", " ", name).strip()
    return name


def normalise(name: str) -> str:
    """Return lowercase, suffix-stripped, whitespace-normalised form for dict lookup."""
    return _strip_noise(name).lower()


# ── Explicit canonical map ─────────────────────────────────────────────────────
# Maps normalised (stripped, lowercase) name → canonical display name.
# Add new entries here as new variants are discovered in MBIE data.

CANONICAL_MAP: dict[str, str] = {
    # ── Civil / roading ───────────────────────────────────────────────────────
    "fulton hogan":            "Fulton Hogan",
    "downer":                  "Downer NZ",
    "downer nz":               "Downer NZ",
    "heb construction":        "HEB Construction",
    "higgins contractors":     "Higgins Contractors",
    "higgins":                 "Higgins Contractors",
    "ventia":                  "Ventia NZ",
    "broadspectrum":           "Broadspectrum NZ",
    "mcconnell dowell":        "McConnell Dowell",
    "dempsey wood civil":      "Dempsey Wood Civil",
    "dempsey wood":            "Dempsey Wood Civil",
    "brian perry civil":       "Brian Perry Civil",
    "city care":               "City Care",
    "southroads":              "SouthRoads",
    "south roads":             "SouthRoads",
    "traffica roading services":"Traffica",
    "traffic systems":         "Traffic Systems Ltd",
    "tsl":                     "Traffic Systems Ltd",
    "arco":                    "ARCO Group",
    "arco group":              "ARCO Group",
    # ── FM ───────────────────────────────────────────────────────────────────
    "jll":                     "JLL NZ",
    "jones lang lasalle":      "JLL NZ",
    "bgis":                    "BGIS",
    "iss facility services":   "ISS Facility Services",
    "iss":                     "ISS Facility Services",
    "sodexo":                  "Sodexo NZ",
    "compass group":           "Compass Group NZ",
    "cushman & wakefield":     "Cushman & Wakefield",
    "cushman and wakefield":   "Cushman & Wakefield",
    "programmed":              "Programmed NZ",
    "programmed property services": "Programmed NZ",
    "spotless":                "Spotless NZ",
    "johnson controls":        "Johnson Controls NZ",
    # ── Construction ─────────────────────────────────────────────────────────
    "fletcher construction":   "Fletcher Construction",
    "naylor love":             "Naylor Love",
    "naylor love construction": "Naylor Love",
    "hawkins":                 "Hawkins Construction",
    "hawkins construction":    "Hawkins Construction",
    "lt mcguinness":           "LT McGuinness",
    "l t mcguinness":          "LT McGuinness",
    "dominion constructors":   "Dominion Constructors",
    "leighs construction":     "Leighs Construction",
    "arrow international":     "Arrow International",
    "ebert construction":      "Ebert Construction",
    "brosnan":                 "Brosnan Construction",
    "canam":                   "Canam NZ",
    "watts & hughes":          "Watts & Hughes Construction",
    "watts and hughes":        "Watts & Hughes Construction",
    "mitchell construction":   "Mitchell Construction",
    # ── ICT ──────────────────────────────────────────────────────────────────
    "datacom":                 "Datacom",
    "spark":                   "Spark NZ",
    "one nz":                  "One NZ",
    "vodafone":                "One NZ",
    "2degrees":                "2degrees",
    "kordia":                  "Kordia",
    "fujitsu":                 "Fujitsu NZ",
    "unisys":                  "Unisys NZ",
    "dxc technology":          "DXC Technology",
    "dxc":                     "DXC Technology",
    "theta":                   "Theta",
    "intergen":                "Theta",
    "fronde":                  "Fronde Systems Group",
    "fronde systems":          "Fronde Systems Group",
    "gen-i":                   "Spark NZ",
    "provoke":                 "Provoke Solutions",
    "assurity":                "Assurity Consulting",
    "solnet":                  "Solnet Solutions",
    # ── Cybersecurity ────────────────────────────────────────────────────────
    "bastion":                 "Bastion Security Group",
    "bastion security":        "Bastion Security Group",
    "cybercx":                 "CyberCX NZ",
    "aura information security": "Aura Information Security",
    "aura":                    "Aura Information Security",
    "kordia security":         "Kordia Security",
    # ── Engineering / consulting ──────────────────────────────────────────────
    "beca":                    "Beca",
    "wsp":                     "WSP NZ",
    "wsp opus":                "WSP NZ",
    "opus":                    "WSP NZ",
    "opus international":      "WSP NZ",
    "opus international consultants": "WSP NZ",
    "aecom":                   "AECOM NZ",
    "ghd":                     "GHD NZ",
    "stantec":                 "Stantec NZ",
    "jacobs":                  "Jacobs NZ",
    "aurecon":                 "Aurecon",
    "tonkin & taylor":         "Tonkin + Taylor",
    "tonkin + taylor":         "Tonkin + Taylor",
    "tonkin and taylor":       "Tonkin + Taylor",
    "tonkin taylor":           "Tonkin + Taylor",
    "pattle delamore partners": "Pattle Delamore Partners",
    "pdp":                     "Pattle Delamore Partners",
    "morphum environmental":   "Morphum Environmental",
    "morphum":                 "Morphum Environmental",
    "mott macdonald":          "Mott MacDonald NZ",
    "harrison grierson":       "Harrison Grierson",
    # ── Advisory / professional services ─────────────────────────────────────
    "deloitte":                "Deloitte",
    "pwc":                     "PwC",
    "pricewaterhousecoopers":  "PwC",
    "kpmg":                    "KPMG",
    "ey":                      "EY",
    "ernst & young":           "EY",
    "ernst and young":         "EY",
    "accenture":               "Accenture NZ",
    "martinjenkins":           "MartinJenkins",
    "martin jenkins":          "MartinJenkins",
    "martin jenkins and associates": "MartinJenkins",
    "sapere":                  "Sapere Research Group",
    "sapere research":         "Sapere Research Group",
    "sapere research group":   "Sapere Research Group",
    "nzier":                   "NZIER",
    "nous":                    "Nous Group",
    "nous group":              "Nous Group",
    "bdo":                     "BDO NZ",
    "grant thornton":          "Grant Thornton NZ",
    # ── Legal ────────────────────────────────────────────────────────────────
    "chapman tripp":           "Chapman Tripp",
    "minterellisonruddwatts":  "MinterEllisonRuddWatts",
    "minter ellison rudd watts": "MinterEllisonRuddWatts",
    "bell gully":              "Bell Gully",
    "russell mcveagh":         "Russell McVeagh",
    "buddle findlay":          "Buddle Findlay",
    "simpson grierson":        "Simpson Grierson",
    "dentons kensington swan": "Dentons Kensington Swan",
    "kensington swan":         "Dentons Kensington Swan",
    "anderson lloyd":          "Anderson Lloyd",
    "hesketh henry":           "Hesketh Henry",
    "wynn williams":           "Wynn Williams",
    # ── Aerospace / Defence ──────────────────────────────────────────────────
    "babcock":                 "Babcock NZ",
    "babcock new zealand":     "Babcock NZ",
    "air new zealand engineering": "Air NZ Engineering Services",
    "anes":                    "Air NZ Engineering Services",
    "haeco":                   "HAECO",
    "standardaero":            "StandardAero NZ",
    # ── Security ─────────────────────────────────────────────────────────────
    "g4s":                     "G4S NZ",
    "armourguard":             "Armourguard Security",
    "securitas":               "Securitas NZ",
    "wilson security":         "Wilson Security NZ",
    "first security":          "FIRST Security",
    # ── Health tech ──────────────────────────────────────────────────────────
    "orion health":            "Orion Health",
    "civica":                  "Civica NZ",
    # ── Research / science ───────────────────────────────────────────────────
    "niwa":                    "NIWA",
    "national institute of water & atmospheric research": "NIWA",
    "national institute of water and atmospheric research": "NIWA",
    "esr":                     "ESR",
    "callaghan innovation":    "Callaghan Innovation",
    "gns science":             "GNS Science",
    "agresearch":              "AgResearch",
    "plant & food research":   "Plant & Food Research",
    "landcare research":       "Landcare Research",
    "manaaki whenua":          "Landcare Research",
    # ── Utilities ────────────────────────────────────────────────────────────
    "vector":                  "Vector",
    "transpower":              "Transpower NZ",
    "chorus":                  "Chorus NZ",
    "enable networks":         "Enable Networks",
    "northpower":              "Northpower",
}


def canonical_name(name: str) -> str:
    """
    Return the canonical company name for a supplier name variant.
    Falls back to title-case stripped form if no explicit mapping exists.
    """
    if not name:
        return name
    key = normalise(name)
    if key in CANONICAL_MAP:
        return CANONICAL_MAP[key]
    # Try after stripping once (some keys are already stripped)
    stripped = _strip_noise(name)
    stripped_key = stripped.lower()
    if stripped_key in CANONICAL_MAP:
        return CANONICAL_MAP[stripped_key]
    # Best-effort: stripped + title-case
    return stripped.title() if stripped else name.strip()


def deduplicate_bidders(bidders: list[dict]) -> list[dict]:
    """
    Deduplicate a ranked bidder list by canonical company name.

    When multiple variants of the same parent appear (e.g. "Fulton Hogan"
    and "Fulton Hogan Canterbury"), keep the entry with the higher
    relevance_score — or the first occurrence if scores are equal.
    Preserves the relative ranking of kept entries.
    """
    seen: dict[str, dict] = {}   # canonical_name → best entry so far
    result: list[dict] = []

    for b in bidders:
        name = b.get("firm_name") or ""
        # Prefer a pre-resolved canonical_name already stored in the dict
        # (set by load_bidders from the CSV canonical_name column).
        canon = b.get("canonical_name") or canonical_name(name)
        b = dict(b)  # don't mutate caller's dict
        b["canonical_name"] = canon

        if canon not in seen:
            seen[canon] = b
            result.append(b)
        else:
            # Replace existing entry if this one has a higher score
            new_score = float(b.get("relevance_score") or 0)
            old_score = float(seen[canon].get("relevance_score") or 0)
            if new_score > old_score:
                idx = next(i for i, x in enumerate(result)
                           if x.get("canonical_name") == canon)
                result[idx] = b
                seen[canon] = b

    return result
