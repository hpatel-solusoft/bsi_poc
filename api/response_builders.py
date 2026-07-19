from utils import html_converter
from typing import Dict, Any, Optional, List
from fastapi import HTTPException
import re


def render_markdown_html(markdown_text: str) -> str:
    """Consolidated markdown-to-BSI-HTML renderer."""
    return html_converter.render_agent_summary(markdown_text)


def render_markdown_html_with_sources(markdown_text: str, provenance_trail: List[dict]) -> str:
    """Render markdown and append data sources when the agent omitted them."""
    text = markdown_text or ""
    if not re.search(r"(?:^|\n)\s*(?:#{1,6}\s*|\*\*\s*)(?:Data\s+Sources|Data\s+Provenance|Provenance)(?:\s*\*\*)?\s*(?:\n|$)", text, re.IGNORECASE):
        text = (
            f"{text.rstrip()}\n\n"
            f"### Data Sources\n{format_provenance_lines(provenance_trail)}"
        )
    return render_markdown_html(text)


def format_provenance_lines(provenance_trail: List[dict]) -> str:
    """Render provenance trail as a clean markdown list, silently skipping empty blocks."""
    if not provenance_trail:
        return "- No external records cited."
    
    lines = []
    valid_blocks_found = False

    for p in provenance_trail:
        sources = p.get("sources", [])
        
        # 1. THE FIX: Silently skip this entire tool block if it has no sources
        if not sources:
            continue
            
        valid_blocks_found = True
        computed_by = p.get("computed_by", "Not available")
        retrieved_at = p.get("retrieved_at", "Not available")
        
        # Clean up the timestamp for display
        display_time = str(retrieved_at).replace("T", " ")[:19] + " UTC" if "T" in str(retrieved_at) else retrieved_at
        
        lines.append(f"**Sources Retrieved ({display_time}):**")
        lines.append("") # Blank line required before Markdown lists
        
        for source in sources:
            lines.append(f"- {source}")
        
        lines.append("") # Blank line required after Markdown lists
        lines.append(f"*(Method: {computed_by})*")
        lines.append("") # Blank line to separate multiple tool calls cleanly
        
    # 2. If the AI ran tools, but ALL of them yielded 0 valid sources:
    if not valid_blocks_found:
        return "- No external records cited."
        
    return "\n".join(lines)


def safe_join(items: List[str], sep: str = ", ") -> str:
    """Join non-empty strings safely; return 'Not available' when empty."""
    clean = [str(i).strip() for i in (items or []) if str(i).strip()]
    return sep.join(clean) if clean else "Not available"

def parse_bsi_section(text: str, header_name: str) -> List[str]:
    """Extract bullet/numbered list items under a markdown section header."""
    if not text:
        return []
    pattern = rf"(?:^|\n)(?:#+\s*)?{header_name}.*?\n(.*?)(?=\n(?:#+\s*)|$)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if not match:
        return []
    items = re.findall(r"(?:^|\n)\s*[-*•\d+.]\s*(.*)", match.group(1))
    return [item.strip() for item in items if item.strip()]


def plan_list_field(plan: dict, key: str) -> List:
    """Return a normalized non-empty list from a plan section field."""
    if not isinstance(plan, dict):
        return []
    raw = plan.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if item is not None and str(item).strip()]


def plan_has_substance(plan: dict) -> bool:
    """True when parsed plan includes at least one actionable section."""
    return any(
        plan_list_field(plan, key)
        for key in ("investigation_steps", "evidence_checklist", "escalation_criteria")
    )

def format_plan_markdown_item(item, index: int = 1) -> str:
    """Render one investigation step, checklist item, or escalation line."""
    if isinstance(item, dict):
        label = (
            item.get("action")
            or item.get("item")
            or item.get("description")
            or item.get("text")
            or ""
        ).strip()
        if not label:
            return ""

        if "action" in item:
            # Investigation step — always show step number.
            # Owner and deadline are Optional; shown only when present
            # (populated during human analyst review).
            step_no = item.get("step", index)
            meta = []
            if item.get("owner"):
                meta.append(f"Owner: {item['owner']}")
            if item.get("deadline_days") is not None:
                meta.append(f"Deadline: {item['deadline_days']} day(s)")
            # AI-16 / Section 8.5: surface where the step came from, and the
            # rule that justifies it when it is rule-derived, so the basis for
            # the recommendation is visible in the rendered plan.
            source = item.get("source")
            if source == "rule_aware":
                source_rule = item.get("source_rule")
                meta.append(f"Source: rule-aware{f' ({source_rule})' if source_rule else ''}")
            elif source == "catalog":
                meta.append("Source: BSI catalogue")
            if item.get("priority"):
                meta.append(f"Priority: {item['priority']}")
            if meta:
                return f"- **Step {step_no}:** {label} ({', '.join(meta)})"
            return f"- **Step {step_no}:** {label}"
        else:
            # Evidence checklist item or other dict — no step number.
            mandatory = item.get("mandatory")
            if mandatory is not None:
                flag = "required" if mandatory else "optional"
                return f"- {label} ({flag})"
            return f"- {label}"

    text = str(item).strip()
    if not text:
        return ""
    return f"- {text}" if not text.startswith("-") else text

