# Intel Library — Groundwork by BidEdge

Strategic intelligence document library. Monitors NZ government strategic documents,
extracts procurement signals from them, and feeds that context into Layer 1 scoring
and Layer 3 artefact generation.

---

## Module overview

```
intel_library/
  __init__.py            Package init
  schema.sql             Migration 005 — 7 tables + 3 views
  seed_sources.py        Populate intel_categories + intel_sources (58 sources)
  extract_signals.py     Fetch documents, extract signals via Claude
  scoring_integration.py Layer 1: get_strategic_score_boost()
  layer3_integration.py  Layer 3: build_strategic_context()
  scheduler_jobs.py      Scheduled refresh jobs
```

---

## Setup

### 1. Apply the database migration

```bash
psql $DATABASE_URL -f intel_library/schema.sql
```

This creates: `intel_categories`, `intel_sources`, `intel_snapshots`, `intel_signals`,
`intel_sector_profiles`, `intel_agency_profiles`, `intel_source_usage`, and views
`v_active_signals`, `v_sector_context`, `v_source_usage_summary`.

### 2. Seed sources and categories

```bash
python intel_library/seed_sources.py
```

Inserts 8 categories and 58 sources. Safe to re-run — uses upsert by title.
Also seeds baseline `intel_sector_profiles` for 5 sectors (infrastructure, ICT,
FM, defence, Construction).

### 3. Install new dependency

```bash
pip install pdfplumber
# or
pip install -r requirements.txt
```

### 4. Run initial Budget 2026 fetch (highest priority)

```bash
python intel_library/scheduler_jobs.py --initial
```

This fetches and extracts signals from all Budget 2026 documents (BEFU2026,
FSR2026, Vote PDFs), DCP2025, NZDIS, NZCSS-2026, GPR5, NIP2025, InfraPipeline,
and Treasury LTIB documents. Calls Claude for each source — allow 5-15 minutes.

---

## How to run the extractor manually

```bash
# Process all active sources
python intel_library/extract_signals.py --all

# Process a specific source by short_name or title fragment
python intel_library/extract_signals.py --source BEFU2026
python intel_library/extract_signals.py --source "Defence Capability"

# Process Budget 2026 sources only (highest priority)
python intel_library/extract_signals.py --budget

# Process Beehive daily sources only
python intel_library/extract_signals.py --daily

# Force re-extraction even if content unchanged
python intel_library/extract_signals.py --all --force

# Control request delay (default 1.5s)
python intel_library/extract_signals.py --all --delay 3.0
```

---

## Scheduled jobs

Add to `scheduler.py` or crontab:

```
# Daily 05:00 — Beehive press releases + speeches
0 5 * * *   cd /path/to/procint && python intel_library/scheduler_jobs.py --daily

# Sunday 06:00 — Full source refresh
0 6 * * 0   cd /path/to/procint && python intel_library/scheduler_jobs.py --weekly

# Quarterly — Infrastructure pipeline snapshot
0 6 1 1,4,7,10 * cd /path/to/procint && python intel_library/scheduler_jobs.py --quarterly

# Monthly 1st — Sector profiles + fortnightly economic update
0 5 1 * *   cd /path/to/procint && python intel_library/scheduler_jobs.py --monthly
```

Print library stats:
```bash
python intel_library/scheduler_jobs.py --stats
```

---

## Accessing /intel

The `/intel` admin page is accessible at `http://localhost:5000/intel`.

- **Requires login + admin flag** (`is_admin: true` in `portal_config.json`).
- Non-admin authenticated users receive HTTP 403.
- Unauthenticated users are redirected to `/login`.

The page provides:
- **Section 1**: Library overview — source count, signal count, Budget 2026 highlights.
- **Section 2**: Source table — all sources with last-checked date, signal count,
  usage count, avg significance. Budget 2026 sources pinned and highlighted in teal.
  Add source form at the bottom.
