"""
Owns: BSI_STYLE — the CSS block utils/html_converter.py's
render_agent_summary injects into every rendered agent_summary,
matching the OpenText AppWorks UI chrome (navy #0C1A5B palette, Segoe UI
typography, table/section conventions, and the four risk-tier color
pairs). Design tokens are documented above the CSS itself, extracted
from AppWorks CPU Release v1.4.54 screenshots.

Split out of html_converter.py verbatim, same rationale as the
reasoning_layer/queries package: this file owns style text only, not
how or when it gets injected — that stays in html_converter.py, which
imports BSI_STYLE from here.

Deliberately separate from utils/report_pdf_renderer.py's own style
block, which targets a different rendering pipeline (xhtml2pdf, not a
browser) with different CSS support — the two are not meant to merge.
"""

# ---------------------------------------------------------------------------
# BSI Design Tokens (extracted from AppWorks CPU Release v1.4.54 screenshots)
# ---------------------------------------------------------------------------
# Navy primary  : #0C1A5B   (header, buttons, active tabs)
# Navy mid      : #1B3A7A   (secondary blue, left accents)
# Navy light    : #E8ECF5   (table header alt, section tint)
# Border        : #D0D7E6   (table cell lines, dividers)
# Surface       : #F7F9FC   (page background / even rows)
# Text primary  : #1A1A2E   (body copy)
# Text muted    : #546285   (secondary labels, provenance text)
# Risk LOW      : #1B5E20 on #E8F5E9
# Risk MEDIUM   : #E65100 on #FFF8E1
# Risk HIGH     : #B71C1C on #FFEBEE
# Risk CRITICAL : #FFFFFF  on #B71C1C
# ---------------------------------------------------------------------------

