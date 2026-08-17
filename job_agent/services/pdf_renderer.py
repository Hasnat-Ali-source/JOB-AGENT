"""
PDF Renderer (Phase 4).

Renders tailored resume / cover letter text to a PDF the user reviews and submits.

Two engines, both fed from the same structured blocks so output is consistent:

- **WeasyPrint** (preferred) — HTML/CSS layout. Real typographic control:
  letter-spaced section rules, controlled widow/orphan behavior, print margins.
  Needs system libraries (pango, cairo, gdk-pixbuf), installed via
  `brew install pango cairo gdk-pixbuf libffi`.
- **ReportLab** (fallback) — pure Python, no native dependencies. Used
  automatically wherever WeasyPrint can't load, so PDF export never becomes an
  environment-specific failure.

`settings.pdf_engine` pins a choice ("weasyprint" / "reportlab") or leaves it
"auto" to prefer WeasyPrint and degrade quietly.

Layout is single-column with standard fonts in both engines: ATS parsers handle
that far better than multi-column designs, and the user's own formatting is
preserved in the untouched master upload regardless.
"""

import html
import json
import logging
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from job_agent.config import settings
from job_agent.services.document_layout import Block, build_blocks

logger = logging.getLogger(__name__)

# A single page should render in well under a second; this is a safety net for
# a child that hangs rather than crashes.
WEASYPRINT_TIMEOUT = 60


@lru_cache(maxsize=1)
def weasyprint_available() -> bool:
    """
    Check whether WeasyPrint can actually render on this machine.

    Importing is not sufficient: without pango/cairo the failure surfaces as an
    OSError from the native loader. This performs one tiny real render and
    caches the answer.

    Returns:
        True if WeasyPrint can produce a PDF
    """
    try:
        from weasyprint import HTML

        HTML(string="<p>probe</p>").write_pdf()
        return True
    except Exception as e:
        logger.info(
            f"WeasyPrint unavailable ({type(e).__name__}) — using ReportLab. "
            f"For higher-fidelity PDFs: brew install pango cairo gdk-pixbuf libffi"
        )
        return False


def select_engine(preference: Optional[str] = None) -> str:
    """
    Decide which engine to render with.

    Args:
        preference: "auto", "weasyprint", "reportlab", or None to read settings

    Returns:
        "weasyprint" or "reportlab"
    """
    choice = (preference or getattr(settings, "pdf_engine", "auto") or "auto").lower()

    if choice == "reportlab":
        return "reportlab"

    if choice == "weasyprint":
        if not weasyprint_available():
            logger.warning(
                "pdf_engine='weasyprint' but it cannot render here — falling back "
                "to ReportLab. Install its libraries: brew install pango cairo "
                "gdk-pixbuf libffi"
            )
            return "reportlab"
        return "weasyprint"

    return "weasyprint" if weasyprint_available() else "reportlab"


# ============================================================================
# Stylesheet (WeasyPrint)
# ============================================================================

DOCUMENT_CSS = """
@page {
    size: Letter;
    margin: 0.7in 0.75in;
}

* { box-sizing: border-box; }

body {
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.42;
    color: #1a1a1a;
    margin: 0;
}

.name {
    font-size: 20pt;
    font-weight: 700;
    letter-spacing: -0.4pt;
    /* Wide enough that a text extractor treats the name as its own line.
       At 2pt the contact details sat close enough to be concatenated onto
       it — "HASNAT TAHIRPetaling Jaya" — and a resume parser reading the
       first line for the candidate's name gets it wrong. */
    margin: 0 0 6pt 0;
}

.contact {
    font-size: 9pt;
    color: #555;
    margin: 0 0 1pt 0;
}

h2 {
    font-size: 9.5pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.9pt;
    color: #111;
    margin: 15pt 0 5pt 0;
    padding-bottom: 3pt;
    border-bottom: 0.7pt solid #b8b8b8;
    /* Never leave a heading stranded at the foot of a page */
    break-after: avoid;
    page-break-after: avoid;
}

p {
    margin: 0 0 5pt 0;
    orphans: 2;
    widows: 2;
}

/* Custom bullets rather than list-style: the default ::marker renders as a
   small raised dot that sits off the text baseline. */
ul {
    list-style: none;
    margin: 0 0 7pt 0;
    padding-left: 0;
}

li {
    position: relative;
    padding-left: 12pt;
    margin-bottom: 2.5pt;
    break-inside: avoid;
    page-break-inside: avoid;
}

li::before {
    content: "\\2022";
    position: absolute;
    left: 2pt;
    color: #888;
}

strong { font-weight: 700; }
"""


