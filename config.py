"""
All tunable pipeline parameters. Adjust here without touching module code.
"""
import os
from pathlib import Path
from typing import Optional
from dotenv import dotenv_values

# Load .env relative to this file and inject into os.environ explicitly.
# Using dotenv_values() + manual injection avoids a python-dotenv 1.x bug
# where load_dotenv() exports empty strings for quoted values containing '--'.
_env_path = Path(__file__).parent / ".env"
for _k, _v in dotenv_values(_env_path).items():
    if _v is not None and _v != "":
        os.environ[_k] = _v

# ── Database ─────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.environ["DATABASE_URL"]

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL: str = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS: int = 1024

# ── GETS scraping ─────────────────────────────────────────────────────────────
GETS_BASE_URL: str = "https://www.gets.govt.nz"
GETS_SEARCH_URL: str = "https://www.gets.govt.nz/ExternalIndex.htm"
REQUEST_TIMEOUT: int = 30          # seconds for lightweight requests attempt
PLAYWRIGHT_TIMEOUT: int = 60_000   # ms

# ── Sector taxonomy ───────────────────────────────────────────────────────────
SECTORS = [
    "infrastructure",
    "construction",
    "FM",
    "cybersecurity",
    "ICT",
    "defence",
    "health",
    "advisory",
    "security",
    "utilities",
    "professional_services",
    "other",
]

# ── Classification confidence thresholds ──────────────────────────────────────
# Minimum keyword match count for HIGH confidence per sector.
# Sectors with unique/specific keywords (cybersecurity, defence) need only 1 match.
# Sectors with generic vocabulary need 2+ to reduce false positives.
SECTOR_HIGH_THRESHOLD: dict[str, int] = {
    "infrastructure":     2,
    "construction":       2,
    "FM":                 2,
    "cybersecurity":      1,   # highly specific terms — 1 match is definitive
    "ICT":                2,
    "defence":            1,   # NZDF/RNZAF/military are unambiguous
    "health":             2,
    "advisory":           2,
    "security":           2,
    "utilities":          2,
    "professional_services": 2,
}

