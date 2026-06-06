# Procint — Command Reference & Schedule

## Automated Schedule (cron)

All jobs run via `crontab` on the local machine. Logs go to `logs/scheduler.log` and `logs/cron.log`.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIME          FREQUENCY    JOB                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  06:00         Daily        Layer 1 — GETS ingest → watchlist HTML          │
│  07:00         Daily        Layer 2 — Award scraping + Market Intelligence  │
│  07:30         Daily        Layer 3 — Pursuit packages for active clients   │
│  08:00         Monday       Weekly watch brief generation + email           │
│  05:00         1st/month    MBIE open data refresh check + re-ingest        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### View active cron jobs
```bash
crontab -l
```

### Edit cron jobs
```bash
crontab -e
```

### View live log output
```bash
tail -f logs/scheduler.log
tail -f logs/cron.log
```

---

## Manual Job Execution

Run any scheduled job immediately on demand:

```bash
# Layer 1 — full GETS ingest, score, enrich, bidder inference, watchlist
python3 scheduler.py --run-now layer1

# Layer 2 — award scraping, org update, pattern detection, inject MI into watchlist
python3 scheduler.py --run-now layer2

# Layer 3 — pursuit packages for all L3_CLIENTS in .env
python3 scheduler.py --run-now layer3

# Weekly watch brief — generate + email to BRIEFING_RECIPIENTS
python3 scheduler.py --run-now brief

# MBIE refresh — check for updated CSVs, download if changed, rebuild win history
python3 scheduler.py --run-now mbie-refresh
```

---

## Pipeline Commands

### Layer 1 — Procurement notice ingestion
```bash
# Full pipeline (ingest → parse → score → enrich → bidders → watchlist)
python3 run_pipeline.py

# Skip ingestion (re-process existing notices)
python3 run_pipeline.py --skip-ingestion

# Skip Claude enrichment (faster, no AI calls)
python3 run_pipeline.py --skip-enrichment

# Skip enrichment and bidder inference
python3 run_pipeline.py --skip-enrichment --skip-bidders

# Full pipeline + Layer 2 + Layer 3 in one command
python3 run_pipeline.py --layer2 --layer3 --l3-client "Downer NZ" --l3-top 3
```

### Layer 2 — Intelligence synthesis
```bash
# Full Layer 2 (org seeding, award scraping, profiling, patterns)
python3 layer2_pipeline.py

# Skip GETS award scraping (use existing contract_awards data)
python3 layer2_pipeline.py --skip-awards

# Skip Claude agency profile generation (faster, no AI calls)
python3 layer2_pipeline.py --skip-profiles

# With competitor intelligence for a specific firm
python3 layer2_pipeline.py --company "Downer NZ"
```

### Layer 3 — Executive artefacts
```bash
# Pursuit intelligence package for a notice + client
python3 layer3_pipeline.py --pursuit 34060392 --client "Downer NZ"

# Demo/cold-outreach package (HTML + PDF)
python3 layer3_pipeline.py --demo 33731454 --client "Prospect Co"

# Alternatively, call directly:
python3 pursuit_package.py 34060392 "Downer NZ"
python3 demo_package.py 33731454 "Prospect Co"

# Weekly watch brief for a client
python3 layer3_pipeline.py --brief --client "Downer NZ"
python3 watch_brief.py "Downer NZ" --sectors infrastructure,FM

# Competitor profile report
python3 layer3_pipeline.py --competitor "Fulton Hogan" --client "Downer NZ"
python3 competitor_profile.py "Fulton Hogan" --client "Downer NZ"

# Pursuit packages for top 5 notices
python3 layer3_pipeline.py --all-pursuits --client "Downer NZ" --top 5
```

### MBIE data management
```bash
# Check for updated MBIE files without downloading
python3 refresh_mbie.py --dry-run

# Force re-download and re-ingest all MBIE files
python3 refresh_mbie.py --force

# Initial MBIE data load (run once on first setup)
python3 historical_data.py
```

### Client portal
```bash
# Start the Flask portal (http://127.0.0.1:5000)
python3 portal.py

# On a server (all interfaces)
PORTAL_HOST=0.0.0.0 python3 portal.py
```

---

