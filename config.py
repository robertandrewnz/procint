"""
All tunable pipeline parameters. Adjust here without touching module code.
"""
import os
from dotenv import load_dotenv

load_dotenv()

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
    "FM",
    "infrastructure",
    "ICT",
    "advisory",
    "health",
    "security",
    "defence",
    "utilities",
    "professional_services",
    "other",
]

# Keywords used to map notice text/category to a sector tag
SECTOR_KEYWORDS: dict[str, list[str]] = {
    "FM": [
        "facilities", "facility management", "cleaning", "maintenance", "property",
        "building services", "FM", "grounds", "caretaking", "HVAC",
    ],
    "infrastructure": [
        "infrastructure", "construction", "roading", "roads", "highway", "bridge",
        "water", "wastewater", "stormwater", "pipeline", "structural", "civil",
    ],
    "ICT": [
        "ICT", "information technology", "software", "cloud", "cyber", "digital",
        "network", "systems integration", "SaaS", "platform", "data", "ERP",
    ],
    "advisory": [
        "advisory", "consulting", "consultancy", "strategy", "review", "audit",
        "assessment", "analysis", "research", "evaluation",
    ],
    "health": [
        "health", "clinical", "medical", "hospital", "aged care", "mental health",
        "pharmacy", "laboratory", "diagnostic",
    ],
    "security": [
        "security", "guarding", "CCTV", "access control", "surveillance",
        "protective services",
    ],
    "defence": [
        "defence", "defense", "NZDF", "military", "navy", "army", "air force",
        "intelligence", "national security",
    ],
    "utilities": [
        "utilities", "energy", "electricity", "gas", "telecoms", "telecommunications",
        "broadband", "waste management",
    ],
    "professional_services": [
        "legal", "legal services", "accounting", "HR", "human resources",
        "recruitment", "training", "professional services", "financial services",
    ],
}

# ── Value bands ───────────────────────────────────────────────────────────────
VALUE_BANDS: list[tuple[str, float | None, float | None]] = [
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

# Sector strategic priority — out of 1.0
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
TOP_N_WATCHLIST: int = 10
TOP_N_BIDDERS_PER_NOTICE: int = 3

# ── Bidder data ───────────────────────────────────────────────────────────────
BIDDER_CSV_PATH: str = "data/bidders.csv"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
