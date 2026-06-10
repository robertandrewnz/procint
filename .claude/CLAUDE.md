# Groundwork by BidEdge — Developer Context

## Product
Procurement intelligence SaaS for NZ government 
tenders. Live at bidedge.co.nz. Not yet at first 
paid client. Pre-sales polish phase.

## Firm
BidEdge is the umbrella firm with three offerings:
- Groundwork: SaaS procurement intelligence
- Terrain: Fixed-price market opportunity scans
- Keystone: Executive decision support packs

## Stack
Python 3.12 (Railway) / 3.9 (local), Flask + 
Gunicorn, PostgreSQL via Supabase (Singapore, 
transaction pooler, port 6543), Claude API 
(claude-sonnet-4-20250514), APScheduler, 
Railway hosting, Cloudflare DNS.

## Repo
github.com/robertandrewnz/procint
Local path: ~/Documents/GitHub/Procint
Deploy branch: main (Railway watches this)
CRITICAL: Always commit directly to main and 
push immediately. Never create PRs or branches.
Never merge develop into main wholesale — 
they have diverged 72+ commits. Cherry-pick only.

## Architecture
Layer 1: Daily GETS scraper (276+ active NZ 
         government tenders), sector classifier, 
         composite scoring, Claude enrichment, 
         ACH bidder inference
Layer 2: 27,948 MBIE historical awards, supplier 
         win profiles, market intelligence signals
Layer 3: Pursuit packages, competitor profiles, 
         watch briefs, demo artefacts, Flask portal

## Key files
portal.py — all Flask routes
demo_package.py — demo generation
pursuit_package.py — pursuit package generation
competitor_profile.py — competitor profile generation
watch_brief.py — watch brief generation
_phase2_targeted_regen.py — targeted demo regen 
                             (run via railway CLI)

## Database rules
Always use transaction pooler URL, port 6543.
Never use session pooler or direct connection.
Always ADD COLUMN IF NOT EXISTS.
All artefact HTML must be stored in html_content 
column in DB as well as written to disk.

## Railway / filesystem
Railway filesystem is ephemeral — never rely on 
disk for anything that must survive redeploy.
Railway Volume is not yet configured for /app/output.
To run scripts against production DB use:
railway run python3 script_name.py
(from ~/Documents/GitHub/Procint with railway 
linked to comfortable-nurturing project)

## Branding
Firm: BidEdge
Product: Groundwork by BidEdge
Colours: Navy #1E2D40, Teal #2A9D8F
Logo: inline SVG in nav (bidedge-nav.svg)
Taglines:
- BidEdge: "Most organisations act on incomplete 
  intelligence. Know before you bid. Know before 
  you enter. Know before you decide."
- Groundwork: "Know before you bid. Win when you do."
- Terrain: "Know the ground before you move."
- Keystone: "Every signal. One decision agenda."

## Pricing
Groundwork: Watch $4,900/yr, Pursue $9,900/yr, 
            Edge custom
Terrain: $6,500 + GST fixed price, 10 business days
Keystone: From $8,500 + GST, retainer options 
          available

## Demo artefacts
7 sectors with fictional firms:
FM → Cityworks NZ
Cybersecurity → Sentinel Digital (competitor: Datacom)
Construction → Meridian Civil (competitor: Fletcher)
Defence → Apex Engineering (competitor: Nova Systems)
ICT → Korepath Systems
Infrastructure → Southern Civil Group (competitor: Downer)
Health → MedTech Solutions NZ (competitor: F&P Healthcare)

Demo rules:
- Each sector must use sector-matched notices only
- Win position must be Competitive or Conditional Go
- Never generate demos via HTTP admin routes
- Call generation functions directly or via 
  railway run python3

## Win position bands
Strong / Competitive / Conditional Go / 
Challenging / Not Recommended
Never show Challenging or Not Recommended 
in a demo artefact.

## Critical principles
- Confident wrong results are worse than no result
- Demo artefacts must show correct-sector content
- Wrong sector content kills a sales conversation
- Never create PRs or branches — commit to main
- Always run pre-work review before any changes
- Always verify after changes before committing
- Never run generation scripts via HTTP requests
