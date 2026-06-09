"""
Layer 3 — Demo Package Generator (business development tool).

Generates a branded sample pursuit intelligence package for a prospective
client. Wraps pursuit_package.py with:
  - A cover page: "Prepared for [Company] — Sample Intelligence Assessment"
  - A footer on every section: "This is a sample produced by [DEMO_FIRM_NAME]"
  - PDF output via weasyprint (pip install weasyprint && brew install pango)

Usage:
  python demo_package.py <notice_id> "<Prospect Company>" [--output-dir path]

This is the cold-outreach artefact — sent to prospective clients to
demonstrate the depth and format of the intelligence product.
"""
import argparse
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import config
from pursuit_package import generate_pursuit_package, _artefact_dir, _slug

logger = logging.getLogger(__name__)

# ── Print CSS injected into HTML before weasyprint renders ────────────────────
# Injected directly into <head> so it wins the CSS cascade over the
# document stylesheet's body { display: flex } layout.

_PDF_PRINT_CSS = """
@page {
    size: A4;
    margin: 14mm 14mm 18mm 14mm;
    @bottom-center {
        content: counter(page) " / " counter(pages);
        font-size: 8pt;
        color: #7d8fa8;
        font-family: 'Inter', system-ui, sans-serif;
    }
}

/* ── Weasyprint has incomplete flex support — convert ALL flex containers ── */

body {
    display: block !important;
    padding: 0 !important;
    background: #f5f6f8 !important;
    color: #2c3e50 !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
}

/* Hide screen-only elements */
.sidebar, .demo-footer-bar { display: none !important; }

/* Main content: full width with internal padding so text clears box edges */
.main {
    display: block !important;
    padding: 20pt 16pt 12pt !important;
    max-width: 100% !important;
    width: 100% !important;
    box-sizing: border-box !important;
}

/* Cover: give it room to breathe */
.cover { position: static !important; padding: 0 0 16pt; }
.cover-meta { display: block !important; margin-bottom: 8pt; }
.meta-chip { display: inline-block; margin-right: 6pt; margin-bottom: 4pt; }

/* Verdict banner — was flex, now block with inline score */
.verdict {
    display: block !important;
    padding: 16pt 16pt 14pt !important;
    margin-top: 12pt;
    margin-bottom: 16pt;
}
.verdict .prob-ring {
    display: inline-block;
    margin-right: 14pt;
    vertical-align: middle;
    text-align: center;
    min-width: 60pt;
}
.verdict .verdict-badge {
    display: inline-block;
    margin-right: 14pt;
    vertical-align: middle;
    font-size: 18pt;
    font-weight: 800;
}
.verdict .verdict-text {
    display: block;
    margin-top: 8pt;
    font-size: 10pt;
    line-height: 1.6;
}
/* Divider line in verdict — hide in PDF */
.verdict > div[style*="width:1px"] { display: none !important; }

/* Card header */
.card-header {
    display: block !important;
    position: static !important;
    padding: 16pt 16pt 12pt !important;
}
.card-header-main { display: block; margin-bottom: 6pt; }
.card-title-row   { display: block !important; margin-bottom: 4pt; }
.score-block      { display: block; text-align: left; margin-top: 6pt; }
.score-bar-track  { width: 120pt; }

/* Meta row — was flex, now inline-block cells */
.card-meta-row { display: block !important; padding: 8pt 16pt; }
.meta-item {
    display: inline-block !important;
    padding: 0 16pt 0 0 !important;
    border-right: none !important;
    vertical-align: top;
    margin-bottom: 4pt;
}

/* Card body — was 3-column flex, now stacked with padding */
.card-body    { display: block !important; padding: 0 16pt 8pt !important; }
.col-intel    { display: block !important; width: 100% !important;
                border-right: none !important; padding: 12pt 0 12pt !important; }
.col-actions  { display: block !important; width: 100% !important;
                border-right: none !important; padding: 12pt 0 !important;
                border-top: 1pt solid #2a3344; }
.col-bidders  { display: block !important; width: 100% !important;
                padding: 12pt 0 0 !important;
                border-top: 1pt solid #2a3344; }

/* Action items */
.action-item  { display: block !important; padding: 6pt 0; }
.action-num   { display: inline-block; margin-right: 6pt; vertical-align: top; }
.action-body  { display: inline-block; vertical-align: top; width: 90%; }

/* Bidder cards */
.bidder-card   { margin-bottom: 8pt; }
.bidder-header { display: block !important; }
.bidder-name   { display: block; font-weight: 700; font-size: 10pt; }
.bidder-meta, .bidder-pill { display: inline-block; margin-right: 6pt;
                              font-size: 8pt; }

/* Flags */
.flag-item { display: block !important; margin-bottom: 4pt; }
.flag-icon { display: inline; margin-right: 4pt; }

/* Suppressed positioning */
.card-header, .cover, .report-header { position: static !important; }

/* Section spacing */
.section { page-break-before: auto; margin-bottom: 16pt; padding: 16pt 16pt 0; }
.section-title { font-size: 13pt; margin-bottom: 8pt; }
.section-number { font-size: 9pt; }
.cover-title { font-size: 20pt; line-height: 1.3; }
.score-number { font-size: 22pt; }

/* Readable prose */
.prose p, .summary-text, .pos-card-detail, .action-text,
.flag-item, .bidder-context, .bidder-bullet {
    font-size: 10pt; line-height: 1.65;
}

/* Page breaks */
.section, .pos-card, .action-item, .verdict,
table, tr { page-break-inside: avoid; }

a { color: #4f9cf9; text-decoration: none; }
"""


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n //= 1024
    return f"{n:.0f}MB"


