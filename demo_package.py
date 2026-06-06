"""
Layer 3 — Demo Package Generator (business development tool).

Generates a branded sample pursuit intelligence package for a prospective
client. Wraps pursuit_package.py with:
  - A cover page: "Prepared for [Company] — Sample Intelligence Assessment"
  - A footer on every section: "This is a sample produced by [DEMO_FIRM_NAME]"
  - PDF output via pdfkit (falls back to HTML-only if wkhtmltopdf not installed)

Usage:
  python demo_package.py <notice_id> "<Prospect Company>" [--output-dir path]

This is the cold-outreach artefact — sent to prospective clients to
demonstrate the depth and format of the intelligence product.
"""
import argparse
import logging
import shutil
from datetime import date
from pathlib import Path
from typing import Optional

import config
from pursuit_package import generate_pursuit_package, _artefact_dir, _slug

logger = logging.getLogger(__name__)

# ── PDF generation ─────────────────────────────────────────────────────────────

_PDF_PRINT_CSS = """
@page {
    size: A4;
    margin: 18mm 18mm 22mm 18mm;
    @bottom-center {
        content: counter(page) " / " counter(pages);
        font-size: 8pt;
        color: #7d8fa8;
        font-family: 'Inter', system-ui, sans-serif;
    }
}

/* ── Reset flex layout for print ── */
body {
    display: block !important;
    padding: 0 !important;
    background: #0d1117 !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
}

/* Hide screen-only chrome */
.sidebar           { display: none !important; }
.demo-footer-bar   { display: none !important; }

/* Main content fills the full page */
.main {
    display: block !important;
    padding: 0 !important;
    max-width: 100% !important;
    width: 100% !important;
}

/* Suppress sticky/fixed positioning */
.card-header, .cover { position: static !important; }

/* SAMPLE watermark — subtle diagonal text via a CSS counter trick */
/* weasyprint does not support fixed/absolute pseudo-elements reliably,
   so we render it as a full-width centred block at the top of each page */
body::before {
    content: "SAMPLE";
    display: block;
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%) rotate(-35deg);
    font-size: 100px;
    font-weight: 900;
    color: rgba(250, 204, 21, 0.06);
    letter-spacing: 0.2em;
    white-space: nowrap;
    pointer-events: none;
}

/* Improve readability in print */
.prose p, .summary-text, .pos-card-detail,
.action-text, .flag-item, .bidder-context,
.bidder-bullet, .opp-summary {
    font-size: 10pt;
    line-height: 1.65;
}

.section-title    { font-size: 12pt; }
.section-number   { font-size: 9pt; }
.card-title       { font-size: 11pt; }
.cover-title      { font-size: 18pt; }
.score-number     { font-size: 20pt; }
.meta-label, .section-label { font-size: 8pt; }

/* Avoid breaking sections across pages */
.section          { page-break-inside: avoid; }
.pos-card         { page-break-inside: avoid; }
.action-item      { page-break-inside: avoid; }
.verdict          { page-break-inside: avoid; }
table             { page-break-inside: avoid; }
tr                { page-break-inside: avoid; }

/* Links */
a { color: #4f9cf9; text-decoration: none; }
"""


def _html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """
    Convert HTML to PDF via weasyprint.
    Injects print-optimised CSS directly into the HTML before rendering
    to correctly override the flex layout and hide screen-only chrome.
    Install: pip install weasyprint && brew install pango
    """
    try:
        from weasyprint import HTML, CSS  # type: ignore
        from weasyprint.text.fonts import FontConfiguration  # type: ignore
    except ImportError:
        logger.warning("weasyprint not installed — skipping PDF. pip install weasyprint")
        return False

    # Read HTML and inject print CSS into <head> so it takes precedence
    html_content = html_path.read_text(encoding="utf-8")
    print_style_tag = f"<style>{_PDF_PRINT_CSS}</style>\n</head>"
    html_content = html_content.replace("</head>", print_style_tag, 1)

    # Write a temporary PDF-optimised HTML file
    tmp_path = pdf_path.with_suffix(".pdf_tmp.html")
    tmp_path.write_text(html_content, encoding="utf-8")

    try:
        font_config = FontConfiguration()
        html_doc = HTML(filename=str(tmp_path))
        html_doc.write_pdf(
            target=str(pdf_path),
            font_config=font_config,
        )
        logger.info("PDF generated: %s (%s)", pdf_path, _human_size(pdf_path.stat().st_size))
        return True
    except Exception as exc:
        logger.warning("weasyprint conversion failed: %s", exc)
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n //= 1024
    return f"{n:.0f}MB"


# ── Demo branding ──────────────────────────────────────────────────────────────

