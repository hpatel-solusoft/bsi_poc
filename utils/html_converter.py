"""
html_converter.py
--------------------
Drop-in replacement for your bare markdown2.markdown() call.
Converts LLM agent_summary markdown into BSI-styled HTML that
matches the OpenText AppWorks UI chrome (navy #0C1A5B palette,
Segoe UI typography, table/section conventions).


"""

import re
from typing import Optional

import markdown2

from utils.bsi_styles import BSI_STYLE

# ---------------------------------------------------------------------------
# Internal post-processing helpers
# ---------------------------------------------------------------------------

_RISK_TIERS = {
    "CRITICAL": "bsi-risk-critical",
    "HIGH": "bsi-risk-high",
    "MEDIUM": "bsi-risk-medium",
    "LOW": "bsi-risk-low",
}

# Headings that mark the start of the Data Provenance block
_PROVENANCE_H2_RE = re.compile(
    r"<h[23]>Data\s+(?:Provenance|Sources)[^<]*</h[23]>",
    re.IGNORECASE,
)

# Everything from the provenance h2 to end-of-content (greedy last section)
_PROVENANCE_BLOCK_RE = re.compile(
    r"(<h[23]>Data\s+(?:Provenance|Sources)[^<]*</h[23]>)(.*?)(?=<h[23]>|$)",
    re.IGNORECASE | re.DOTALL,
)

_STEP_LABEL_RE = re.compile(r"<strong>(Step\s+\d+:)</strong>")

_SCORE_RE = re.compile(
    r"<strong>(\d+(?:\.\d+)?\s+points?)</strong>",
    re.IGNORECASE,
)


