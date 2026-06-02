"""
html_converter.py
--------------------
Drop-in replacement for your bare markdown2.markdown() call.
Converts LLM agent_summary markdown into BSI-styled HTML that
matches the OpenText AppWorks UI chrome (navy #0C1A5B palette,
Segoe UI typography, table/section conventions).


"""

import re
import markdown2
from typing import Optional, List, Tuple

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

_BSI_STYLE = """<style>
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


# ---------------------------------------------------------------------------
# Internal post-processing helpers
# ---------------------------------------------------------------------------

_RISK_TIERS = {
    "CRITICAL": "bsi-risk-critical",
    "HIGH":     "bsi-risk-high",
    "MEDIUM":   "bsi-risk-medium",
    "LOW":      "bsi-risk-low",
}

# Headings that mark the start of the Data Provenance block
_PROVENANCE_H2_RE = re.compile(
    r'<h[23]>Data\s+(?:Provenance|Sources)[^<]*</h[23]>',
    re.IGNORECASE,
)

# Everything from the provenance h2 to end-of-content (greedy last section)
_PROVENANCE_BLOCK_RE = re.compile(
    r'(<h[23]>Data\s+(?:Provenance|Sources)[^<]*</h[23]>)(.*?)(?=<h[23]>|$)',
    re.IGNORECASE | re.DOTALL,
)

_STEP_LABEL_RE = re.compile(r'<strong>(Step\s+\d+:)</strong>')

_SCORE_RE = re.compile(
    r'<strong>(\d+(?:\.\d+)?\s+points?)</strong>',
    re.IGNORECASE,
)


def _inject_risk_badges(html: str) -> str:
    """Wrap standalone risk tier words (not inside tags) with badge spans."""
    for tier, css_class in _RISK_TIERS.items():
        # Match the word case-insensitively, not already inside an HTML tag
        html = re.sub(
            rf'(?<!<[^>]{0,200})\b{tier}\b(?![^<]*>)',
            lambda m: f'<span class="{css_class}">{m.group(0)}</span>',
            html,
            flags=re.IGNORECASE,
        )
    return html


def _wrap_provenance_section(html: str) -> str:
    """
    Move the Data Provenance / Data Sources section into a styled
    .bsi-provenance-section wrapper div.
    """
    def replacer(m: re.Match) -> str:
        heading_html = m.group(1)
        body_html    = m.group(2)
        # Strip the h2 tag — label goes into <summary> instead
        heading_text = re.sub(r'<[^>]+>', '', heading_html).strip()
        return (
            f'<details class="bsi-provenance-section">'
            f'<summary>{heading_text}</summary>'
            f'<div class="bsi-provenance-body">{body_html}</div>'
            f'</details>'
        )
    return _PROVENANCE_BLOCK_RE.sub(replacer, html)


def _style_step_labels(html: str) -> str:
    """Convert **Step N:** bold markers to pill badges."""
    return _STEP_LABEL_RE.sub(
        r'<strong class="bsi-step-label">\1</strong>', html
    )


def _style_score_metrics(html: str) -> str:
    """Promote bold point-score values to larger metric callout spans."""
    return _SCORE_RE.sub(
        r'<span class="bsi-metric">\1</span>', html
    )



# ---------------------------------------------------------------------------
# Similar Cases collapsible rows — SCOPED to case-list headings only
# ---------------------------------------------------------------------------
# Matches headings like: "Similar Cases", "Related Cases",
# "Returned Historical Cases", "Historical Cases", "Prior Cases"
# Does NOT match: Investigation Steps, Allegations, plan checklists.

_SIMILAR_SECTION_RE = re.compile(
    r'(<h[23][^>]*>'
    r'(?=[^<]*\bCases?\b)'
    r'(?=[^<]*(?:Similar|Related|Returned|Historical|Prior|Subject\s+History|Overview|came\s+back|found))'
    r'[^<]*</h[23]>)'
    r'(.*?)'
    r'(?=<h[23]>|$)',
    re.IGNORECASE | re.DOTALL,
)

# Pattern A — nested <ul> sub-items (standard markdown2 output)
# e.g.  <li>Case ID: 123 <ul><li>Date: ...</li></ul></li>
_SIMILAR_ITEM_UL_RE = re.compile(
    r'<li>((?:(?!</?[uo]l>).)*?)<ul>(.*?)</ul>\s*</li>',
    re.DOTALL,
)

# Pattern B — <p> with <br/> fields (LLM inline-paragraph style)
# e.g.  <li><p><strong>Case ID:</strong> 123<br/><strong>Date:</strong> ...<br/></p></li>
_SIMILAR_ITEM_P_RE = re.compile(
    r'<li>\s*<p>(.*?)</p>\s*</li>',
    re.DOTALL,
)

# Extracts individual <strong>Label:</strong> Value pairs from a <p><br/> block
_FIELD_RE = re.compile(
    r'<strong>([^<]+?):?\s*</strong>\s*(.*?)(?=\s*<br\s*/?>|\s*<strong>|\s*$)',
    re.DOTALL,
)

_SIMILAR_LIST_RE = re.compile(r'<(ol|ul)>(.*?)</\1>', re.DOTALL)


def _build_case_row_from_ul(counter: int, header_html: str, sub_ul_body: str) -> str:
    """Build collapsible row from nested-<ul> item structure (Pattern A)."""
    return (
        f'<li>'
        f'<details class="bsi-case-item">'
        f'<summary><span class="bsi-case-num">{counter}</span>{header_html.strip()}</summary>'
        f'<div class="bsi-case-body"><ul>{sub_ul_body}</ul></div>'
        f'</details>'
        f'</li>'
    )


def _build_case_row_from_p(counter: int, p_content: str) -> str:
    """
    Build collapsible row from <p><br/> item structure (Pattern B).
    First field (Case ID) becomes the summary header; remaining fields
    become the 2-column body grid.
    """
    # Clean up <br/> whitespace noise
    p_clean = re.sub(r'\s*<br\s*/?>\s*', '\n', p_content).strip()
    fields = _FIELD_RE.findall(p_clean)

    if not fields:
        # Fallback — can't parse, return unchanged plain <li>
        return f'<li><p>{p_content}</p></li>'

    # First field → summary header (typically "Case ID")
    first_label, first_value = fields[0]
    summary_text = f'<strong>{first_label.strip()}:</strong>&nbsp;{first_value.strip()}'

    # Remaining fields → body grid cells
    body_cells = ''.join(
        f'<li><strong>{lbl.strip()}</strong>{val.strip()}</li>'
        for lbl, val in fields[1:]
    )

    return (
        f'<li>'
        f'<details class="bsi-case-item">'
        f'<summary><span class="bsi-case-num">{counter}</span>{summary_text}</summary>'
        f'<div class="bsi-case-body"><ul>{body_cells}</ul></div>'
        f'</details>'
        f'</li>'
    )


def find_outer_list(html: str) -> Optional[tuple[int, int, str, str]]:
    """
    Finds the first outermost <ol> or <ul> in the HTML block,
    respecting nested lists by counting tag depth.
    Returns: (start_index, end_index, tag_type, list_body)
    """
    # Find the first occurrence of <ol> or <ul>
    match = re.search(r'<(ol|ul)\b[^>]*>', html, re.IGNORECASE)
    if not match:
        return None
    
    tag_type = match.group(1).lower()  # 'ol' or 'ul'
    start_pos = match.start()
    
    # We now scan forward to find the matching closing tag
    depth = 0
    
    tag_re = re.compile(r'</?(ol|ul)\b[^>]*>', re.IGNORECASE)
    for m in tag_re.finditer(html, start_pos):
        tag = m.group(0)
        is_closing = tag.startswith('</')
        
        if is_closing:
            depth -= 1
            if depth == 0:
                end_pos = m.end()
                # Extract the body (excluding the outer tags)
                list_body = html[match.end():m.start()]
                return start_pos, end_pos, tag_type, list_body
        else:
            depth += 1
            
    return None


def _collapsible_similar_cases(html: str) -> str:
    """
    Convert the <ol>/<ul> inside recognised case-list headings into collapsible
    <details>/<summary> rows.  Handles both LLM output structures:

      Pattern A — nested <ul> sub-items  (standard markdown list)
      Pattern B — <li><p> with <br/> fields  (inline paragraph style)

    Strictly scoped: only the section whose heading matches the case-list
    keyword list is transformed.  All other lists in the document are
    untouched.
    """
    def convert_section(sec_m: re.Match) -> str:
        heading_html = sec_m.group(1)
        section_body = sec_m.group(2)

        # Use find_outer_list helper to locate and parse the outer list
        list_info = find_outer_list(section_body)
        if not list_info:
            return sec_m.group(0)

        start_idx, end_idx, list_tag, list_body = list_info

        # Detect which pattern is present
        has_ul  = bool(_SIMILAR_ITEM_UL_RE.search(list_body))
        has_p   = bool(_SIMILAR_ITEM_P_RE.search(list_body))

        if not has_ul and not has_p:
            return sec_m.group(0)  # plain list — leave untouched

        counter = [0]

        if has_ul:
            def convert_ul_item(m: re.Match) -> str:
                counter[0] += 1
                return _build_case_row_from_ul(counter[0], m.group(1), m.group(2))
            converted = _SIMILAR_ITEM_UL_RE.sub(convert_ul_item, list_body)
        else:
            def convert_p_item(m: re.Match) -> str:
                counter[0] += 1
                return _build_case_row_from_p(counter[0], m.group(1))
            converted = _SIMILAR_ITEM_P_RE.sub(convert_p_item, list_body)

        # Construct the new section body with the transformed list
        new_list_html = f'<{list_tag} class="bsi-case-list">{converted}</{list_tag}>'
        new_section_body = section_body[:start_idx] + new_list_html + section_body[end_idx:]

        return f'{heading_html}{new_section_body}'

    return _SIMILAR_SECTION_RE.sub(convert_section, html)


def _post_process(html: str) -> str:
    html = _inject_risk_badges(html)
    html = _wrap_provenance_section(html)
    html = _style_step_labels(html)
    html = _style_score_metrics(html)
    html = _collapsible_similar_cases(html)
    return html


# ---------------------------------------------------------------------------
# JS fallback for <details>/<summary> in AppWorks HTML panels
# ---------------------------------------------------------------------------
# AppWorks HTML widget panels occasionally suppress native <details> toggle
# behaviour when the panel re-renders or is embedded in certain widget types.
# This small inline script binds explicit onclick handlers as a fallback.
# It is a no-op if the browser already supports <details> natively — it only
# activates when <details> is missing the built-in open/close mechanism.

_JS_DETAILS_FALLBACK = """<script>
(function () {
  function initToggle(el) {
    var summary = el.querySelector(':scope > summary');
    if (!summary) return;
    summary.addEventListener('click', function (e) {
      e.preventDefault();
      if (typeof el.open !== 'undefined') {
        // Native <details> support present — let the browser handle it
        el.open = !el.open;
      } else {
        // Fallback: toggle via CSS class
        el.classList.toggle('bsi-open');
      }
    });
  }
  document.querySelectorAll(
    'details.bsi-case-item, details.bsi-provenance-section'
  ).forEach(initToggle);
})();
</script>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_agent_summary(
    markdown_text: str,
    stale_warning: bool = False,
) -> str:
    """
    Convert a markdown agent summary to self-contained BSI-styled HTML.

    Args:
        markdown_text : Markdown string produced by the LLM agent pipeline.
        stale_warning : If True, prepend the "case details have changed" banner
                        (mirrors the red banner currently shown in Copilot tab).

    Returns:
        HTML string — embed directly into AppWorks HTML widget panel.
        No external CSS dependencies; all styles are self-contained.
    """
    html_body = markdown2.markdown(
        markdown_text,
        extras=[
            "tables",
            "fenced-code-blocks",
            "strike",
        ],
    )

    html_body = _post_process(html_body)

    banner = ""
    if stale_warning:
        banner = (
            '<div class="bsi-stale-warning">'
            "Case details have changed since this AI summary was generated. "
            "Reload the summary from the <strong>Case Summary</strong> tab to continue."
            "</div>\n"
        )

    return (
        f"{_BSI_STYLE}\n"
        f'<div class="bsi-content">\n'
        f"{banner}"
        f"{html_body}\n"
        f"</div>\n"
        f"{_JS_DETAILS_FALLBACK}"
    )