# Keywords used to map notice text/category to a sector tag
SECTOR_KEYWORDS: dict[str, list[str]] = {
    # ── Infrastructure / Roading ──────────────────────────────────────────────
    # Physical civil and transport infrastructure ONLY.
    # Deliberately excludes "construction" and "contractor" (→ construction sector)
    # and "maintenance" alone (→ FM if combined with building/property terms).
    "infrastructure": [
        "roading", "road network", "road maintenance", "road rehabilitation",
        "pavement", "sealed road", "unsealed road", "chipsealing", "resealing",
        "carriageway", "highway", "state highway", "transport infrastructure",
        "bridge", "culvert", "kerb", "drainage network", "earthworks",
        "wastewater", "stormwater", "water supply", "pipeline", "three waters",
        "waka kotahi", "NZTA", "horizontal infrastructure",
    ],
    # ── Construction ─────────────────────────────────────────────────────────
    # Building and structural works. Overlaps deliberately avoided with
    # infrastructure (no "road", "pavement") and FM (no "maintenance" alone).
    "construction": [
        "building contractor", "main contractor", "construction services",
        "refurbishment", "fitout", "fit-out", "fit out",
        "school property", "school building", "school construction",
        "roof replacement", "roofing", "demolition",
        "weathertightening", "weatherproofing", "remediation works",
        "new build", "building works", "structural upgrade",
        "consent works", "construction contract",
        "retaining wall", "civil works", "earthworks", "site works",
        "deck replacement", "structural works", "building upgrade", "building renovation",
    ],
    # ── Facilities Management ─────────────────────────────────────────────────
    # Building operations and soft services. Does NOT include road/transport
    # maintenance — those belong to infrastructure.
    "FM": [
        "facilities management", "facility management",
        "building maintenance", "property services", "property management",
        "grounds maintenance", "grounds keeping",
        "cleaning services", "cleaning contract", "commercial cleaning",
        "caretaking", "janitorial",
        "HVAC maintenance", "HVAC services",
        "building services contract", "FM services",
        "estate management", "asset management services",
    ],
    # ── Cybersecurity ─────────────────────────────────────────────────────────
    # Highly specific terms — 1 match is sufficient (threshold = 1).
    "cybersecurity": [
        "cyber security", "cybersecurity", "cyber resilience",
        "security operations centre", "SOC", "SIEM",
        "penetration testing", "pen testing",
        "information security", "infosec",
        "CISO", "zero trust", "IRAP", "NZISM",
        "vulnerability assessment", "threat detection",
        "incident response", "security audit",
    ],
    # ── ICT ───────────────────────────────────────────────────────────────────
    # Digital and technology. "network" removed (too generic — hits road network,
    # water network, bus network). "cyber" moved to cybersecurity.
    "ICT": [
        "information technology", "digital transformation",
        "ICT services", "ICT platform", "ICT infrastructure",
        "software development", "software platform", "application development",
        "system integration", "systems integration", "ERP",
        "cloud services", "cloud migration", "SaaS",
        "database", "data platform", "data warehouse",
        "technology services", "digital services",
        "all-of-government", "AoG ICT",
        "contact centre", "contact center",
        "digital platform", "technology solution", "ICT solution",
    ],
    # ── Defence ──────────────────────────────────────────────────────────────
    # Military and national security. 1 match is sufficient (threshold = 1).
    "defence": [
        "NZDF", "NZ Defence Force", "New Zealand Defence Force",
        "RNZAF", "Royal New Zealand Air Force",
        "RNZN", "Royal New Zealand Navy",
        "NZ Army", "New Zealand Army",
        "military", "defence force", "defense force",
        "naval", "air base", "defence estate",
    ],
    # ── Health ────────────────────────────────────────────────────────────────
    "health": [
        "health services", "health care", "healthcare",
        "clinical services", "clinical systems",
        "medical services", "medical supplies",
        "hospital", "Te Whatu Ora", "Health NZ",
        "nursing", "aged care", "residential care",
        "pharmacy", "mental health", "addiction services",
        "laboratory services", "diagnostic services",
        "allied health",
    ],
    # ── Advisory / Professional ────────────────────────────────────────────────
    # Requires 2+ matches to avoid grabbing notices that mention "review" once.
    "advisory": [
        "advisory services", "consulting services", "management consulting",
        "strategic review", "policy review", "policy advisory",
        "independent review", "business case",
        "programme evaluation", "evaluation services",
        "research services", "feasibility study",
        "technical advisory", "specialist advice",
    ],
    # ── Security (physical) ──────────────────────────────────────────────────
    # Physical / manned security. Requires 2+ to avoid grabbing ICT security.
    "security": [
        "security patrol", "security guard", "security officer",
        "security services", "manned guarding",
        "animal control", "parking enforcement",
        "CCTV monitoring", "alarm response", "alarm monitoring",
        "access control services", "protective services",
        "event security",
    ],
    # ── Utilities ─────────────────────────────────────────────────────────────
    "utilities": [
        "electricity supply", "power supply", "energy procurement",
        "gas supply", "utilities contract",
        "telecommunications services", "broadband services",
        "waste management", "recycling services",
        "water services contract",
    ],
    # ── Professional Services ────────────────────────────────────────────────
    "professional_services": [
        "legal services", "legal advice",
        "accounting services", "audit services",
        "human resources", "HR services",
        "recruitment services", "staffing services",
        "financial services", "financial advisory",
        "training services", "professional development",
    ],
}

# ── Value bands ───────────────────────────────────────────────────────────────
VALUE_BANDS: list[tuple[str, Optional[float], Optional[float]]] = [
    ("under_100k",   None,      100_000),
    ("100k_500k",    100_000,   500_000),
    ("500k_2m",      500_000,   2_000_000),
    ("2m_10m",       2_000_000, 10_000_000),
    ("10m_plus",     10_000_000, None),
]
VALUE_BAND_UNKNOWN = "unknown"

