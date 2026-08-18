"""
report_pdf_renderer.py
-----------------------
Owns: converting a finalized report markdown body — the same
`report_markdown` string produced by POST /generate_report / POST
/generate_report/pdf (api/server.py) from
agent_service.prompt_builders.build_report_generation_prompt — into a
downloadable, paginated PDF.

This is a DELIBERATE, standalone renderer. It never imports or calls
into utils/html_converter.py, per New_REPORT_Design_1.md (ACTIONS #6 /
TASKS #5): html_converter.py's <details>/<summary> collapsible sections
(Data Provenance, Similar Cases) and its <script> toggle fallback exist
to let an investigator click a section open in the on-screen AppWorks
HTML widget panel. Neither concept survives a headless print render —
a section that starts collapsed can end up completely absent from the
rendered PDF. Every section here is rendered flat and always-visible
instead.

Pipeline: markdown -> plain static HTML (no JS, no <details>) -> PDF
via a headless, in-process render using xhtml2pdf (pure Python, built
on reportlab). Deliberately NOT WeasyPrint: despite being pip-
installable, WeasyPrint still dlopens native Pango/cairo/gobject
libraries at import time — present by default on most Linux dev boxes
(which is why an earlier version of this module worked there), but NOT
present on a stock Windows deployment, where importing it fails with
an OSError trying to load libgobject. xhtml2pdf has no such OS-level
dependency, which is the deciding factor for a Windows-hosted API
process. Its CSS support is narrower than a real browser engine
(html_converter.py's on-screen renderer can lean on more of the CSS
spec), so the stylesheet below is deliberately kept to the subset
xhtml2pdf/reportlab is known to support — no :nth-child, no
text-transform, page numbering via xhtml2pdf's own @frame/<pdf:*>
syntax instead of WeasyPrint's @bottom-center.

Does NOT own: assembling report content (reasoning_layer/
report_generation.py, reasoning_layer/decision_log.py,
agent_service/prompt_builders.py) or either report route that calls
this module (api/server.py: POST /generate_report, POST
/generate_report/pdf).
"""

from __future__ import annotations

import io
import logging
import re
from typing import Optional

import markdown2
from xhtml2pdf import pisa

logger = logging.getLogger(__name__)


class ReportPdfRenderError(RuntimeError):
    """Raised when xhtml2pdf reports a render failure (result.err != 0)."""


# ---------------------------------------------------------------------------
# Print stylesheet — static only, no interactivity, no collapsible markup.
# Deliberately separate from html_converter.py's _BSI_STYLE, which styles
# an on-screen, collapsible widget panel rather than a paginated document.
# Colour tokens are kept consistent with the on-screen BSI styling so the
# exported PDF still reads as the same product, just print-adapted.
# ---------------------------------------------------------------------------
_PRINT_STYLE = """
@page {
    size: a4 portrait;
    margin: 20mm 16mm 22mm 16mm;
    @frame footer_frame {
        -pdf-frame-content: bsi-pdf-footer;
        bottom: 8mm;
        left: 16mm;
        right: 16mm;
        height: 10mm;
    }
}

body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 11px;
    line-height: 1.5;
    color: #1A1A2E;
}

#bsi-pdf-footer {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 8px;
    color: #546285;
    text-align: center;
}

.bsi-report-header {
    border-bottom: 2px solid #0C1A5B;
    padding-bottom: 10px;
    margin-bottom: 16px;
}

.bsi-report-header .bsi-report-title {
    font-size: 18px;
    font-weight: bold;
    color: #0C1A5B;
    margin: 0 0 4px 0;
}

.bsi-report-header .bsi-report-meta {
    font-size: 10px;
    color: #546285;
}

h1 {
    font-size: 15px;
    font-weight: bold;
    color: #0C1A5B;
    border-bottom: 1.5px solid #0C1A5B;
    padding-bottom: 5px;
    margin: 16px 0 10px 0;
}

h2 {
    font-size: 12.5px;
    font-weight: bold;
    color: #0C1A5B;
    background-color: #EEF2FA;
    padding: 4px 8px;
    margin: 14px 0 8px 0;
}

h3 {
    font-size: 11.5px;
    font-weight: bold;
    color: #1B3A7A;
    margin: 10px 0 5px 0;
    padding-bottom: 3px;
}

p { margin: 4px 0; }

strong, b { color: #0C1A5B; font-weight: bold; }
em, i { color: #546285; }

table {
    width: 100%;
    border-collapse: collapse;
    font-size: 10px;
    margin: 8px 0 12px 0;
    border: 1px solid #D0D7E6;
}

thead th {
    background-color: #0C1A5B;
    color: #FFFFFF;
    padding: 6px 8px;
    text-align: left;
    font-weight: bold;
}

tbody td {
    padding: 5px 8px;
    border-bottom: 1px solid #D0D7E6;
    vertical-align: top;
}

tbody tr.bsi-row-even td { background-color: #F7F9FC; }

ul, ol { margin: 5px 0; padding-left: 20px; }
li { margin-bottom: 3px; }

hr {
    border: none;
    border-top: 1px solid #D0D7E6;
    margin: 14px 0;
}

code, pre {
    font-family: Courier, monospace;
    font-size: 9.5px;
    background-color: #F0F4F9;
}
pre { padding: 8px; border: 1px solid #D0D7E6; }
code { padding: 1px 4px; }
"""