- **Section 3**: Signal feed — 50 most recent extracted signals with confidence,
  affected sectors/agencies, dollar values, source attribution.
- **Section 4**: Usage log — every time an intel source influenced a Groundwork output.

Trigger jobs from the UI:
- **Daily fetch** — fetches Beehive press releases and speeches.
- **Initial Budget fetch** — fetches all Budget 2026 and priority PDF sources.
- **Refresh all sources** — full weekly run (may take several minutes).

---

## Layer 1 scoring integration

```python
from intel_library.scoring_integration import get_strategic_score_boost, apply_boost_to_composite

boost = get_strategic_score_boost(notice_dict)
# boost = {
#   "modifier": 0.4,          # float -1.0 to +2.0
#   "signal_labels": [...],   # short titles of matching signals
#   "source_names": [...],    # source short names
#   "confidence": "high",
#   "signal_count": 3,
# }

# Apply to existing composite score
boosted_score = apply_boost_to_composite(composite_score, boost)
```

Budget 2026 / BEFU2026 signals are weighted at 1.5× automatically.
The result is capped between 1.0 and 10.0.

---

## Layer 3 enrichment integration

```python
from intel_library.layer3_integration import build_strategic_context

# Returns a formatted text block for injection into Claude prompts
ctx = build_strategic_context(
    notice_sector="infrastructure",
    agency_name="Waka Kotahi NZTA",
    used_in="notice:33920315",
    usage_type="pursuit_package",
)
```

Returns a `STRATEGIC ENVIRONMENT — [Sector]` block, e.g.:

```
STRATEGIC ENVIRONMENT — Infrastructure
Policy framework: GPS-Transport 2024, NPS-Infrastructure 2025, Budget 2026 $60B programme
Investment pipeline: $185B [InfraPipeline] | annual government spend ~$21.4B
Budget 2026:
  • $400M state highway resilience (BEFU2026)
  • KiwiRail $1.075B 2027-2030 (BEFU2026)
Key signals:
  • National Infrastructure Plan published — 30-year needs assessment [NIP2025]
Competitive dynamics: Dominant suppliers: Fulton Hogan, Downer, Fletcher Construction...
```

Inject this block before the MBIE data section in pursuit_package.py, watch_brief.py,
and competitor_profile.py Claude prompts.

---

## All seeded sources

### Procurement Rules & Policy

| Short name | Title |
|---|---|
| GPR5 | Government Procurement Rules, 5th Edition |
| GPS-Procurement | NZ Government Procurement Strategy |
| AoG | All-of-Government Contracts and Common Capability Register |
| — | Supplier Code of Conduct |
| — | Principles of Partnership — He Tūāpiri |

### Investment & Infrastructure Forecasts

| Short name | Title |
|---|---|
| NIP2025 | National Infrastructure Plan 2025 |
| InfraPipeline | NZ Infrastructure Pipeline — Quarterly Snapshot |
| — | Forward Guidance on Infrastructure Investment |
| BEFU2026 | Budget Economic and Fiscal Update 2026 ★ |
| FSR2026 | Fiscal Strategy Report 2026 |
| — | Budget 2026 Summary of Initiatives |
| — | Budget 2026 — Vote Documents (all capital votes) |
| LTIB-Treasury-2025 | Treasury Long-term Insights Briefing 2025 — Te Ara Mokopuna |
| LTFS-2025 | Treasury 2025 Long-term Fiscal Statement |
| IS-2025 | Treasury Investment Statement 2025 |
| FEU | Treasury Fortnightly Economic Update |
| — | Capital Intentions Plan |

### Sector Strategies