# ── PDF generation ─────────────────────────────────────────────────────────────

def _html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """
    Convert HTML to PDF via weasyprint.
    Injects _PDF_PRINT_CSS directly into <head> so it overrides the flex
    layout and hides screen-only elements.
    Install: pip install weasyprint && brew install pango
    """
    try:
        from weasyprint import HTML  # type: ignore
        from weasyprint.text.fonts import FontConfiguration  # type: ignore
    except ImportError:
        logger.warning("weasyprint not installed — skipping PDF. pip install weasyprint")
        return False

    # Inject print CSS into a temp copy of the HTML
    html_content = html_path.read_text(encoding="utf-8")
    html_content = html_content.replace(
        "</head>",
        f"<style>{_PDF_PRINT_CSS}</style>\n</head>",
        1,
    )

    tmp_path = pdf_path.with_suffix(".pdf_tmp.html")
    tmp_path.write_text(html_content, encoding="utf-8")

    try:
        font_config = FontConfiguration()
        HTML(filename=str(tmp_path)).write_pdf(
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


# ── Demo branding ──────────────────────────────────────────────────────────────

def _inject_demo_styles(html: str, prospect_name: str) -> str:
    """Inject SAMPLE watermark and branded footer into the HTML (screen version)."""
    firm = config.DEMO_FIRM_NAME
    phone = config.DEMO_CONTACT_PHONE

    footer_contact = firm
    if config.DEMO_CONTACT_EMAIL:
        footer_contact += f" &nbsp;&middot;&nbsp; {config.DEMO_CONTACT_EMAIL}"
    if phone:
        footer_contact += f" &nbsp;&middot;&nbsp; {phone}"
    if config.DEMO_WEBSITE:
        footer_contact += (
            f' &nbsp;&middot;&nbsp; <a href="{config.DEMO_WEBSITE}" style="color:inherit;">'
            f'{config.DEMO_WEBSITE}</a>'
        )

    demo_css = """
    body::after {
        content: 'SAMPLE\A BIDEDGE';
        position: fixed;
        top: 50%; left: 50%;
        transform: translate(-50%, -50%) rotate(-35deg);
        font-size: 120px; font-weight: 900;
        color: rgba(250,204,21,0.04);
        pointer-events: none; z-index: 9999;
        letter-spacing: .2em; white-space: nowrap;
    }
    .demo-footer-bar {
        position: fixed; bottom: 0; left: 0; right: 0;
        background: rgba(26,45,74,0.97); border-top: 1px solid #2a9d8f;
        padding: .4rem 1.5rem; font-size: .68rem; color: rgba(255,255,255,0.8);
        display: flex; justify-content: space-between; align-items: center;
        z-index: 1000; font-family: 'Inter', system-ui, sans-serif;
    }
    .demo-footer-bar strong { color: #2a9d8f; }
    .main { padding-bottom: 4rem; }
    """

    demo_footer = (
        f'<div class="demo-footer-bar">'
        f'<span><strong>SAMPLE DOCUMENT</strong> &nbsp;&mdash;&nbsp; '
        f'Prepared for <strong>{prospect_name}</strong> to demonstrate the Procurement Intelligence Platform.</span>'
        f'<span>{footer_contact}</span>'
        f'</div>'
    )

    html = html.replace("</style>", demo_css + "\n    </style>", 1)
    html = html.replace("</body>", demo_footer + "\n</body>", 1)
    return html


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_demo_package(
    notice_id: str,
    prospect_name: str,
    output_dir: Optional[Path] = None,
    generate_pdf: bool = True,
    firm_profile: Optional[dict] = None,
) -> dict:
    """
    Generate a branded demo pursuit intelligence package.
    Returns {"html": Path, "pdf": Path or None}.
    """
    logger.info("Generating demo package: notice=%s prospect=%s", notice_id, prospect_name)

    watermark_text = (
        f"This is a sample produced by {config.DEMO_FIRM_NAME}. "
        "Contact us to discuss how we support your procurement pursuits."
    )

    if output_dir is None:
        output_dir = _artefact_dir(f"DEMO_{prospect_name}")

    html_path = generate_pursuit_package(
        notice_id=notice_id,
        client_name=prospect_name,
        output_dir=output_dir,
        is_demo=True,
        demo_watermark=watermark_text,
        firm_profile=firm_profile,
    )

    html_content = html_path.read_text(encoding="utf-8")
    html_content = _inject_demo_styles(html_content, prospect_name)

    html_filename = f"DEMO_{_slug(prospect_name)}_{notice_id}.html"
    demo_html_path = output_dir / html_filename
    demo_html_path.write_text(html_content, encoding="utf-8")

    if html_path != demo_html_path:
        html_path.unlink(missing_ok=True)

    logger.info("Demo HTML written to %s", demo_html_path)

    import storage as _storage
    import db as _db
    from datetime import date as _date
    run_date = _date.today()
    html_storage_path = f"demo/{html_filename}"
    if not _storage.upload_file(str(demo_html_path), html_storage_path, "text/html"):
        logger.warning("Storage upload failed for %s", html_filename)
    _db.save_output(
        "demo_html", run_date, html_filename,
        content=html_content, storage_path=html_storage_path,
        notice_id=notice_id,
    )

    pdf_path = None
    if generate_pdf:
        pdf_dest = demo_html_path.with_suffix(".pdf")
        if _html_to_pdf(demo_html_path, pdf_dest):
            pdf_path = pdf_dest
            pdf_filename = pdf_dest.name
            pdf_storage_path = f"demo/{pdf_filename}"
            with open(pdf_dest, "rb") as fh:
                pdf_bytes = fh.read()
            if not _storage.upload_file(str(pdf_dest), pdf_storage_path, "application/pdf"):
                logger.warning("Storage upload failed for %s", pdf_filename)
            _db.save_output(
                "demo_pdf", run_date, pdf_filename,
                content_bytes=pdf_bytes, storage_path=pdf_storage_path,
                notice_id=notice_id,
            )

    return {"html": demo_html_path, "pdf": pdf_path}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
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
        print("PDF:  Not generated (install weasyprint: pip install weasyprint && brew install pango)")
