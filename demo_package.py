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

def _html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """
    Convert HTML to PDF via pdfkit (requires wkhtmltopdf installed).
    Returns True on success, False if pdfkit/wkhtmltopdf not available.
    Install: brew install wkhtmltopdf
    """
    try:
        import pdfkit  # type: ignore
    except ImportError:
        logger.warning("pdfkit not installed — skipping PDF generation. pip install pdfkit")
        return False

    if not shutil.which("wkhtmltopdf"):
        logger.warning(
            "wkhtmltopdf not found — skipping PDF generation. "
            "Install with: brew install wkhtmltopdf"
        )
        return False

    options = {
        "page-size": "A4",
        "margin-top": "12mm",
        "margin-bottom": "16mm",
        "margin-left": "10mm",
        "margin-right": "10mm",
        "encoding": "UTF-8",
        "enable-local-file-access": "",
        "quiet": "",
        "print-media-type": "",
        "no-outline": "",
        # Sidebar doesn't translate well to PDF — use a print-optimised layout
        "user-style-sheet": "",
    }

    try:
        pdfkit.from_file(str(html_path), str(pdf_path), options=options)
        logger.info("PDF generated: %s", pdf_path)
        return True
    except Exception as exc:
        logger.warning("pdfkit conversion failed: %s", exc)
        return False


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