# ── Scoring weights ───────────────────────────────────────────────────────────
# Each dimension contributes up to the weight shown; composite is sum / total_weight * 10

SCORE_WEIGHTS = {
    "value":      3.0,   # contract value
    "sector":     3.0,   # strategic sector relevance
    "complexity": 2.0,   # evaluation complexity
    "urgency":    2.0,   # days-to-close urgency
}

# Value scores (mapped from band) — out of 1.0
VALUE_SCORE_MAP: dict[str, float] = {
    "under_100k":  0.2,
    "100k_500k":   0.4,
    "500k_2m":     0.65,
    "2m_10m":      0.85,
    "10m_plus":    1.0,
    "unknown":     0.3,
}

# Sector strategic priority — used only during pipeline scoring (stored to DB).
# These weights are intentionally kept for historical backward-compat; they are
# NOT used at render/output time.  At output time, client preferred_sectors
# determine sector scoring so each client sees their own sector ranked highest.
# See scoring.client_sector_score() for the render-time logic.
SECTOR_PRIORITY: dict[str, float] = {
    "FM":                   0.95,
    "infrastructure":       0.90,
    "defence":              0.90,
    "utilities":            0.85,
    "security":             0.85,
    "ICT":                  0.80,
    "advisory":             0.70,
    "professional_services":0.60,
    "health":               0.55,
    "other":                0.30,
}

# ── Client-configurable sector scoring (render-time) ──────────────────────────
# When no client sector preference is set, all sectors score equally (neutral).
# When preferred_sectors is provided, matching sectors score PREFERRED and all
# others score OTHER — making the client's sectors rise to the top.
SECTOR_SCORE_NEUTRAL:   float = 0.65   # applied to ALL sectors when no preference set
SECTOR_SCORE_PREFERRED: float = 1.00   # applied to sectors in client's preferred list
SECTOR_SCORE_OTHER:     float = 0.20   # applied to non-preferred sectors

# Bidder inference — specialist handling
# For notices in specialist sectors (aerospace, cyber, legal, health),
# if fewer than this many credible bidders are found after exclusion filtering,
# show what was found rather than padding with irrelevant general firms.
SPECIALIST_MIN_BIDDERS: int = 1

# Urgency scoring: days-to-close → score (out of 1.0)
URGENCY_THRESHOLDS: list[tuple[int, float]] = [
    (7,   1.00),   # ≤7 days → highest urgency
    (14,  0.80),
    (21,  0.60),
    (30,  0.40),
    (60,  0.20),
]
URGENCY_DEFAULT = 0.10  # >60 days or unknown

# Complexity heuristics: phrases that indicate high-complexity evaluation
COMPLEXITY_PHRASES: list[str] = [
    "weighted criteria", "technical and commercial", "best value", "multi-stage",
    "shortlist", "RFP", "request for proposal", "negotiation", "BAFO",
    "best and final", "expressions of interest", "two-stage",
]

# ── Enrichment ────────────────────────────────────────────────────────────────
PRIORITY_THRESHOLD: float = float(os.getenv("PRIORITY_THRESHOLD", "5.0"))

# ── Output ────────────────────────────────────────────────────────────────────
OUTPUT_DIR: str = "output"
TOP_N_WATCHLIST: int = int(os.getenv("TOP_N_WATCHLIST", "25"))
TOP_N_BIDDERS_PER_NOTICE: int = 3

# Cap on how many notices (sorted by score desc) receive Claude API bidder
# context enrichment per daily run. Notices outside this cap get rule-based
# bidder inference only. Keeps API cost and runtime predictable.
MAX_ENRICHMENT_NOTICES: int = int(os.getenv("MAX_ENRICHMENT_NOTICES", "20"))

# Score threshold for the daily HTML/MD/JSON watchlist output.
# Lower than PRIORITY_THRESHOLD so more notices appear in the report,
# but only notices above PRIORITY_THRESHOLD receive Claude notice enrichment
# and only the top MAX_ENRICHMENT_NOTICES receive Claude bidder context.
WATCHLIST_THRESHOLD: float = float(os.getenv("WATCHLIST_THRESHOLD", "4.0"))

