"""
intel_library/seed_sources.py — Seed intel_categories and intel_sources.

Run once (or re-run safely — uses ON CONFLICT DO NOTHING for categories,
upserts sources by title to avoid duplicates).

Usage:
    python -m intel_library.seed_sources
    # or directly:
    python intel_library/seed_sources.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

import db

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

# ── Category definitions ──────────────────────────────────────────────────────

CATEGORIES = [
    {
        "name": "Procurement Rules & Policy",
        "icon": "📋",
        "description": "Mandatory procurement rules, AoG contracts, supplier codes.",
    },
    {
        "name": "Investment & Infrastructure Forecasts",
        "icon": "📊",
        "description": "Budget documents, infrastructure pipelines, fiscal updates.",
    },
    {
        "name": "Sector Strategies",
        "icon": "🏗️",
        "description": "Sector-specific government strategies and capital plans.",
    },
    {
        "name": "National Security & Cyber Strategy",
        "icon": "🛡️",
        "description": "Defence capability, cyber security strategies, PSR framework.",
    },
    {
        "name": "Regulatory & Planning Framework",
        "icon": "⚖️",
        "description": "National policy statements, emissions plans, justice pipeline.",
    },
    {
        "name": "Market Intelligence",
        "icon": "📈",
        "description": "Construction sector trends, competitor financials, Stats NZ.",
    },
    {
        "name": "Agency Intelligence",
        "icon": "🏛️",
        "description": "Agency annual reports, procurement pipelines, SOIs.",
    },
    {
        "name": "Live Intelligence",
        "icon": "⚡",
        "description": "Daily/weekly refreshed sources: Beehive, MBIE newsroom, Parliament.",
    },
]

# ── Source definitions ────────────────────────────────────────────────────────
# category_name must match exactly one entry in CATEGORIES[*]["name"].

SOURCES = [

    # ── PROCUREMENT RULES & POLICY ────────────────────────────────────────────

    {
        "category_name": "Procurement Rules & Policy",
        "title": "Government Procurement Rules, 5th Edition",
        "short_name": "GPR5",
        "publisher": "MBIE / NZ Government Procurement",
        "url": "https://www.procurement.govt.nz/government-procurement-framework/government-procurement-rules/",
        "pdf_url": "https://www.procurement.govt.nz/assets/procurement-property/documents/government-procurement-rules-5th-edition.pdf",
        "document_type": "policy",
        "update_frequency": "rolling",
        "nz_relevance_score": 10,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "Mandatory from 1 December 2025. Reduced from 71 to 47 rules. "
            "Key changes: new mandatory 10% minimum weighting for economic benefit to NZ in all evaluations; "
            "new proportionality principle; strengthened payment terms (Rule 36). "
            "Replaces 4th edition entirely."
        ),
    },
    {
        "category_name": "Procurement Rules & Policy",
        "title": "NZ Government Procurement Strategy",
        "short_name": "GPS-Procurement",
        "publisher": "MBIE",
        "url": "https://www.procurement.govt.nz/",
        "document_type": "strategy",
        "update_frequency": "rolling",
        "nz_relevance_score": 8,
        "procurement_relevance": ["all sectors"],
    },
    {
        "category_name": "Procurement Rules & Policy",
        "title": "All-of-Government Contracts and Common Capability Register",
        "short_name": "AoG",
        "publisher": "MBIE / NZ Government Procurement",
        "url": "https://www.procurement.govt.nz/procurement/all-of-government-contracts/",
        "document_type": "policy",
        "update_frequency": "rolling",
        "nz_relevance_score": 9,
        "procurement_relevance": ["ICT", "FM", "Professional Services"],
        "notes": (
            "Critical for panel intelligence — which AoG panels exist, who is on them, "
            "which agencies must use them. Determines whether an opportunity is open to "
            "new entrants or restricted to panel members."
        ),
    },
    {
        "category_name": "Procurement Rules & Policy",
        "title": "Supplier Code of Conduct",
        "publisher": "MBIE",
        "url": "https://www.procurement.govt.nz/procurement/principles-and-rules/supplier-code-of-conduct/",
        "document_type": "policy",
        "update_frequency": "one-off",
        "nz_relevance_score": 6,
        "procurement_relevance": ["all sectors"],
    },
    {
        "category_name": "Procurement Rules & Policy",
        "title": "Principles of Partnership — He Tūāpiri (Treaty procurement obligations)",
        "publisher": "MBIE",
        "url": "https://www.procurement.govt.nz/procurement/principles-and-rules/he-tuapiri-partnership-commitments/",
        "document_type": "policy",
        "update_frequency": "rolling",
        "nz_relevance_score": 7,
        "procurement_relevance": ["Construction", "FM", "Consultancy"],
    },

    # ── INVESTMENT & INFRASTRUCTURE FORECASTS ─────────────────────────────────

    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "National Infrastructure Plan 2025",
        "short_name": "NIP2025",
        "publisher": "NZ Infrastructure Commission / Te Waihanga",
        "url": "https://tewaihanga.govt.nz/national-infrastructure-plan-online/",
        "document_type": "strategy",
        "update_frequency": "5-yearly",
        "nz_relevance_score": 10,
        "procurement_relevance": ["Construction", "Roading", "Water", "Energy", "ICT", "FM"],
        "notes": (
            "First ever National Infrastructure Plan. Sets 30-year investment needs across all sectors. "
            "Government response expected June 2026 — fetch and process immediately when published."
        ),
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "NZ Infrastructure Pipeline — Quarterly Snapshot",
        "short_name": "InfraPipeline",
        "publisher": "NZ Infrastructure Commission / Te Waihanga",
        "url": "https://tewaihanga.govt.nz/the-pipeline/pipeline-snapshot",
        "document_type": "forecast",
        "update_frequency": "quarterly",
        "nz_relevance_score": 10,
        "procurement_relevance": ["Construction", "Roading", "Water", "Energy", "FM"],
        "notes": (
            "December 2025 snapshot shows $21.4B projected spend in 2026, "
            "$185B total funded pipeline across 12,000+ initiatives. "
            "Directly maps to GETS opportunities. Fetch every quarter."
        ),
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Forward Guidance on Infrastructure Investment",
        "publisher": "NZ Infrastructure Commission / Te Waihanga",
        "url": "https://tewaihanga.govt.nz/",
        "document_type": "forecast",
        "update_frequency": "annual",
        "nz_relevance_score": 9,
        "procurement_relevance": ["Construction", "Water", "Health", "Education", "Roading"],
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Budget Economic and Fiscal Update 2026",
        "short_name": "BEFU2026",
        "publisher": "The Treasury",
        "url": "https://www.treasury.govt.nz/publications/efu/budget-economic-and-fiscal-update-2026",
        "pdf_url": "https://www.treasury.govt.nz/sites/default/files/2026-05/befu26.pdf",
        "document_type": "forecast",
        "update_frequency": "annual",
        "nz_relevance_score": 10,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "HIGHEST PRIORITY DOCUMENT. Weight signals at 1.5x. "
            "Central forecast: Strait of Hormuz closure pushed Brent crude to US$138/barrel peak April 2026, "
            "easing to ~$77/barrel by mid-2027. GDP growth slowing through 2026-27, unemployment peaking at 5.5% mid-2026. "
            "$60B infrastructure over 4 years, $7B new spending. Defence $1.2B operating + $2.3B capital uplift. "
            "Health NZ $153.6M cyber/IT. Justice $100M courthouses. KiwiRail $1.075B 2027-2030. "
            "$400M state highway resilience. $156M intelligence/security uplift. $600M fuel crisis response."
        ),
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Fiscal Strategy Report 2026",
        "short_name": "FSR2026",
        "publisher": "The Treasury",
        "url": "https://budget.govt.nz/budget/2026/fiscal-strategy-report/",
        "document_type": "forecast",
        "update_frequency": "annual",
        "nz_relevance_score": 9,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "$3.5B capital available per year for 4 years per Budget Policy Statement. "
            "Core Crown expenses target: 30% of GDP."
        ),
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Budget 2026 Summary of Initiatives",
        "publisher": "The Treasury",
        "url": "https://budget.govt.nz/budget/pdfs/summary-initiatives/b26-sum-initiatives.pdf",
        "document_type": "report",
        "update_frequency": "annual",
        "nz_relevance_score": 10,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "Line-by-line breakdown of every Budget 2026 initiative with dollar values. "
            "Fetch and parse all construction, ICT, FM, defence, health, justice, and corrections line items."
        ),
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Budget 2026 — Vote Documents (all capital votes)",
        "publisher": "The Treasury",
        "url": "https://budget.govt.nz/",
        "document_type": "forecast",
        "update_frequency": "annual",
        "nz_relevance_score": 10,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "Fetch individual Vote PDFs: Vote Transport, Vote Health, Vote Education, Vote Defence, "
            "Vote Housing, Vote Justice, Vote Infrastructure, Vote Corrections, "
            "Vote Digital Government, Vote Intelligence and Security. "
            "Extract capital appropriations and named programmes from each."
        ),
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Treasury Long-term Insights Briefing 2025 — Te Ara Mokopuna",
        "short_name": "LTIB-Treasury-2025",
        "publisher": "The Treasury",
        "url": "https://www.treasury.govt.nz/publications/ltib/te-ara-mokopuna-2025",
        "document_type": "report",
        "update_frequency": "3-yearly",
        "nz_relevance_score": 8,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "Covers fiscal policy through shocks and cycles — when government should maintain/rebuild "
            "critical infrastructure. Directly relevant given Hormuz supply shock context. "
            "Signals government commitment to infrastructure as economic stabiliser."
        ),
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Treasury 2025 Long-term Fiscal Statement",
        "short_name": "LTFS-2025",
        "publisher": "The Treasury",
        "url": "https://www.treasury.govt.nz/publications/treasurys-stewardship-reports",
        "document_type": "report",
        "update_frequency": "3-yearly",
        "nz_relevance_score": 8,
        "procurement_relevance": ["all sectors"],
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Treasury Investment Statement 2025",
        "short_name": "IS-2025",
        "publisher": "The Treasury",
        "url": "https://www.treasury.govt.nz/publications/treasurys-stewardship-reports",
        "document_type": "report",
        "update_frequency": "3-yearly",
        "nz_relevance_score": 8,
        "procurement_relevance": ["Construction", "ICT", "FM", "Health", "Education"],
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Treasury Fortnightly Economic Update",
        "short_name": "FEU",
        "publisher": "The Treasury",
        "url": "https://www.treasury.govt.nz/publications/research-and-commentary/fortnightly-economic-updates",
        "document_type": "report",
        "update_frequency": "fortnightly",
        "nz_relevance_score": 7,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "Live economic updates — fetch latest fortnightly edition. "
            "March 2026 edition covered Hormuz oil price scenarios in detail. "
            "Signals shifts in agency spending capacity between budgets."
        ),
    },
    {
        "category_name": "Investment & Infrastructure Forecasts",
        "title": "Capital Intentions Plan",
        "publisher": "The Treasury",
        "url": "https://www.treasury.govt.nz/",
        "document_type": "forecast",
        "update_frequency": "annual",
        "nz_relevance_score": 8,
        "procurement_relevance": ["all sectors"],
    },

    # ── SECTOR STRATEGIES ─────────────────────────────────────────────────────

    {
        "category_name": "Sector Strategies",
        "title": "Government Policy Statement on Land Transport 2024",
        "short_name": "GPS-Transport",
        "publisher": "MCERT / Waka Kotahi NZTA",
        "url": "https://www.nzta.govt.nz/planning-and-investment/gps/",
        "document_type": "strategy",
        "update_frequency": "3-yearly",
        "nz_relevance_score": 10,
        "procurement_relevance": ["Roading", "Transport", "Construction"],
        "notes": (
            "Sets NZTA's multi-billion dollar investment priorities. "
            "Primary driver of roading and transport procurement pipeline. "
            "Now under MCERT jurisdiction."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "National Construction Pipeline Report 2025",
        "short_name": "NCPR-2025",
        "publisher": "MBIE / BRANZ / Pacifecon",
        "url": "https://www.mbie.govt.nz/building-and-energy/building/supporting-a-skilled-and-productive-workforce/national-construction-pipeline-report",
        "document_type": "report",
        "update_frequency": "annual",
        "nz_relevance_score": 9,
        "procurement_relevance": ["Construction", "FM", "Roading"],
        "notes": (
            "6-year projection to 2030. Total construction activity forecast to recover from $55.7B in 2025, "
            "rising to $65.4B by 2030. Non-residential: $12.1B in 2024, rising to $13.5B by 2030. "
            "Essential for scoring construction notices against market capacity."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "MBIE/MFAT Joint Long-term Insights Briefing 2025 — New Zealand's Productivity in a Changing World",
        "short_name": "LTIB-MBIE-2025",
        "publisher": "MBIE / MFAT",
        "url": "https://www.mbie.govt.nz/business-and-employment/economic-growth/long-term-insights-briefings/new-zealands-productivity-in-a-changing-world",
        "pdf_url": "https://www.mbie.govt.nz/dmsdocument/31696-new-zealands-productivity-in-a-changing-world-mbie-mfat-long-term-insights-briefing-2025",
        "document_type": "report",
        "update_frequency": "3-yearly",
        "nz_relevance_score": 8,
        "procurement_relevance": ["ICT", "Advanced Manufacturing", "Professional Services"],
        "notes": (
            "Focus on accelerating high-productivity sectors. "
            "Signals where government will direct ICT and advanced technology procurement."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "Digital Strategy for Aotearoa",
        "publisher": "MBIE",
        "url": "https://www.digital.govt.nz/digital-government/strategy/digital-strategy-for-aotearoa/",
        "document_type": "strategy",
        "update_frequency": "rolling",
        "nz_relevance_score": 8,
        "procurement_relevance": ["ICT", "Digital"],
    },
    {
        "category_name": "Sector Strategies",
        "title": "All-of-Government ICT Strategy",
        "publisher": "DIA",
        "url": "https://www.digital.govt.nz/",
        "document_type": "strategy",
        "update_frequency": "rolling",
        "nz_relevance_score": 8,
        "procurement_relevance": ["ICT"],
    },
    {
        "category_name": "Sector Strategies",
        "title": "2025 Defence Capability Plan",
        "short_name": "DCP2025",
        "publisher": "Ministry of Defence",
        "url": "https://www.defence.govt.nz/publications/2025-defence-capability-plan/",
        "document_type": "strategy",
        "update_frequency": "2-yearly",
        "nz_relevance_score": 10,
        "procurement_relevance": ["Defence", "ICT", "Construction", "Maritime", "Aerospace"],
        "notes": (
            "$12B over 4 years, $9B new spending. 15-year investment horizon. "
            "Raises defence spending to 2% GDP over 8 years. "
            "Three strategic priorities: combat capability/lethality, ANZAC force integration with Australia, "
            "innovation/ISR/uncrewed systems. Covers maritime, land, aerospace, and information domains. "
            "Requires NZ Industry Capability Plans from prime suppliers."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "New Zealand Defence Industry Strategy — Delivering Capability Faster",
        "short_name": "NZDIS",
        "publisher": "Ministry of Defence",
        "url": "https://www.defence.govt.nz/business-and-industry/defence-industry-strategy/",
        "pdf_url": "https://www.defence.govt.nz/publications/new-zealand-defence-industry-strategy-delivering-capability-faster/",
        "document_type": "strategy",
        "update_frequency": "rolling",
        "nz_relevance_score": 10,
        "procurement_relevance": ["Defence", "ICT", "Advanced Manufacturing", "Space"],
        "notes": (
            "Outlines 4-year implementation plan for DCP2025. "
            "Three Strategic Industrial Base Priorities: space capabilities, uncrewed systems and counter-systems, "
            "and sustainment. Introduces NZ Industry Capability Plans for prime suppliers. "
            "Promotes 'Thin Prime' model for NZ SMEs. Critical for competitor landscape in defence."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "Australia–New Zealand Joint Statement on Closer Defence Relations, December 2024",
        "publisher": "Ministry of Defence / Australian DoD",
        "url": "https://defence.govt.nz/publications/australia-new-zealand-joint-statement-on-closer-defence-relations/",
        "document_type": "strategy",
        "update_frequency": "one-off",
        "nz_relevance_score": 8,
        "procurement_relevance": ["Defence", "ICT", "Maritime"],
        "notes": (
            "Commits to increasingly integrated ANZAC force. "
            "Signals interoperability requirements that flow into procurement specs."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "Health Infrastructure Programme / Health NZ Capital Plan",
        "publisher": "Health NZ / Te Whatu Ora",
        "url": "https://www.tewhatuora.govt.nz/",
        "document_type": "forecast",
        "update_frequency": "rolling",
        "nz_relevance_score": 9,
        "procurement_relevance": ["Construction", "FM", "ICT"],
        "notes": (
            "Budget 2026 added $153.6M cyber/IT uplift and continues hospital build programme. "
            "Nelson Hospital redevelopment and Wellington Emergency Department among named projects."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "NZ School Property Agency Pipeline (NZSPA)",
        "publisher": "NZ School Property Agency / Ministry of Education",
        "url": "https://www.education.govt.nz/our-work/infrastructure/school-property/",
        "document_type": "forecast",
        "update_frequency": "rolling",
        "nz_relevance_score": 9,
        "procurement_relevance": ["Construction", "FM"],
        "notes": (
            "NZSPA launches mid-2026, taking over MoE School Property Group. "
            "Portfolio: 2,100+ schools, 8,000 hectares, $33.5B value. "
            "Second-largest social property portfolio in NZ. "
            "Major ongoing construction and FM procurement source."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "Kāinga Ora Development Pipeline",
        "publisher": "Kāinga Ora",
        "url": "https://kaingaora.govt.nz/",
        "document_type": "forecast",
        "update_frequency": "rolling",
        "nz_relevance_score": 8,
        "procurement_relevance": ["Construction"],
        "notes": (
            "Subject to ongoing government review of Kāinga Ora's scale and funding. "
            "Monitor for programme changes."
        ),
    },
    {
        "category_name": "Sector Strategies",
        "title": "Corrections Capital Programme",
        "publisher": "Department of Corrections",
        "url": "https://www.corrections.govt.nz/",
        "document_type": "forecast",
        "update_frequency": "rolling",
        "nz_relevance_score": 8,
        "procurement_relevance": ["Construction", "FM"],
        "notes": (
            "Budget 2026 allocated $500M for frontline services including capital works. "
            "Ongoing prison maintenance and upgrade programme."
        ),
    },

    # ── NATIONAL SECURITY & CYBER STRATEGY ───────────────────────────────────

    {
        "category_name": "National Security & Cyber Strategy",
        "title": "New Zealand Cyber Security Strategy 2026–2030",
        "short_name": "NZCSS-2026",
        "publisher": "DPMC",
        "url": "https://www.dpmc.govt.nz/our-programmes/national-security/cyber-security-strategy",
        "pdf_url": "https://www.dpmc.govt.nz/sites/default/files/2026-02/nz-cyber-security-strategy-2026-30.pdf",
        "document_type": "strategy",
        "update_frequency": "5-yearly",
        "nz_relevance_score": 10,
        "procurement_relevance": ["ICT", "Defence", "Critical Infrastructure"],
        "notes": (
            "Four pillars: Understand, Prevent & Prepare, Respond, Partner. "
            "Focuses on critical infrastructure cyber uplift. "
            "Key signal: all agencies must lift cyber capability — creates ICT procurement wave."
        ),
    },
    {
        "category_name": "National Security & Cyber Strategy",
        "title": "New Zealand Cyber Security Action Plan 2026–2027",
        "short_name": "CyberAP-2026",
        "publisher": "DPMC",
        "url": "https://www.dpmc.govt.nz/publications/new-zealands-cyber-security-action-plan-2026-2027",
        "document_type": "strategy",
        "update_frequency": "2-yearly",
        "nz_relevance_score": 9,
        "procurement_relevance": ["ICT", "Critical Infrastructure"],
        "notes": (
            "Immediate implementation steps for NZCSS-2026. "
            "Key initiative: mandatory minimum cyber security standards for critical infrastructure entities. "
            "Procurement signal: agencies contracting for security uplift services."
        ),
    },
    {
        "category_name": "National Security & Cyber Strategy",
        "title": "Discussion Document — Enhancing the Cyber Security of NZ's Critical Infrastructure System",
        "publisher": "DPMC",
        "url": "https://www.dpmc.govt.nz/our-programmes/national-security/critical-infrastructure",
        "document_type": "guidance",
        "update_frequency": "one-off",
        "nz_relevance_score": 9,
        "procurement_relevance": ["ICT", "Energy", "Water", "Transport", "Health"],
        "notes": (
            "Consultation closed 19 April 2026. Policy decisions pending. "
            "Will mandate minimum cyber defence levels for critical infrastructure entities. "
            "Outcome expected to drive major ICT security contracts."
        ),
    },
    {
        "category_name": "National Security & Cyber Strategy",
        "title": "NCSC Mandatory Cybersecurity Baseline Standards for Public Sector Agencies",
        "short_name": "NCSC-Baseline",
        "publisher": "NCSC / GCSB",
        "url": "https://www.ncsc.govt.nz/",
        "document_type": "policy",
        "update_frequency": "rolling",
        "nz_relevance_score": 9,
        "procurement_relevance": ["ICT"],
        "notes": (
            "Mandatory for all GCISO-mandated agencies from October 2025. "
            "Coordinated with PSR team. Agencies must meet baseline and demonstrate compliance. "
            "Creates procurement demand for security uplift services and tools."
        ),
    },
    {
        "category_name": "National Security & Cyber Strategy",
        "title": "NCSC Annual Cyber Threat Report 2023–24",
        "short_name": "NCSC-CTR",
        "publisher": "NCSC / GCSB",
        "url": "https://www.ncsc.govt.nz/insights-and-research/cyber-threat-reports/",
        "document_type": "report",
        "update_frequency": "annual",
        "nz_relevance_score": 8,
        "procurement_relevance": ["ICT", "Defence"],
        "notes": (
            "First combined CERT NZ + NCSC report. 7,122 incidents reported in 2023-24 period. "
            "Signals threat environment driving agency security procurement decisions. "
            "Fetch latest edition annually."
        ),
    },
    {
        "category_name": "National Security & Cyber Strategy",
        "title": "Protective Security Requirements Framework",
        "short_name": "PSR",
        "publisher": "NZSIS / DPMC",
        "url": "https://www.protectivesecurity.govt.nz/about/framework",
        "document_type": "policy",
        "update_frequency": "rolling",
        "nz_relevance_score": 8,
        "procurement_relevance": ["ICT", "FM", "Construction", "Physical Security"],
        "notes": (
            "Mandatory for 37 government agencies. Four pillars: security governance, personnel security, "
            "information security, physical security. Includes NZ Information Security Manual (NZISM). "
            "Agencies contract for PSR compliance services — FM, physical security, ICT security."
        ),
    },
    {
        "category_name": "National Security & Cyber Strategy",
        "title": "NZ National Security Strategy",
        "publisher": "DPMC",
        "url": "https://www.dpmc.govt.nz/our-programmes/national-security",
        "document_type": "strategy",
        "update_frequency": "rolling",
        "nz_relevance_score": 8,
        "procurement_relevance": ["Defence", "ICT", "Critical Infrastructure"],
    },
    {
        "category_name": "National Security & Cyber Strategy",
        "title": "Critical Infrastructure Resilience Strategy",
        "publisher": "DPMC",
        "url": "https://www.dpmc.govt.nz/our-programmes/national-security/critical-infrastructure",
        "document_type": "strategy",
        "update_frequency": "rolling",
        "nz_relevance_score": 9,
        "procurement_relevance": ["ICT", "Construction", "Energy", "Water", "Transport"],
    },
    {
        "category_name": "National Security & Cyber Strategy",
        "title": "Defence Policy and Strategy Statement 2023",
        "short_name": "DPSS-2023",
        "publisher": "Ministry of Defence",
        "url": "https://www.defence.govt.nz/our-work/plan-and-assess/defence-policy-review/",
        "document_type": "strategy",
        "update_frequency": "one-off",
        "nz_relevance_score": 7,
        "procurement_relevance": ["Defence"],
        "notes": "August 2023 contextual predecessor to DCP2025.",
    },

    # ── REGULATORY & PLANNING FRAMEWORK ──────────────────────────────────────

    {
        "category_name": "Regulatory & Planning Framework",
        "title": "National Policy Statement for Infrastructure 2025",
        "short_name": "NPS-Infrastructure",
        "publisher": "MCERT / NZ Gazette",
        "url": "https://gazette.govt.nz/notice/id/2025-sl7039",
        "document_type": "policy",
        "update_frequency": "one-off",
        "nz_relevance_score": 9,
        "procurement_relevance": ["Construction", "Roading", "Energy", "Water", "Telecoms"],
        "notes": (
            "First national direction specifically enabling infrastructure under the new resource management system. "
            "Directs consent decision-makers to recognise and provide for benefits of infrastructure. "
            "Reduces consent delays — accelerates pipeline delivery. Gazetted December 2025, in force 15 January 2026."
        ),
    },
    {
        "category_name": "Regulatory & Planning Framework",
        "title": "NZ's Second Emissions Reduction Plan 2026–30 (Amended January 2026)",
        "short_name": "ERP2",
        "publisher": "MCERT (formerly MfE)",
        "url": "https://environment.govt.nz/publications/new-zealands-second-emissions-reduction-plan/",
        "document_type": "strategy",
        "update_frequency": "5-yearly",
        "nz_relevance_score": 7,
        "procurement_relevance": ["Construction", "Energy", "Transport"],
        "notes": (
            "Signals decarbonisation obligations flowing into procurement specs. "
            "GPR5 requires agencies to consider decarbonisation where it represents good public value."
        ),
    },
    {
        "category_name": "Regulatory & Planning Framework",
        "title": "Ministry of Justice — Future of Courts and Justice Services LTIB (December 2025)",
        "publisher": "Ministry of Justice",
        "url": "https://www.justice.govt.nz/justice-sector-policy/key-initiatives/",
        "document_type": "report",
        "update_frequency": "3-yearly",
        "nz_relevance_score": 8,
        "procurement_relevance": ["Construction", "ICT"],
        "notes": (
            "30-year justice infrastructure pipeline. Budget 2026 funded $100M for two new Rotorua courthouses "
            "(construction 2027, complete mid-2030). Te Au Reka digital courts programme ongoing. "
            "PPP courthouse model under consideration for further sites."
        ),
    },

    # ── MARKET INTELLIGENCE ───────────────────────────────────────────────────

    {
        "category_name": "Market Intelligence",
        "title": "MBIE Building and Construction Sector Trends Reporting Package",
        "publisher": "MBIE",
        "url": "https://www.mbie.govt.nz/building-and-energy/building/building-system-insights-programme/sector-trends-reporting",
        "document_type": "report",
        "update_frequency": "biannual",
        "nz_relevance_score": 8,
        "procurement_relevance": ["Construction", "FM"],
    },
    {
        "category_name": "Market Intelligence",
        "title": "Stats NZ Business Demography Statistics",
        "publisher": "Stats NZ",
        "url": "https://www.stats.govt.nz/topics/business-demography",
        "document_type": "report",
        "update_frequency": "annual",
        "nz_relevance_score": 6,
        "procurement_relevance": ["all sectors"],
    },
    {
        "category_name": "Market Intelligence",
        "title": "Construction Sector Accord — Transformation Plan",
        "publisher": "MBIE",
        "url": "https://www.constructionaccord.govt.nz",
        "document_type": "strategy",
        "update_frequency": "rolling",
        "nz_relevance_score": 7,
        "procurement_relevance": ["Construction"],
    },
    {
        "category_name": "Market Intelligence",
        "title": "NZX Quarterly Results — Listed Contractors (Fletcher Building, Downer, Fulton Hogan parent Bouygues)",
        "publisher": "NZX / ASX",
        "url": "https://www.nzx.com/",
        "document_type": "report",
        "update_frequency": "quarterly",
        "nz_relevance_score": 7,
        "procurement_relevance": ["Construction", "Roading", "FM"],
        "notes": (
            "Signals competitor financial health, capacity constraints, and strategic direction. "
            "Monitor FBU.NZ, DOW.AX quarterly results."
        ),
    },
    {
        "category_name": "Market Intelligence",
        "title": "MBIE Procurement Market Analysis",
        "publisher": "MBIE",
        "url": "https://www.procurement.govt.nz/",
        "document_type": "report",
        "update_frequency": "annual",
        "nz_relevance_score": 7,
        "procurement_relevance": ["all sectors"],
    },
    {
        "category_name": "Market Intelligence",
        "title": "NZIAT Investment Priorities and Programme",
        "short_name": "NZIAT",
        "publisher": "NZIAT / MBIE",
        "url": "https://www.mbie.govt.nz/",
        "document_type": "report",
        "update_frequency": "rolling",
        "nz_relevance_score": 7,
        "procurement_relevance": ["ICT", "Advanced Technology", "Research"],
        "notes": (
            "NZ Institute for Advanced Technology established July 2025 as MBIE unit. "
            "Focus: AI, quantum computing, advanced materials. "
            "Signals government ICT and research contracting direction. "
            "Absorbs Technology Incubator, NZ Product Accelerator, HealthTech Activator."
        ),
    },

    # ── AGENCY INTELLIGENCE ───────────────────────────────────────────────────

    {
        "category_name": "Agency Intelligence",
        "title": "Waka Kotahi NZTA Annual Report and Statement of Intent",
        "publisher": "Waka Kotahi NZTA",
        "url": "https://www.nzta.govt.nz/resources/annual-report/",
        "document_type": "report",
        "update_frequency": "annual",
        "nz_relevance_score": 9,
        "procurement_relevance": ["Roading", "Transport", "Construction"],
    },
    {
        "category_name": "Agency Intelligence",
        "title": "Te Waihanga / Infrastructure Commission Annual Report",
        "publisher": "NZ Infrastructure Commission",
        "url": "https://tewaihanga.govt.nz/",
        "document_type": "report",
        "update_frequency": "annual",
        "nz_relevance_score": 8,
        "procurement_relevance": ["Construction", "all infrastructure sectors"],
    },
    {
        "category_name": "Agency Intelligence",
        "title": "Health NZ / Te Whatu Ora Procurement Pipeline",
        "publisher": "Health NZ",
        "url": "https://www.tewhatuora.govt.nz/",
        "document_type": "forecast",
        "update_frequency": "rolling",
        "nz_relevance_score": 9,
        "procurement_relevance": ["FM", "ICT", "Construction"],
    },
    {
        "category_name": "Agency Intelligence",
        "title": "Ministry of Education School Property Pipeline (transitioning to NZSPA mid-2026)",
        "publisher": "Ministry of Education / NZSPA",
        "url": "https://www.education.govt.nz/our-work/infrastructure/school-property/",
        "document_type": "forecast",
        "update_frequency": "rolling",
        "nz_relevance_score": 9,
        "procurement_relevance": ["Construction", "FM"],
    },
    {
        "category_name": "Agency Intelligence",
        "title": "MCERT / Waka Kotahi Transport Investment Programme",
        "publisher": "MCERT / Waka Kotahi",
        "url": "https://www.nzta.govt.nz/planning-and-investment/",
        "document_type": "forecast",
        "update_frequency": "annual",
        "nz_relevance_score": 9,
        "procurement_relevance": ["Roading", "Transport", "Construction"],
    },

    # ── LIVE INTELLIGENCE ─────────────────────────────────────────────────────

    {
        "category_name": "Live Intelligence",
        "title": "Beehive Press Releases — Ministerial Announcements",
        "publisher": "NZ Government",
        "url": "https://www.beehive.govt.nz/press-releases",
        "document_type": "news",
        "update_frequency": "daily",
        "nz_relevance_score": 8,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "Scrape for announcements referencing procurement, infrastructure, capital investment, "
            "defence, health, education, justice, corrections. "
            "Filter out political/social announcements with no procurement signal."
        ),
    },
    {
        "category_name": "Live Intelligence",
        "title": "Beehive Ministerial Speeches",
        "publisher": "NZ Government",
        "url": "https://www.beehive.govt.nz/speeches",
        "document_type": "speech",
        "update_frequency": "daily",
        "nz_relevance_score": 7,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "Ministers often signal programme intentions in speeches before formal documents are published. "
            "Particularly: Minister of Infrastructure (Bishop), Minister of Defence (Collins), "
            "Minister of Finance (Willis), Minister of Health."
        ),
    },
    {
        "category_name": "Live Intelligence",
        "title": "MBIE Newsroom",
        "publisher": "MBIE",
        "url": "https://www.mbie.govt.nz/about/news",
        "document_type": "news",
        "update_frequency": "weekly",
        "nz_relevance_score": 7,
        "procurement_relevance": ["all sectors"],
    },
    {
        "category_name": "Live Intelligence",
        "title": "NZ Parliament Bills — Proposed Laws",
        "publisher": "NZ Parliament",
        "url": "https://www.parliament.nz/en/pb/bills-and-laws/bills-proposed-laws/",
        "document_type": "guidance",
        "update_frequency": "weekly",
        "nz_relevance_score": 7,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "Flag any Bill referencing procurement thresholds, infrastructure investment, "
            "sector regulation, or public finance obligations."
        ),
    },
    {
        "category_name": "Live Intelligence",
        "title": "NCSC News and Cyber Security Insights Quarterly Report",
        "publisher": "NCSC / GCSB",
        "url": "https://www.ncsc.govt.nz/news/",
        "document_type": "report",
        "update_frequency": "quarterly",
        "nz_relevance_score": 7,
        "procurement_relevance": ["ICT", "Defence"],
        "notes": "Q3 2025: 1,249 incidents. Tracks threat environment driving agency cyber procurement decisions.",
    },
    {
        "category_name": "Live Intelligence",
        "title": "Budget 2026 — All Documents (BEFU, FSR, Vote PDFs, At a Glance)",
        "short_name": "Budget2026-Full",
        "publisher": "The Treasury",
        "url": "https://budget.govt.nz/",
        "document_type": "forecast",
        "update_frequency": "annual",
        "nz_relevance_score": 10,
        "procurement_relevance": ["all sectors"],
        "notes": (
            "HIGHEST PRIORITY. Seed this as a meta-source pointing to all Budget 2026 documents. "
            "Signal weight: 1.5x across all scoring and enrichment. "
            "Fetch BEFU, FSR, Summary of Initiatives, and all individual Vote PDFs on first run."
        ),
    },
]


# ── Seed functions ─────────────────────────────────────────────────────────────

def _seed_categories() -> dict:
    """Insert categories, return {name: id} mapping."""
    cat_id_map = {}
    for cat in CATEGORIES:
        existing = db.fetchone(
            "SELECT id FROM intel_categories WHERE name = %s",
            (cat["name"],),
        )
        if existing:
            cat_id_map[cat["name"]] = existing["id"]
            logger.info("Category exists: %s (id=%s)", cat["name"], existing["id"])
        else:
            db.execute(
                """
                INSERT INTO intel_categories (name, icon, description)
                VALUES (%s, %s, %s)
                """,
                (cat["name"], cat.get("icon"), cat.get("description")),
            )
            row = db.fetchone(
                "SELECT id FROM intel_categories WHERE name = %s",
                (cat["name"],),
            )
            cat_id_map[cat["name"]] = row["id"]
            logger.info("Inserted category: %s (id=%s)", cat["name"], row["id"])
    return cat_id_map


def _seed_sources(cat_id_map: dict) -> int:
    """Upsert all sources. Returns count of new inserts."""
    inserted = 0
    for src in SOURCES:
        cat_name = src.get("category_name", "")
        cat_id = cat_id_map.get(cat_name)
        if not cat_id:
            logger.warning("Unknown category '%s' for source '%s' — skipping", cat_name, src["title"])
            continue

        existing = db.fetchone(
            "SELECT id FROM intel_sources WHERE title = %s",
            (src["title"],),
        )
        if existing:
            # Update notes and other fields so re-running refreshes the data
            db.execute(
                """
                UPDATE intel_sources
                SET category_id           = %s,
                    short_name            = %s,
                    publisher             = %s,
                    url                   = %s,
                    pdf_url               = %s,
                    document_type         = %s,
                    update_frequency      = %s,
                    nz_relevance_score    = %s,
                    procurement_relevance = %s,
                    notes                 = %s
                WHERE id = %s
                """,
                (
                    cat_id,
                    src.get("short_name"),
                    src.get("publisher"),
                    src.get("url"),
                    src.get("pdf_url"),
                    src["document_type"],
                    src.get("update_frequency"),
                    src.get("nz_relevance_score"),
                    src.get("procurement_relevance", []),
                    src.get("notes"),
                    existing["id"],
                ),
            )
            logger.info("Updated source: %s", src["title"][:70])
        else:
            db.execute(
                """
                INSERT INTO intel_sources (
                    category_id, title, short_name, publisher, url, pdf_url,
                    document_type, update_frequency, nz_relevance_score,
                    procurement_relevance, notes, is_active
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                """,
                (
                    cat_id,
                    src["title"],
                    src.get("short_name"),
                    src.get("publisher"),
                    src.get("url"),
                    src.get("pdf_url"),
                    src["document_type"],
                    src.get("update_frequency"),
                    src.get("nz_relevance_score"),
                    src.get("procurement_relevance", []),
                    src.get("notes"),
                ),
            )
            logger.info("Inserted source: %s", src["title"][:70])
            inserted += 1
    return inserted


def seed_initial_sector_profiles() -> None:
    """Insert baseline sector profiles if none exist."""
    profiles = [
        {
            "sector": "infrastructure",
            "government_spend_annual": 21_400_000_000,
            "pipeline_value": 185_000_000_000,
            "top_agencies": ["Waka Kotahi NZTA", "NZ Infrastructure Commission", "KiwiRail",
                             "Local Government NZ"],
            "dominant_suppliers": ["Fulton Hogan", "Downer", "Fletcher Construction",
                                   "McConnell Dowell", "HEB Construction"],
            "policy_drivers": ["GPS-Transport 2024", "National Infrastructure Plan 2025",
                               "NPS-Infrastructure 2025", "Budget 2026 $60B programme"],
            "risk_factors": ["Supply chain pressure from Hormuz oil shock", "Skilled labour shortages",
                             "Inflation in materials costs", "Consent delays (improving)"],
            "opportunity_factors": ["$185B funded pipeline", "$400M state highway resilience",
                                    "KiwiRail $1.075B 2027-2030", "NPS-Infrastructure reduces consent delays"],
        },
        {
            "sector": "ICT",
            "government_spend_annual": 2_500_000_000,
            "pipeline_value": None,
            "top_agencies": ["DIA", "MBIE", "Health NZ", "ACC", "Inland Revenue"],
            "dominant_suppliers": ["Fujitsu NZ", "Datacom", "Spark", "Unison", "Microsoft NZ",
                                   "IBM NZ", "Deloitte Digital"],
            "policy_drivers": ["NZCSS-2026 cyber uplift mandate", "NCSC Baseline Standards Oct 2025",
                               "Digital Strategy for Aotearoa", "AoG ICT contracts", "Health NZ $153.6M cyber"],
            "risk_factors": ["AoG panel lock-in limits open competition", "Rapid threat landscape evolution",
                             "Legacy system debt across agencies"],
            "opportunity_factors": ["Mandatory cyber uplift across all agencies", "Te Au Reka digital courts",
                                    "NZIAT AI and advanced tech contracting", "Health NZ ICT uplift"],
        },
        {
            "sector": "FM",
            "government_spend_annual": 1_200_000_000,
            "pipeline_value": None,
            "top_agencies": ["Health NZ", "Ministry of Education / NZSPA", "Corrections",
                             "NZ Defence Force"],
            "dominant_suppliers": ["Programmed", "ISS", "OCS", "Cushman & Wakefield",
                                   "BGIS", "Spotless"],
            "policy_drivers": ["NZSPA $33.5B school portfolio launch mid-2026",
                               "Corrections $500M capital programme", "GPR5 economic benefit weighting"],
            "risk_factors": ["Living wage obligations increasing contract costs",
                             "Panel renewals concentrated in 2026-27"],
            "opportunity_factors": ["NZSPA new agency = new panel structure opportunity",
                                    "Health hospital build programme FM tail", "Corrections ongoing FM"],
        },
        {
            "sector": "defence",
            "government_spend_annual": 3_500_000_000,
            "pipeline_value": 12_000_000_000,
            "top_agencies": ["Ministry of Defence", "NZ Defence Force", "GCSB / NCSC"],
            "dominant_suppliers": ["BAE Systems NZ", "Lockheed Martin", "Raytheon NZ",
                                   "Leidos", "L3 Harris"],
            "policy_drivers": ["DCP2025 $12B over 4 years", "NZDIS Thin Prime model",
                               "ANZAC integration commitments", "2% GDP target"],
            "risk_factors": ["NZ Industry Capability Plan requirements for primes",
                             "Long procurement cycles (5-10 years)", "ITAR/export control complexity"],
            "opportunity_factors": ["$9B new spending announced", "Space capabilities SIB priority",
                                    "Uncrewed systems programme", "Sustainment contracts"],
        },
        {
            "sector": "Construction",
            "government_spend_annual": 8_000_000_000,
            "pipeline_value": 65_400_000_000,
            "top_agencies": ["Waka Kotahi NZTA", "Ministry of Education / NZSPA", "Health NZ",
                             "Corrections", "Ministry of Justice"],
            "dominant_suppliers": ["Fulton Hogan", "Fletcher Construction", "Downer",
                                   "McConnell Dowell", "Hawkins"],
            "policy_drivers": ["Budget 2026 $60B over 4 years", "NCPR-2025 6-year projection",
                               "NPS-Infrastructure consent streamlining"],
            "risk_factors": ["Materials cost inflation", "Labour shortages", "Oil price impact on plant costs"],
            "opportunity_factors": ["Justice $100M courthouses Rotorua", "Health hospital builds",
                                    "NZSPA school property portfolio", "Corrections capital works"],
        },
    ]

    for p in profiles:
        existing = db.fetchone(
            "SELECT id FROM intel_sector_profiles WHERE sector = %s",
            (p["sector"],),
        )
        if existing:
            logger.info("Sector profile exists: %s", p["sector"])
            continue
        db.execute(
            """
            INSERT INTO intel_sector_profiles (
                sector, government_spend_annual, pipeline_value,
                top_agencies, dominant_suppliers, policy_drivers,
                risk_factors, opportunity_factors
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                p["sector"],
                p.get("government_spend_annual"),
                p.get("pipeline_value"),
                p.get("top_agencies", []),
                p.get("dominant_suppliers", []),
                p.get("policy_drivers", []),
                p.get("risk_factors", []),
                p.get("opportunity_factors", []),
            ),
        )
        logger.info("Inserted sector profile: %s", p["sector"])


def run_seed() -> None:
    """Run full seed sequence."""
    logger.info("=== Intel Library Seed ===")
    logger.info("Seeding categories...")
    cat_id_map = _seed_categories()
    logger.info("Seeding sources...")
    n = _seed_sources(cat_id_map)
    logger.info("Seeding sector profiles...")
    seed_initial_sector_profiles()
    logger.info(
        "Seed complete. %d categories, %d new sources inserted.",
        len(cat_id_map), n,
    )


if __name__ == "__main__":
    run_seed()