BSI_STYLE = """<style>
.bsi-content {
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 13px;
    line-height: 1.6;
    color: #1A1A2E;
    padding: 4px 14px 16px 14px;
    max-width: 900px;
}

/* ── Headings ─────────────────────────────────────────────── */
.bsi-content h1 {
    font-size: 17px;
    font-weight: 700;
    color: #0C1A5B;
    border-bottom: 2px solid #0C1A5B;
    padding-bottom: 7px;
    margin: 10px 0 14px 0;
    letter-spacing: 0.01em;
}

.bsi-content h2 {
    font-size: 13px;
    font-weight: 700;
    color: #0C1A5B;
    border-left: 3px solid #0C1A5B;
    background: #EEF2FA;
    padding: 5px 10px;
    margin: 18px 0 8px 0;
    border-radius: 0 3px 3px 0;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

.bsi-content h3 {
    font-size: 13px;
    font-weight: 600;
    color: #1B3A7A;
    margin: 12px 0 5px 0;
    border-bottom: 1px dashed #D0D7E6;
    padding-bottom: 3px;
}

/* ── Paragraphs & inline ──────────────────────────────────── */
.bsi-content p {
    margin: 5px 0;
}

.bsi-content strong {
    color: #0C1A5B;
    font-weight: 600;
}

.bsi-content em {
    color: #546285;
}

/* ── Tables ──────────────────────────────────────────────── */
.bsi-content table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin: 10px 0 14px 0;
    border: 1px solid #D0D7E6;
}

.bsi-content thead th {
    background: #0C1A5B;
    color: #FFFFFF;
    padding: 8px 10px;
    text-align: left;
    font-weight: 600;
    font-size: 12px;
    letter-spacing: 0.02em;
    white-space: nowrap;
}

.bsi-content tbody td {
    padding: 6px 10px;
    border-bottom: 1px solid #D0D7E6;
    vertical-align: top;
}

.bsi-content tbody tr:nth-child(even) td {
    background: #F0F4F9;
}

.bsi-content tbody tr:hover td {
    background: #E8ECF5;
}

/* ── Lists ───────────────────────────────────────────────── */
.bsi-content ul, .bsi-content ol {
    margin: 6px 0;
    padding-left: 22px;
}

.bsi-content li {
    margin-bottom: 4px;
}

.bsi-content li strong {
    color: #0C1A5B;
}

/* ── Risk tier badges ────────────────────────────────────── */
.bsi-risk-low {
    display: inline-block;
    background: #E8F5E9;
    color: #1B5E20;
    padding: 1px 9px;
    border-radius: 3px;
    font-weight: 700;
    font-size: 12px;
    border: 1px solid #A5D6A7;
}

.bsi-risk-medium {
    display: inline-block;
    background: #FFF8E1;
    color: #E65100;
    padding: 1px 9px;
    border-radius: 3px;
    font-weight: 700;
    font-size: 12px;
    border: 1px solid #FFCC80;
}

.bsi-risk-high {
    display: inline-block;
    background: #FFEBEE;
    color: #B71C1C;
    padding: 1px 9px;
    border-radius: 3px;
    font-weight: 700;
    font-size: 12px;
    border: 1px solid #EF9A9A;
}

.bsi-risk-critical {
    display: inline-block;
    background: #B71C1C;
    color: #FFFFFF;
    padding: 1px 9px;
    border-radius: 3px;
    font-weight: 700;
    font-size: 12px;
}

/* ── Data Provenance collapsible block ───────────────────── */
.bsi-provenance-section {
    border: 1px solid #D0D7E6;
    border-left: 3px solid #1B3A7A;
    border-radius: 0 4px 4px 0;
    margin: 14px 0;
    font-size: 12px;
    color: #546285;
    overflow: hidden;
}

.bsi-provenance-section summary {
    display: flex;
    align-items: center;
    gap: 7px;
    background: #EEF2FA;
    padding: 6px 12px;
    cursor: pointer;
    font-size: 11px;
    font-weight: 700;
    color: #1B3A7A;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    list-style: none;
    user-select: none;
}

.bsi-provenance-section summary::-webkit-details-marker {
    display: none;
}

.bsi-provenance-section summary::before {
    content: '▶';
    font-size: 9px;
    color: #1B3A7A;
    transition: transform 0.15s ease;
    display: inline-block;
    width: 10px;
}

.bsi-provenance-section[open] summary::before {
    transform: rotate(90deg);
}

.bsi-provenance-section summary:hover {
    background: #E0E8F5;
}

.bsi-provenance-body {
    background: #F0F4F9;
    padding: 10px 14px;
}

.bsi-provenance-section ul {
    margin: 0;
    padding-left: 16px;
}

.bsi-provenance-section li {
    margin-bottom: 3px;
    line-height: 1.5;
}

.bsi-provenance-section strong {
    color: #0C1A5B;
}

/* ── Step label (Investigation plan) ─────────────────── */
.bsi-step-label {
    font-weight: 700;
    color: #0C1A5B;
    font-size: 13px;
    margin-right: 5px;
}

/* ── Score / metric callout ──────────────────────────────── */
.bsi-metric {
    font-size: 16px;
    font-weight: 700;
    color: #0C1A5B;
}

/* ── Stale-summary warning banner ────────────────────────── */
.bsi-stale-warning {
    background: #FFF8E1;
    border-left: 3px solid #F57F17;
    padding: 6px 12px;
    font-size: 12px;
    color: #7B4F00;
    margin: 0 0 12px 0;
    border-radius: 0 3px 3px 0;
}

/* ── Collapsible case list (Similar / Historical Cases ONLY) ─ */
.bsi-content ol.bsi-case-list, .bsi-content ul.bsi-case-list {
    list-style: none;
    padding: 0;
    margin: 8px 0 14px 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.bsi-case-list > li {
    border: 1px solid #D0D7E6;
    border-radius: 4px;
    overflow: hidden;
}

.bsi-case-item {
    width: 100%;
}

.bsi-case-item summary {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 8px 12px;
    cursor: pointer;
    background: #F7F9FC;
    list-style: none;
    user-select: none;
    font-size: 12px;
    font-weight: 600;
    color: #1A1A2E;
    border-left: 3px solid #D0D7E6;
    transition: background 0.1s;
}

.bsi-case-item summary::-webkit-details-marker { display: none; }

.bsi-case-item summary::before {
    content: '▶';
    font-size: 8px;
    color: #546285;
    transition: transform 0.15s ease;
    display: inline-block;
    width: 10px;
    flex-shrink: 0;
}

.bsi-case-item[open] summary::before  { transform: rotate(90deg); }
.bsi-case-item[open] summary          { background: #EEF2FA; border-left-color: #0C1A5B; color: #0C1A5B; }
.bsi-case-item summary:hover          { background: #E8ECF5; border-left-color: #1B3A7A; }

/* JS-fallback open state (class toggled when <details> not supported) */
.bsi-case-item.bsi-open summary           { background: #EEF2FA; border-left-color: #0C1A5B; color: #0C1A5B; }
.bsi-case-item.bsi-open summary::before   { transform: rotate(90deg); }
.bsi-case-item.bsi-open .bsi-case-body    { display: block; }
.bsi-case-item:not([open]):not(.bsi-open) .bsi-case-body { display: none; }
.bsi-case-item[open] .bsi-case-body { display: block; }

/* JS-fallback for provenance */
.bsi-provenance-section.bsi-open summary         { background: #E0E8F5; }
.bsi-provenance-section.bsi-open summary::before { transform: rotate(90deg); }
.bsi-provenance-section.bsi-open .bsi-provenance-body  { display: block; }
.bsi-provenance-section:not([open]):not(.bsi-open) .bsi-provenance-body { display: none; }
.bsi-provenance-section[open] .bsi-provenance-body { display: block; }

.bsi-case-num {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: #D0D7E6;
    color: #546285;
    font-size: 10px;
    font-weight: 700;
    flex-shrink: 0;
}

.bsi-case-item[open] .bsi-case-num,
.bsi-case-item.bsi-open .bsi-case-num {
    background: #0C1A5B;
    color: #fff;
}

.bsi-case-body { background: #fff; }

.bsi-case-body ul {
    list-style: none !important;
    padding: 0 !important;
    margin: 0 !important;
    display: grid;
    grid-template-columns: 1fr 1fr;
    border-top: 1px solid #E8ECF5;
}

.bsi-case-body ul li {
    padding: 7px 14px;
    font-size: 12px;
    border-bottom: 1px solid #F0F4F9;
    border-right: 1px solid #F0F4F9;
    color: #1A1A2E;
    margin: 0 !important;
}

.bsi-case-body ul li:nth-child(even) { border-right: none; }

.bsi-case-body ul li strong {
    display: block;
    font-size: 10px;
    font-weight: 700;
    color: #546285;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 2px;
}
</style>"""