def build_plan_summary(
    case_id: str,
    plan: dict,
    case_data: dict,
    provenance_trail: List[dict],
) -> str:
    """Build agent_summary markdown from the same structured plan returned in ai_summary."""
    if not isinstance(plan, dict):
        return f"Plan generation for case {case_id} completed, but no plan payload was returned."

    complaint_intelligence = case_data.get("complaint_intelligence")
    if not isinstance(complaint_intelligence, dict):
        complaint_intelligence = {}

    complaint_summary = complaint_intelligence.get("summary")
    if not isinstance(complaint_summary, dict):
        complaint_summary = {}

    label = complaint_summary.get("complaint_no") or case_id 
    steps = plan_list_field(plan, "investigation_steps")
    checklist = plan_list_field(plan, "evidence_checklist")
    criteria = plan_list_field(plan, "escalation_criteria")
    risk_tier = plan.get("risk_tier") or "Not available"
    fraud_types = safe_join(plan.get("fraud_types", []))
    plan_id = plan.get("plan_id", "Not available")

    lines = [
        f"### Preliminary Investigation Strategy for Case {label}",
        "",
        (
            f"Investigation plan **{plan_id}** for Complaint #{label} "
            f"({risk_tier} risk; fraud types: {fraud_types}). "
            f"This strategy includes {len(steps)} investigation step(s), "
            f"{len(checklist)} evidence checklist item(s), and "
            f"{len(criteria)} escalation criterion/criteria."
        ),
    ]
    if plan.get("escalation_required"):
        lines.append(
            "Management escalation is flagged for this case based on risk tier and escalation criteria."
        )
    lines.append("")

    # Section 8.5: rule-aware recommendations are displayed SEPARATELY from
    # the generic investigation steps, each labelled with the rule that
    # produced it, so an investigator can see the basis for the task rather
    # than a flat undifferentiated list.
    rule_aware_tasks = plan_list_field(plan, "rule_aware_tasks")
    if rule_aware_tasks:
        lines.extend(["#### Rule-Aware Task Recommendations", ""])
        for task in rule_aware_tasks:
            if not isinstance(task, dict):
                continue
            label = str(task.get("task_type") or "").strip()
            if not label:
                continue
            meta = []
            if task.get("priority"):
                meta.append(f"Priority: {task['priority']}")
            if task.get("source_rule"):
                meta.append(f"Rule: {task['source_rule']}")
            if task.get("detects"):
                meta.append(f"Detects: {task['detects']}")
            lines.append(f"- {label}" + (f" ({', '.join(meta)})" if meta else ""))
        lines.append("")

    lines.extend(["#### Investigation Steps", ""])
    if steps:
        for idx, step in enumerate(steps, start=1):
            formatted = format_plan_markdown_item(step, idx)
            if formatted:
                lines.append(formatted)
    else:
        lines.append("- No investigation steps were returned.")
    lines.append("")

    lines.extend(["#### Evidence Checklist", ""])
    if checklist:
        for idx, item in enumerate(checklist, start=1):
            formatted = format_plan_markdown_item(item, idx)
            if formatted:
                lines.append(formatted)
    else:
        lines.append("- No evidence checklist items were returned.")
    lines.append("")

    lines.extend(["#### Escalation Criteria", ""])
    if criteria:
        for idx, item in enumerate(criteria, start=1):
            formatted = format_plan_markdown_item(item, idx)
            if formatted:
                lines.append(formatted)
    else:
        lines.append("- No escalation criteria were returned.")
    lines.append("")

    lines.extend([
        "### Data Sources",
        format_provenance_lines(provenance_trail),
    ])
    return "\n".join(lines)


def resolve_plan_agent_summary(
    assistant_text: str,
    plan: dict,
    case_id: str,
    case_data: dict,
    provenance_trail: List[dict],
) -> str:
    """
    Prefer markdown synthesized from parsed investigation_plan so agent_summary
    cannot contradict ai_summary.investigation_plan. Fall back to LLM prose only
    when no plan sections were parsed.
    """
    if plan_has_substance(plan):
        return build_plan_summary(case_id, plan, case_data, provenance_trail)
    return assistant_text or build_plan_summary(case_id, plan, case_data, provenance_trail)



def build_confidence_summary(rules_fired: Optional[List[dict]]) -> Dict[str, int]:
    """Tally the rules_fired block (Functional Spec A.4 — rule_id, fired,
    confidence, corroborated per entry) into a {high, medium, unresolved}
    count of FIRED rules, for the /intake graph_findings response block."""
    summary = {"high": 0, "medium": 0, "unresolved": 0}
    for entry in (rules_fired or []):
        if not isinstance(entry, dict) or not entry.get("fired"):
            continue
        confidence = str(entry.get("confidence") or "").strip().lower()
        if confidence in summary:
            summary[confidence] += 1
    return summary


def validate_ai_summary_contract(ai_summary: Optional[Dict[str, Any]]) -> None:
    """Validate required v6 ai_summary payload shape for ON-DEMAND requests."""
    if not isinstance(ai_summary, dict):
        raise HTTPException(
            status_code=400,
            detail="ai_summary is required and must be an object.",
        )
    if "investigation" not in ai_summary or not isinstance(ai_summary.get("investigation"), dict):
        raise HTTPException(
            status_code=400,
            detail="ai_summary.investigation is required and must be an object.",
        )
    # v6 session-recovery rule: if provenance_trail is absent, continue with warning
    # and degrade source citations gracefully (no crash).
    if "provenance_trail" in ai_summary and not isinstance(ai_summary.get("provenance_trail"), list):
        raise HTTPException(
            status_code=400,
            detail="ai_summary.provenance_trail must be an array when provided.",
        )