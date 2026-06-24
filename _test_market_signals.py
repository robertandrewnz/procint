"""
Diagnostic: test market signal generation end-to-end for a given user.
Prints full tracebacks — does not swallow exceptions.

Usage:
    railway run python3 _test_market_signals.py
    railway run python3 _test_market_signals.py admin
"""
import logging
import sys
import traceback

# Configure logging at DEBUG before any project imports so every logger is visible
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(name)s — %(message)s",
    stream=sys.stderr,
)

USER_ID = sys.argv[1] if len(sys.argv) > 1 else "robert"

DIV = "\n" + "=" * 72

print(DIV)
print(f"Market Signals Test — user_id={USER_ID!r}")

# ── Step 1: config sanity ─────────────────────────────────────────────────────
print("\n[1] Config:")
try:
    import config
    print(f"    CLAUDE_MODEL       = {config.CLAUDE_MODEL!r}")
    print(f"    ANTHROPIC_API_KEY  = {'set (' + str(len(config.ANTHROPIC_API_KEY)) + ' chars)' if getattr(config, 'ANTHROPIC_API_KEY', None) else 'MISSING'}")
except Exception:
    print("    FAILED to import config:")
    traceback.print_exc()
    sys.exit(1)

# ── Step 2: user preferences ──────────────────────────────────────────────────
print("\n[2] User preferences:")
sectors = []
try:
    from preferences import get_user_preferences
    prefs = get_user_preferences(USER_ID)
    sectors = prefs.get("sectors") or []
    print(f"    sectors: {sectors}")
    print(f"    min_value: {prefs.get('min_value_nzd')}")
except Exception:
    print("    FAILED:")
    traceback.print_exc()

# ── Step 3: context builders ──────────────────────────────────────────────────
print("\n[3] Context builders:")
notices = awards = renewals = []
try:
    from market_intelligence import (
        _recent_notices_summary,
        _recent_awards_summary,
        _renewal_summary,
        _build_prompt,
        _SIGNAL_TOOL,
        _SYSTEM_PROMPT,
    )
    notices  = _recent_notices_summary(sectors)
    awards   = _recent_awards_summary(sectors)
    renewals = _renewal_summary(sectors)
    print(f"    notices:  {len(notices)} rows")
    print(f"    awards:   {len(awards)} rows")
    print(f"    renewals: {len(renewals)} rows")
except Exception:
    print("    FAILED:")
    traceback.print_exc()

# ── Step 4: direct Claude tool_use call (no exception swallowing) ─────────────
print("\n[4] Direct Claude tool_use call:")
try:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = _build_prompt(USER_ID, sectors, notices, awards, renewals)

    print(f"    Sending request — model={config.CLAUDE_MODEL!r} tool={_SIGNAL_TOOL['name']!r}")
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=800,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        tools=[_SIGNAL_TOOL],
        tool_choice={"type": "tool", "name": "report_market_signals"},
    )
    print(f"    stop_reason: {resp.stop_reason}")
    print(f"    content blocks ({len(resp.content)} total):")
    for i, block in enumerate(resp.content):
        btype = getattr(block, "type", "?")
        print(f"      [{i}] type={btype!r}" +
              (f" name={block.name!r}" if btype == "tool_use" else ""))

    tool_block = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"), None
    )
    if tool_block:
        raw_signals = tool_block.input.get("signals", [])
        print(f"    signals in tool_use block: {len(raw_signals)}")
        for s in raw_signals:
            print(f"      [{s.get('priority','?')}] {str(s.get('signal',''))[:90]}")
    else:
        print("    ERROR: no tool_use block found in response")
        for block in resp.content:
            print(f"      raw block: {block}")

except Exception:
    print("    FAILED — full traceback:")
    traceback.print_exc()

# ── Step 5: full generate_market_intelligence() with its own exception handling ─
print("\n[5] generate_market_intelligence() (via module, with its own catch):")
try:
    from market_intelligence import generate_market_intelligence
    result = generate_market_intelligence(USER_ID)
    if result:
        print(f"    SUCCESS — {len(result)} signal(s) returned and stored:")
        for s in result:
            print(f"      [{s.get('priority','?')}] {str(s.get('signal',''))[:90]}")
    else:
        print("    RETURNED [] — check stderr above for the logged error")
        print("    (The except at line ~238 caught something — look for ERROR lines above)")
except Exception:
    print("    RAISED (escaped the module's own try/except — unexpected):")
    traceback.print_exc()

# ── Step 6: current market_signals table state ────────────────────────────────
print("\n[6] Current market_signals rows for this user (all time, last 5):")
try:
    import db
    rows = db.fetchall(
        """
        SELECT id, priority, generated_at,
               generated_at::date AS date_utc,
               LEFT(signal, 70)   AS signal_preview
          FROM market_signals
         WHERE user_id = %s
         ORDER BY generated_at DESC
         LIMIT 5
        """,
        (USER_ID,),
    )
    if rows:
        for r in rows:
            print(f"    id={r['id']} date_utc={r['date_utc']} [{r['priority']}] {r['signal_preview']}")
    else:
        print("    No rows found.")
except Exception:
    print("    FAILED:")
    traceback.print_exc()

print(DIV + "\nDone.\n")