| Short name | Title |
|---|---|
| GPS-Transport | Government Policy Statement on Land Transport 2024 |
| NCPR-2025 | National Construction Pipeline Report 2025 |
| LTIB-MBIE-2025 | MBIE/MFAT Joint Long-term Insights Briefing 2025 |
| — | Digital Strategy for Aotearoa |
| — | All-of-Government ICT Strategy |
| DCP2025 | 2025 Defence Capability Plan |
| NZDIS | New Zealand Defence Industry Strategy |
| — | Australia–NZ Joint Statement on Closer Defence Relations |
| — | Health Infrastructure Programme / Health NZ Capital Plan |
| — | NZ School Property Agency Pipeline (NZSPA) |
| — | Kāinga Ora Development Pipeline |
| — | Corrections Capital Programme |

### National Security & Cyber Strategy

| Short name | Title |
|---|---|
| NZCSS-2026 | New Zealand Cyber Security Strategy 2026–2030 |
| CyberAP-2026 | New Zealand Cyber Security Action Plan 2026–2027 |
| — | Discussion Document — Enhancing Cyber Security of NZ's Critical Infrastructure |
| NCSC-Baseline | NCSC Mandatory Cybersecurity Baseline Standards |
| NCSC-CTR | NCSC Annual Cyber Threat Report 2023–24 |
| PSR | Protective Security Requirements Framework |
| — | NZ National Security Strategy |
| — | Critical Infrastructure Resilience Strategy |
| DPSS-2023 | Defence Policy and Strategy Statement 2023 |

### Regulatory & Planning Framework

| Short name | Title |
|---|---|
| NPS-Infrastructure | National Policy Statement for Infrastructure 2025 |
| ERP2 | NZ's Second Emissions Reduction Plan 2026–30 |
| — | Ministry of Justice — Future of Courts LTIB |

### Market Intelligence

| Short name | Title |
|---|---|
| — | MBIE Building and Construction Sector Trends Reporting Package |
| — | Stats NZ Business Demography Statistics |
| — | Construction Sector Accord — Transformation Plan |
| — | NZX Quarterly Results — Listed Contractors |
| — | MBIE Procurement Market Analysis |
| NZIAT | NZIAT Investment Priorities and Programme |

### Agency Intelligence

| Short name | Title |
|---|---|
| — | Waka Kotahi NZTA Annual Report and Statement of Intent |
| — | Te Waihanga / Infrastructure Commission Annual Report |
| — | Health NZ / Te Whatu Ora Procurement Pipeline |
| — | Ministry of Education School Property Pipeline |
| — | MCERT / Waka Kotahi Transport Investment Programme |

### Live Intelligence (daily/weekly refresh)

| Short name | Title |
|---|---|
| — | Beehive Press Releases — Ministerial Announcements |
| — | Beehive Ministerial Speeches |
| — | MBIE Newsroom |
| — | NZ Parliament Bills — Proposed Laws |
| — | NCSC News and Cyber Security Insights Quarterly Report |
| Budget2026-Full | Budget 2026 — All Documents (meta-source) ★ |

★ = Highest priority — signals weighted at 1.5× in scoring and enrichment.

---

## Agency structure notes (as of 2026)

- **MCERT** — Ministry of Cities, Environment, Regions and Transport. Formed by merger of
  MfE, MHuD, and MoT. Jurisdiction over transport policy, emissions, environment.
- **NZSPA** — NZ School Property Agency. Launching mid-2026, taking over school property
  from MoE School Property Group. Portfolio: 2,100+ schools, $33.5B value.
- **NIA** — National Infrastructure Agency. Being established by repurposing Crown
  Infrastructure Partners (CIP).

---

## Key signal weights

| Signal type | Base modifier |
|---|---|
| budget_increase | +0.40 |
| new_initiative | +0.35 |
| opportunity | +0.30 |
| policy_change | +0.20 |
| risk | −0.25 |

Budget 2026 / BEFU2026 signals receive an additional **1.5× multiplier**.
Confidence multipliers: high=1.0, medium=0.7, low=0.4.
Total modifier capped at −1.0 to +2.0. Applied composite capped at 1.0–10.0.