def _inject_demo_styles(html: str, prospect_name: str) -> str:
    """
    Inject demo-specific CSS and footer into the HTML document.
    - Adds a "SAMPLE" diagonal watermark (CSS only)
    - Adds a sticky footer bar on every page
    - Adds a print-friendly media query
    """
    contact = config.DEMO_CONTACT_EMAIL or config.DEMO_WEBSITE or "contact us"
    firm = config.DEMO_FIRM_NAME
    phone = config.DEMO_CONTACT_PHONE

    footer_contact = f"{firm}"
    if config.DEMO_CONTACT_EMAIL:
        footer_contact += f" &nbsp;&middot;&nbsp; {config.DEMO_CONTACT_EMAIL}"
    if phone:
        footer_contact += f" &nbsp;&middot;&nbsp; {phone}"
    if config.DEMO_WEBSITE:
        footer_contact += f' &nbsp;&middot;&nbsp; <a href="{config.DEMO_WEBSITE}" style="color:inherit;">{config.DEMO_WEBSITE}</a>'

    demo_css = """
    /* Demo watermark */
    body::after {
        content: 'SAMPLE';
        position: fixed;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%) rotate(-35deg);
        font-size: 120px;
        font-weight: 900;
        color: rgba(250, 204, 21, 0.04);
        pointer-events: none;
        z-index: 9999;
        letter-spacing: .2em;
        white-space: nowrap;
    }
    /* Demo footer bar */
    .demo-footer-bar {
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        background: rgba(13, 17, 23, 0.95);
        border-top: 1px solid #2a3344;
        padding: .4rem 1.5rem;
        font-size: .68rem;
        color: #7d8fa8;
        display: flex;
        justify-content: space-between;
        align-items: center;
        z-index: 1000;
        backdrop-filter: blur(4px);
    }
    .demo-footer-bar strong { color: #facc15; }
    /* Push main content above footer */
    .main { padding-bottom: 4rem; }
    @media print {
        .demo-footer-bar { position: fixed; bottom: 0; }
        body::after { display: block; }
        .sidebar { display: none; }
        .main { padding-left: 1.5rem; max-width: 100%; }
    }
    """

    demo_footer_html = f"""
    <div class="demo-footer-bar">
        <span><strong>SAMPLE DOCUMENT</strong> &nbsp;&mdash;&nbsp;
        Prepared for <strong>{prospect_name}</strong> to demonstrate the Procurement Intelligence Platform.</span>
        <span>{footer_contact}</span>
    </div>
    """

    # Inject CSS into existing <style> block
    html = html.replace("</style>", demo_css + "\n    </style>", 1)

    # Inject footer bar before </body>
    html = html.replace("</body>", demo_footer_html + "\n</body>", 1)

    return html


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_demo_package(
    notice_id: str,
    prospect_name: str,
    output_dir: Optional[Path] = None,
    generate_pdf: bool = True,
) -> dict[str, Optional[Path]]:
    """
    Generate a branded demo pursuit intelligence package.

    Returns dict with keys:
      html: Path to HTML file
      pdf:  Path to PDF file (None if generation failed or skipped)
    """
    logger.info(
        "Generating demo package: notice=%s prospect=%s",
        notice_id, prospect_name,
    )

    watermark_text = (
        f"This is a sample produced by {config.DEMO_FIRM_NAME}. "
        "Contact us to discuss how we support your procurement pursuits."
    )

    # 1. Generate the core pursuit package with demo flag
    if output_dir is None:
        output_dir = _artefact_dir(f"DEMO_{prospect_name}")

    html_path = generate_pursuit_package(
        notice_id=notice_id,
        client_name=prospect_name,
        output_dir=output_dir,
        is_demo=True,
        demo_watermark=watermark_text,
    )

    # 2. Inject demo branding into the HTML
    html_content = html_path.read_text(encoding="utf-8")
    html_content = _inject_demo_styles(html_content, prospect_name)

    # Rename to demo filename
    demo_html_path = output_dir / f"DEMO_{_slug(prospect_name)}_{notice_id}.html"
    demo_html_path.write_text(html_content, encoding="utf-8")

    # Remove the intermediate file
    if html_path != demo_html_path:
        html_path.unlink(missing_ok=True)

    logger.info("Demo HTML written to %s", demo_html_path)

    # 3. Generate PDF
    pdf_path = None
    if generate_pdf:
        pdf_dest = demo_html_path.with_suffix(".pdf")
        success = _html_to_pdf(demo_html_path, pdf_dest)
        if success:
            pdf_path = pdf_dest

    return {"html": demo_html_path, "pdf": pdf_path}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    p = argparse.ArgumentParser(description="Generate a demo pursuit intelligence package")
    p.add_argument("notice_id", help="GETS notice ID to use as demonstration")
    p.add_argument("prospect_name", help="Prospect company name")
    p.add_argument("--output-dir", help="Output directory (optional)")
    p.add_argument("--no-pdf", action="store_true", help="Skip PDF generation")
    args = p.parse_args()

    result = generate_demo_package(
        notice_id=args.notice_id,
        prospect_name=args.prospect_name,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        generate_pdf=not args.no_pdf,
    )

    print(f"HTML: {result['html']}")
    if result["pdf"]:
        print(f"PDF:  {result['pdf']}")
    else:
        print("PDF:  Not generated (install wkhtmltopdf: brew install wkhtmltopdf)")