# xhtml2pdf has no :nth-child support, so alternating table-row shading is
# applied by tagging every second <tbody><tr> with this class instead.
_TBODY_TR_RE = re.compile(r"(<tbody\b[^>]*>)(.*?)(</tbody>)", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr(\s[^>]*)?>", re.IGNORECASE)

# Same markdown2 extras html_converter.py uses for the on-screen render
# (tables, fenced code, strikethrough) plus break-on-newline, since a
# PDF has no follow-up on-screen reflow to fall back on.
_MARKDOWN_EXTRAS = ["tables", "fenced-code-blocks", "strike", "break-on-newline"]

_STRIP_RE = re.compile(r"[\r\n\t]+")


def _escape(value: Optional[str]) -> str:
    """Minimal HTML-escape for header metadata values (case_id, report_id,
    timestamps) — these are server-generated/identifier-shaped, but escaped
    defensively since they are interpolated directly into the HTML head."""
    if not value:
        return ""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _zebra_stripe_tables(html: str) -> str:
    """Tag every second <tr> inside each <tbody> with bsi-row-even, since
    xhtml2pdf does not support the :nth-child CSS selector used for this
    in a real browser engine."""

    def stripe_tbody(match: re.Match) -> str:
        """Zebra-stripe one matched <tbody>'s rows by tagging every second <tr>."""
        open_tag, body, close_tag = match.group(1), match.group(2), match.group(3)
        counter = [0]

        def tag_row(row_match: re.Match) -> str:
            """Add the bsi-row-even class to every second <tr> in sequence."""
            counter[0] += 1
            if counter[0] % 2 == 0:
                attrs = row_match.group(1) or ""
                return f'<tr class="bsi-row-even"{attrs}>'
            return row_match.group(0)

        return open_tag + _TR_RE.sub(tag_row, body) + close_tag

    return _TBODY_TR_RE.sub(stripe_tbody, html)


def render_report_markdown_to_html(
    markdown_text: str,
    *,
    case_id: str,
    report_id: str,
    generated_at: str,
    complaint_number: Optional[str] = None,
) -> str:
    """
    Convert the finalized report markdown body into a SELF-CONTAINED,
    static HTML document suitable for a headless print render.

    Every section that render_agent_summary() (html_converter.py) would
    render as an on-screen collapsible <details> block is rendered flat
    and always-visible here instead — see module docstring for why.

    The VISIBLE report title shows complaint_number (AppWorks'
    WorkfolderComplaintNumber — see core.case_store.get_complaint_number),
    falling back to case_id only if complaint_number is unavailable
    (e.g. a very old cached report predating this field). case_id
    itself never appears anywhere in the rendered document — an
    investigator has no way to recognise it; the complaint number is
    the same identifier they already work with in AppWorks.
    """
    display_id = complaint_number or case_id
    body_html = markdown2.markdown(markdown_text or "", extras=_MARKDOWN_EXTRAS)
    body_html = _zebra_stripe_tables(body_html)

    header_html = (
        '<div class="bsi-report-header">'
        f'<div class="bsi-report-title">Investigation Report — {_escape(display_id)}</div>'
        f'<div class="bsi-report-meta">Report ID: {_escape(report_id)}'
        f" &nbsp;|&nbsp; Generated: {_escape(generated_at)}</div>"
        "</div>"
    )

    # xhtml2pdf's page-number support is its own <pdf:pagenumber>/
    # <pdf:pagecount> tags inside the @frame-designated footer element
    # (see -pdf-frame-content: bsi-pdf-footer in _PRINT_STYLE), not
    # WeasyPrint's counter(page)/counter(pages) CSS content.
    footer_html = '<div id="bsi-pdf-footer">Page <pdf:pagenumber /> of <pdf:pagecount /></div>'

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<style>{_PRINT_STYLE}</style></head><body>"
        f"{footer_html}{header_html}{body_html}"
        "</body></html>"
    )