# ── Bidder data ───────────────────────────────────────────────────────────────
BIDDER_CSV_PATH: str = "data/bidders.csv"

# ── Layer 2 — Knowledge graph & intelligence synthesis ────────────────────────

# GETS award notices search URL. Needs verification against live site.
GETS_AWARDS_URL: str = "https://www.gets.govt.nz/ExternalIndex.htm"
GETS_AWARDS_PARAMS: dict = {"status": "awarded", "ResultType": "tender"}

# Fuzzy name matching: minimum ratio (0–100) to treat two org names as the same
ORG_FUZZY_MATCH_THRESHOLD: int = int(os.getenv("ORG_FUZZY_MATCH_THRESHOLD", "88"))

# Minimum notices before generating a Claude agency profile narrative
AGENCY_PROFILE_MIN_NOTICES: int = int(os.getenv("AGENCY_PROFILE_MIN_NOTICES", "3"))

# Days ahead to flag contracts approaching renewal
RENEWAL_WINDOW_DAYS: int = int(os.getenv("RENEWAL_WINDOW_DAYS", "90"))

# Days lookback for procurement surge detection
SURGE_LOOKBACK_DAYS: int = int(os.getenv("SURGE_LOOKBACK_DAYS", "30"))

# Win streak threshold (consecutive awards in the same sector for same supplier)
WIN_STREAK_THRESHOLD: int = int(os.getenv("WIN_STREAK_THRESHOLD", "3"))

# Max notices to generate agency profiles for per Layer 2 run (cost control)
MAX_AGENCY_PROFILES_PER_RUN: int = int(os.getenv("MAX_AGENCY_PROFILES_PER_RUN", "15"))

# Max competitor assessments per Layer 2 run
MAX_COMPETITOR_ASSESSMENTS: int = int(os.getenv("MAX_COMPETITOR_ASSESSMENTS", "10"))

# Layer 2 output section title
LAYER2_SECTION_TITLE: str = "Market Intelligence"

# ── Layer 3 — Executive artefacts & client delivery ──────────────────────────

# Artefact output root (subdirs: {client_slug}/{date}/)
ARTEFACTS_DIR: str = os.getenv("ARTEFACTS_DIR", "output/artefacts")

# Model for Layer 3 (longer-form synthesis — can be overridden per client)
CLAUDE_MODEL_L3: str = os.getenv("CLAUDE_MODEL_L3", CLAUDE_MODEL)
CLAUDE_MAX_TOKENS_L3: int = int(os.getenv("CLAUDE_MAX_TOKENS_L3", "4096"))

# Portal auth (single shared password per deployment)
PORTAL_PASSWORD: str = os.getenv("PORTAL_PASSWORD", "changeme")
PORTAL_HOST: str = os.getenv("PORTAL_HOST", "127.0.0.1")
PORTAL_PORT: int = int(os.getenv("PORTAL_PORT", "5000"))
PORTAL_SECRET_KEY: str = os.getenv("PORTAL_SECRET_KEY", "change-this-secret-key")

# SMTP for weekly briefing emails
SMTP_HOST: str = os.getenv("SMTP_HOST", "")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USERNAME", os.getenv("SMTP_USER", ""))
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM: str = os.getenv("SMTP_FROM", "")
BRIEFING_RECIPIENTS: str = os.getenv("BRIEFING_RECIPIENTS", "")  # comma-separated

# Demo package branding (for cold-outreach samples)
DEMO_FIRM_NAME: str = os.getenv("DEMO_FIRM_NAME", "Procurement Win AI")
DEMO_CONTACT_EMAIL: str = os.getenv("DEMO_CONTACT_EMAIL", "")
DEMO_CONTACT_PHONE: str = os.getenv("DEMO_CONTACT_PHONE", "")
DEMO_WEBSITE: str = os.getenv("DEMO_WEBSITE", "")

# How many competitors to show in pursuit packages
PURSUIT_COMPETITOR_LIMIT: int = int(os.getenv("PURSUIT_COMPETITOR_LIMIT", "8"))

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