def _inject_risk_badges(html: str) -> str:
    """Wrap standalone risk tier words (not inside tags) with badge spans."""
    for tier, css_class in _RISK_TIERS.items():
        # Match the word case-insensitively, not already inside an HTML tag
        html = re.sub(
            rf"(?<!<[^>]{0,200})\b{tier}\b(?![^<]*>)",
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
        """Wrap one matched provenance heading+body pair in a collapsible <details> block."""
        heading_html = m.group(1)
        body_html = m.group(2)
        # Strip the h2 tag — label goes into <summary> instead
        heading_text = re.sub(r"<[^>]+>", "", heading_html).strip()
        return (
            f'<details class="bsi-provenance-section">'
            f"<summary>{heading_text}</summary>"
            f'<div class="bsi-provenance-body">{body_html}</div>'
            f"</details>"
        )

    return _PROVENANCE_BLOCK_RE.sub(replacer, html)


def _style_step_labels(html: str) -> str:
    """Convert **Step N:** bold markers to pill badges."""
    return _STEP_LABEL_RE.sub(r'<strong class="bsi-step-label">\1</strong>', html)


def _style_score_metrics(html: str) -> str:
    """Promote bold point-score values to larger metric callout spans."""
    return _SCORE_RE.sub(r'<span class="bsi-metric">\1</span>', html)


# ---------------------------------------------------------------------------
# Similar Cases collapsible rows — SCOPED to case-list headings only
# ---------------------------------------------------------------------------
# Matches headings like: "Similar Cases", "Related Cases",
# "Returned Historical Cases", "Historical Cases", "Prior Cases"
# Does NOT match: Investigation Steps, Allegations, plan checklists.

_SIMILAR_SECTION_RE = re.compile(
    r"(<h[23][^>]*>"
    r"(?=[^<]*\bCases?\b)"
    r"(?=[^<]*(?:Similar|Related|Returned|Historical|Prior|Subject\s+History|Overview|came\s+back|found))"
    r"[^<]*</h[23]>)"
    r"(.*?)"
    r"(?=<h[23]>|$)",
    re.IGNORECASE | re.DOTALL,
)

# Pattern A — nested <ul> sub-items (standard markdown2 output)
# e.g.  <li>Case ID: 123 <ul><li>Date: ...</li></ul></li>
_SIMILAR_ITEM_UL_RE = re.compile(
    r"<li>((?:(?!</?[uo]l>).)*?)<ul>(.*?)</ul>\s*</li>",
    re.DOTALL,
)

# Pattern B — <p> with <br/> fields (LLM inline-paragraph style)
# e.g.  <li><p><strong>Case ID:</strong> 123<br/><strong>Date:</strong> ...<br/></p></li>
_SIMILAR_ITEM_P_RE = re.compile(
    r"<li>\s*<p>(.*?)</p>\s*</li>",
    re.DOTALL,
)

# Extracts individual <strong>Label:</strong> Value pairs from a <p><br/> block
_FIELD_RE = re.compile(
    r"<strong>([^<]+?):?\s*</strong>\s*(.*?)(?=\s*<br\s*/?>|\s*<strong>|\s*$)",
    re.DOTALL,
)

_SIMILAR_LIST_RE = re.compile(r"<(ol|ul)>(.*?)</\1>", re.DOTALL)


def _build_case_row_from_ul(counter: int, header_html: str, sub_ul_body: str) -> str:
    """Build collapsible row from nested-<ul> item structure (Pattern A)."""
    return (
        f"<li>"
        f'<details class="bsi-case-item">'
        f'<summary><span class="bsi-case-num">{counter}</span>{header_html.strip()}</summary>'
        f'<div class="bsi-case-body"><ul>{sub_ul_body}</ul></div>'
        f"</details>"
        f"</li>"
    )


def _build_case_row_from_p(counter: int, p_content: str) -> str:
    """
    Build collapsible row from <p><br/> item structure (Pattern B).
    First field (Case ID) becomes the summary header; remaining fields
    become the 2-column body grid.
    """
    # Clean up <br/> whitespace noise
    p_clean = re.sub(r"\s*<br\s*/?>\s*", "\n", p_content).strip()
    fields = _FIELD_RE.findall(p_clean)

    if not fields:
        # Fallback — can't parse, return unchanged plain <li>
        return f"<li><p>{p_content}</p></li>"

    # First field → summary header (typically "Case ID")
    first_label, first_value = fields[0]
    summary_text = f"<strong>{first_label.strip()}:</strong>&nbsp;{first_value.strip()}"

    # Remaining fields → body grid cells
    body_cells = "".join(f"<li><strong>{lbl.strip()}</strong>{val.strip()}</li>" for lbl, val in fields[1:])

    return (
        f"<li>"
        f'<details class="bsi-case-item">'
        f'<summary><span class="bsi-case-num">{counter}</span>{summary_text}</summary>'
        f'<div class="bsi-case-body"><ul>{body_cells}</ul></div>'
        f"</details>"
        f"</li>"
    )


def find_outer_list(html: str) -> Optional[tuple[int, int, str, str]]:
    """
    Finds the first outermost <ol> or <ul> in the HTML block,
    respecting nested lists by counting tag depth.
    Returns: (start_index, end_index, tag_type, list_body)
    """
    # Find the first occurrence of <ol> or <ul>
    match = re.search(r"<(ol|ul)\b[^>]*>", html, re.IGNORECASE)
    if not match:
        return None

    tag_type = match.group(1).lower()  # 'ol' or 'ul'
    start_pos = match.start()

    # We now scan forward to find the matching closing tag
    depth = 0

    tag_re = re.compile(r"</?(ol|ul)\b[^>]*>", re.IGNORECASE)
    for m in tag_re.finditer(html, start_pos):
        tag = m.group(0)
        is_closing = tag.startswith("</")

        if is_closing:
            depth -= 1
            if depth == 0:
                end_pos = m.end()
                # Extract the body (excluding the outer tags)
                list_body = html[match.end() : m.start()]
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
        """Rewrite one matched similar-cases section's list into styled case rows, if it matches Pattern A/B."""
        heading_html = sec_m.group(1)
        section_body = sec_m.group(2)

        # Use find_outer_list helper to locate and parse the outer list
        list_info = find_outer_list(section_body)
        if not list_info:
            return sec_m.group(0)

        start_idx, end_idx, list_tag, list_body = list_info

        # Detect which pattern is present
        has_ul = bool(_SIMILAR_ITEM_UL_RE.search(list_body))
        has_p = bool(_SIMILAR_ITEM_P_RE.search(list_body))

        if not has_ul and not has_p:
            return sec_m.group(0)  # plain list — leave untouched

        counter = [0]

        if has_ul:

            def convert_ul_item(m: re.Match) -> str:
                """Build one numbered case row from a Pattern A (<li><strong>) list item."""
                counter[0] += 1
                return _build_case_row_from_ul(counter[0], m.group(1), m.group(2))

            converted = _SIMILAR_ITEM_UL_RE.sub(convert_ul_item, list_body)
        else:

            def convert_p_item(m: re.Match) -> str:
                """Build one numbered case row from a Pattern B (<li><p> with <br/>) list item."""
                counter[0] += 1
                return _build_case_row_from_p(counter[0], m.group(1))

            converted = _SIMILAR_ITEM_P_RE.sub(convert_p_item, list_body)

        # Construct the new section body with the transformed list
        new_list_html = f'<{list_tag} class="bsi-case-list">{converted}</{list_tag}>'
        new_section_body = section_body[:start_idx] + new_list_html + section_body[end_idx:]

        return f"{heading_html}{new_section_body}"

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
            "cuddled-lists",
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
        f"{BSI_STYLE}\n"
        f'<div class="bsi-content">\n'
        f"{banner}"
        f"{html_body}\n"
        f"</div>\n"
        f"{_JS_DETAILS_FALLBACK}"
    )