# ============================================================================
# Renderer
# ============================================================================

class PdfRenderer:
    """Renders document text to PDF using the best available engine."""

    def __init__(self, engine: Optional[str] = None, isolate: Optional[bool] = None):
        """
        Initialize the renderer.

        Args:
            engine: "auto", "weasyprint", or "reportlab" (defaults to settings)
            isolate: Run WeasyPrint in a subprocess (defaults to settings).
                Costs a process spawn per render and buys immunity from a
                native crash taking down the caller.
        """
        self.engine = select_engine(engine)
        self.isolate = (
            getattr(settings, "pdf_isolate_weasyprint", True)
            if isolate is None else isolate
        )

    def render(
        self,
        text: str,
        output_path: Path,
        title: Optional[str] = None,
    ) -> Path:
        """
        Render document text to a PDF file.

        Args:
            text: Document text (structure inferred from headings and bullets)
            output_path: Where to write the PDF
            title: PDF metadata title

        Returns:
            The written path

        Raises:
            ValueError: If the text is empty or produces nothing renderable
        """
        blocks = build_blocks(text)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        document_title = title or output_path.stem

        if self.engine == "weasyprint":
            try:
                self._render_weasyprint(blocks, output_path, document_title)
            except Exception as e:
                # A render-time failure shouldn't lose the document
                logger.warning(f"WeasyPrint render failed ({e}) — retrying with ReportLab")
                self.engine = "reportlab"
                self._render_reportlab(blocks, output_path, document_title)
        else:
            self._render_reportlab(blocks, output_path, document_title)

        logger.info(
            f"Rendered PDF via {self.engine}: {output_path} "
            f"({output_path.stat().st_size} bytes)"
        )

        return output_path

    # ------------------------------------------------------------------
    # WeasyPrint
    # ------------------------------------------------------------------

    def _render_weasyprint(self, blocks: List[Block], output_path: Path, title: str) -> None:
        """
        Render blocks through HTML/CSS.

        Runs in a subprocess by default — see `_render_weasyprint_isolated`.

        Args:
            blocks: Structured document blocks
            output_path: Where to write the PDF
            title: Document title

        Raises:
            RuntimeError: If rendering failed (the caller falls back to ReportLab)
        """
        document_html = self.build_html(blocks, title)

        if self.isolate:
            self._render_weasyprint_isolated(document_html, output_path)
            return

        from weasyprint import CSS, HTML

        HTML(string=document_html).write_pdf(
            str(output_path),
            stylesheets=[CSS(string=DOCUMENT_CSS)],
        )

    @staticmethod
    def _render_weasyprint_isolated(document_html: str, output_path: Path) -> None:
        """
        Render through WeasyPrint in a separate process.

        WeasyPrint reaches native code (pango, cairo, harfbuzz) through cffi. A
        fault there takes down the interpreter, and rendering happens inside
        unattended scheduled runs — so a crash would kill the run silently at
        3 AM. In a subprocess the same fault is just a non-zero exit code, and
        the caller retries with ReportLab.

        Args:
            document_html: The HTML to render
            output_path: Where to write the PDF

        Raises:
            RuntimeError: On failure, timeout, or a crash in the child
        """
        job = json.dumps({
            "html": document_html,
            "css": DOCUMENT_CSS,
            "output_path": str(output_path),
        })

        # The child must be able to import the package regardless of its
        # working directory — a scheduled run starts wherever launchd puts it.
        environment = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent.parent)
        existing_path = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            f"{package_root}{os.pathsep}{existing_path}" if existing_path else package_root
        )

        try:
            completed = subprocess.run(
                [sys.executable, "-m", "job_agent.services._pdf_worker"],
                input=job, capture_output=True, text=True,
                timeout=WEASYPRINT_TIMEOUT, env=environment,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"WeasyPrint did not finish within {WEASYPRINT_TIMEOUT}s"
            )

        if completed.returncode == 0:
            return

        # A negative return code means the child was killed by a signal —
        # exactly the native crash this isolation exists for.
        if completed.returncode < 0:
            raise RuntimeError(
                f"WeasyPrint crashed with signal {-completed.returncode} "
                f"(isolated, so the run continues)"
            )

        raise RuntimeError(
            f"WeasyPrint failed: {(completed.stderr or '').strip() or 'unknown error'}"
        )

    def build_html(self, blocks: List[Block], title: str) -> str:
        """
        Convert blocks into the HTML document WeasyPrint renders.

        Args:
            blocks: Structured document blocks
            title: Document title

        Returns:
            Complete HTML string
        """
        parts: List[str] = []

        for block in blocks:
            if block.kind == "name":
                parts.append(f'<div class="name">{self._inline(block.text)}</div>')
            elif block.kind == "contact":
                parts.append(f'<div class="contact">{self._inline(block.text)}</div>')
            elif block.kind == "heading":
                parts.append(f"<h2>{self._inline(block.text)}</h2>")
            elif block.kind == "bullets":
                items = "".join(f"<li>{self._inline(item)}</li>" for item in block.items)
                parts.append(f"<ul>{items}</ul>")
            else:
                parts.append(f"<p>{self._inline(block.text)}</p>")

        body = "\n".join(parts)

        return (
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title></head>"
            f"<body>{body}</body></html>"
        )

    @staticmethod
    def _inline(text: str) -> str:
        """
        Escape text for HTML, keeping simple **bold** markers.

        Escaping first means a resume containing "C++ & <framework>" renders as
        written instead of being swallowed as markup.
        """
        escaped = html.escape(text)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)

    # ------------------------------------------------------------------
    # ReportLab
    # ------------------------------------------------------------------

    def _render_reportlab(self, blocks: List[Block], output_path: Path, title: str) -> None:
        """Render blocks with ReportLab's platypus flowables."""
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            HRFlowable,
            ListFlowable,
            ListItem,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
        )

        base = getSampleStyleSheet()

        styles = {
            "name": ParagraphStyle(
                "DocName", parent=base["Title"], fontName="Helvetica-Bold",
                # See the .name rule in the CSS: the gap keeps the name from
                # being extracted as one run with the contact line.
                fontSize=18, leading=22, spaceAfter=6, alignment=TA_LEFT,
            ),
            "contact": ParagraphStyle(
                "DocContact", parent=base["Normal"], fontName="Helvetica",
                fontSize=9.5, leading=13, textColor="#555555", spaceAfter=1,
            ),
            "heading": ParagraphStyle(
                "DocHeading", parent=base["Heading2"], fontName="Helvetica-Bold",
                fontSize=10, leading=13, spaceBefore=13, spaceAfter=3,
            ),
            "body": ParagraphStyle(
                "DocBody", parent=base["Normal"], fontName="Helvetica",
                fontSize=10, leading=14, spaceAfter=4,
            ),
            "bullet": ParagraphStyle(
                "DocBullet", parent=base["Normal"], fontName="Helvetica",
                fontSize=10, leading=14, spaceAfter=2,
            ),
        }

        flowables: List = []

        for block in blocks:
            if block.kind == "name":
                flowables.append(Paragraph(self._inline(block.text), styles["name"]))
            elif block.kind == "contact":
                flowables.append(Paragraph(self._inline(block.text), styles["contact"]))
            elif block.kind == "heading":
                flowables.append(
                    Paragraph(self._inline(block.text.upper()), styles["heading"])
                )
                flowables.append(
                    HRFlowable(width="100%", thickness=0.6, color="#b8b8b8",
                               spaceBefore=1, spaceAfter=6)
                )
            elif block.kind == "bullets":
                # bulletFontSize must track the body size, otherwise the glyph
                # renders as a small dot floating above the baseline.
                flowables.append(
                    ListFlowable(
                        [
                            ListItem(
                                Paragraph(self._inline(item), styles["bullet"]),
                                leftIndent=14,
                            )
                            for item in block.items
                        ],
                        bulletType="bullet",
                        start="•",
                        bulletFontName="Helvetica",
                        bulletFontSize=10,
                        bulletColor="#888888",
                        bulletOffsetY=-1,
                        leftIndent=14, spaceAfter=5,
                    )
                )
            else:
                flowables.append(Paragraph(self._inline(block.text), styles["body"]))

        flowables.append(Spacer(1, 2))

        SimpleDocTemplate(
            str(output_path),
            pagesize=LETTER,
            leftMargin=0.75 * inch, rightMargin=0.75 * inch,
            topMargin=0.7 * inch, bottomMargin=0.7 * inch,
            title=title,
            author="",  # Never inject an author the user didn't supply
        ).build(flowables)


def render_pdf(
    text: str,
    output_path: Path,
    title: Optional[str] = None,
    engine: Optional[str] = None,
) -> Path:
    """
    Convenience wrapper around PdfRenderer.render().

    Args:
        text: Document text
        output_path: Where to write the PDF
        title: PDF metadata title
        engine: Override the engine choice

    Returns:
        The written path
    """
    return PdfRenderer(engine=engine).render(text, output_path, title)
