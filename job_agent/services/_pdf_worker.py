"""
WeasyPrint rendering worker (Phase 9 hardening).

Renders one PDF and exits. Invoked as a subprocess by PdfRenderer:

    python -m job_agent.services._pdf_worker < job.json

Reading a JSON job from stdin — `{"html": ..., "css": ..., "output_path": ...}` —
and writing the PDF to `output_path`.

**Why a subprocess.** WeasyPrint renders through cffi into pango/cairo/harfbuzz.
A fault anywhere in that native stack takes down the whole interpreter, and PDF
rendering happens inside unattended scheduled runs where that means the run dies
silently at 3 AM. Isolating it converts a fatal crash into a non-zero exit code
that the caller can retry with ReportLab.

This module deliberately imports nothing from the rest of the package: it should
start fast and share no state with the agent.
"""

import json
import sys


def main() -> int:
    """
    Render one PDF from a job on stdin.

    Returns:
        0 on success, non-zero on failure
    """
    try:
        job = json.load(sys.stdin)
    except Exception as e:
        print(f"could not read the render job: {e}", file=sys.stderr)
        return 2

    html = job.get("html")
    css = job.get("css") or ""
    output_path = job.get("output_path")

    if not html or not output_path:
        print("render job needs 'html' and 'output_path'", file=sys.stderr)
        return 2

    try:
        from weasyprint import CSS, HTML

        HTML(string=html).write_pdf(
            output_path,
            stylesheets=[CSS(string=css)] if css else None,
        )
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