def render_report_pdf(
    markdown_text: str,
    *,
    case_id: str,
    report_id: str,
    generated_at: str,
    complaint_number: Optional[str] = None,
) -> bytes:
    """
    Full markdown -> PDF pipeline for the report export capability
    (New_REPORT_Design_1.md, ACTIONS #6 / TASKS #5). Headless render via
    xhtml2pdf, entirely in-process — pure Python (reportlab-backed), no
    browser or OS-level native library to install or manage in
    production (see module docstring for why this isn't WeasyPrint).

    case_id is kept as a required parameter purely for LOGGING (the
    three log lines below) — real operational traceability is worth
    keeping even though nothing user-facing shows it anymore.
    complaint_number is what actually renders in the document; see
    render_report_markdown_to_html's docstring.

    Raises ReportPdfRenderError on a failed render (pisa reports a
    non-zero err count) and re-raises any exception xhtml2pdf itself
    throws; the caller (api/server.py) is responsible for translating
    either into the route's standard HTTPException(500) handling, same
    as every other failure mode on that route.
    """
    html_document = render_report_markdown_to_html(
        markdown_text,
        case_id=case_id,
        report_id=report_id,
        generated_at=generated_at,
        complaint_number=complaint_number,
    )
    output = io.BytesIO()
    try:
        result = pisa.CreatePDF(io.StringIO(html_document), dest=output)
    except Exception:
        logger.exception(
            "Report PDF render failed for case_id=%s report_id=%s",
            case_id,
            report_id,
        )
        raise

    if result.err:
        logger.error(
            "Report PDF render reported %d error(s) for case_id=%s report_id=%s",
            result.err,
            case_id,
            report_id,
        )
        raise ReportPdfRenderError(f"PDF render failed with {result.err} error(s) for case_id={case_id}")

    return output.getvalue()


def report_pdf_filename(display_id: str, report_id: str) -> str:
    """
    Derive a safe download filename. display_id should be the
    investigator-facing complaint number (falls back to case_id only if
    unavailable — see render_report_markdown_to_html's docstring); the
    downloaded filename is exactly as user-visible as the PDF's own
    title, so it gets the same treatment. Strips anything that isn't
    alphanumeric/dash/underscore so neither value (both server-generated,
    but defensively sanitised here) can break the Content-Disposition
    header.
    """

    def _clean(value: object) -> str:
        # str() defensively, not just an `or ""` fallback: this is the
        # last line of defense before building a filename, and a
        # non-string value (e.g. AppWorks returning a field as a JSON
        # number rather than a string — confirmed happening for
        # complaint_no in production) must degrade to "stringify it",
        # not crash the whole PDF response with a raw TypeError.
        text = "" if value is None else str(value)
        text = _STRIP_RE.sub("", text)
        return re.sub(r"[^A-Za-z0-9_\-]", "_", text) or "unknown"

    return f"Investigation_Report_{_clean(display_id)}_{_clean(report_id)}.pdf"