## Setup & First-Time Installation

```bash
# 1. Install Python dependencies
pip3 install -r requirements.txt

# 2. Install Playwright browser (for GETS scraping fallback)
playwright install chromium

# 3. Install PDF generation system dependency
brew install pango

# 4. Copy and configure environment
cp .env.example .env
# Edit .env — set DATABASE_URL, ANTHROPIC_API_KEY, SMTP_*, L3_CLIENTS

# 5. Apply database schema
psql $DATABASE_URL -f schema.sql

# 6. Apply Layer 2 migrations (if upgrading from Layer 1 only)
psql $DATABASE_URL -f migrations/001_bidder_pool_enrichment.sql
psql $DATABASE_URL -f migrations/002_layer2_knowledge_graph.sql
psql $DATABASE_URL -f migrations/003_mbie_historical_data.sql

# 7. Load MBIE historical data (27,948 award notices, 2014–2025)
python3 historical_data.py

# 8. Run Layer 1 to populate notices
python3 run_pipeline.py

# 9. Seed the knowledge graph from Layer 1 data
python3 layer2_pipeline.py --skip-awards --skip-profiles

# 10. Install cron schedule
#   Run the cron installer:
PROJ=$(pwd)
PY=/usr/bin/python3
(crontab -l 2>/dev/null | grep -v "scheduler.py"; cat << EOF
# Procint — automated pipeline schedule
0 6 * * * cd $PROJ && $PY scheduler.py --run-now layer1 >> $PROJ/logs/cron.log 2>&1
0 7 * * * cd $PROJ && $PY scheduler.py --run-now layer2 >> $PROJ/logs/cron.log 2>&1
30 7 * * * cd $PROJ && $PY scheduler.py --run-now layer3 >> $PROJ/logs/cron.log 2>&1
0 8 * * 1 cd $PROJ && $PY scheduler.py --run-now brief >> $PROJ/logs/cron.log 2>&1
0 5 1 * * cd $PROJ && $PY scheduler.py --run-now mbie-refresh >> $PROJ/logs/cron.log 2>&1
EOF
) | crontab -
```

---

## Output Locations

| Artefact | Location |
|---|---|
| Daily watchlist HTML | `output/watchlist_YYYY-MM-DD.html` |
| Daily watchlist JSON | `output/watchlist_YYYY-MM-DD.json` |
| Pursuit packages | `output/artefacts/{client}/{date}/{notice_id}_pursuit_package.html` |
| Demo packages | `output/artefacts/DEMO_{prospect}/{date}/DEMO_{prospect}_{notice_id}.html/.pdf` |
| Watch briefs | `output/artefacts/{client}/{date}/watch_brief_{date}.html` |
| Competitor profiles | `output/artefacts/{client}/{date}/competitor_{name}.html` |
| Scheduler log | `logs/scheduler.log` |
| Cron output log | `logs/cron.log` |
| MBIE data files | `data/mbie/*.csv` |
| MBIE file metadata | `data/mbie/metadata.json` |

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✓ | PostgreSQL connection string |
| `ANTHROPIC_API_KEY` | ✓ | Anthropic API key for Claude enrichment |
| `SMTP_HOST` | For email | SMTP server hostname |
| `SMTP_PORT` | For email | SMTP port (587 for TLS) |
| `SMTP_USER` | For email | SMTP username |
| `SMTP_PASSWORD` | For email | SMTP password / app password |
| `SMTP_FROM` | For email | From address with display name |
| `BRIEFING_RECIPIENTS` | For email | Comma-separated email addresses |
| `ADMIN_EMAIL` | For alerts | Failure alert destination |
| `L3_CLIENTS` | For Layer 3 | Comma-separated client firm names |
| `L3_SECTORS` | Optional | Sector filter for watch briefs |
| `L3_TOP` | Optional | Notices per client per run (default 3) |
| `PORTAL_PASSWORD` | For portal | Shared portal access password |
| `PRIORITY_THRESHOLD` | Optional | Min score for enrichment (default 5.0) |
| `WATCHLIST_THRESHOLD` | Optional | Min score for watchlist (default 4.0) |
| `LOG_LEVEL` | Optional | Logging level (default INFO) |
