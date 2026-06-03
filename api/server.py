"""
HTTP endpoints for the BSI Fraud Investigation Platform.
Responsibilities: endpoints, CASE_STORE (CS-4), response shaping,
provenance trail extraction and persistence.
Outside its scope: calling appworks_services directly, knowing tool names
beyond what TOOL_TO_SECTION provides.
"""
import json
import logging
import os
import re
import threading
import time
import yaml
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from utils import html_converter
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from semantic_layer.entity_contracts import InvestigationPlan as InvestigationPlanContract
from agent_service.agent_runner import build_similar_cases_prompt
load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="BSI Fraud Investigation Platform")

# -----------------------------------------------------------------------
# CS-4: Case session context — in-memory for POC with no TTL.
# Entries live for the lifetime of the server process.
# Falls back to ai_summary sent in the request body only if the server has restarted.
# ai_summary is a REQUIRED field on all ON-DEMAND requests (v6 spec).
# -----------------------------------------------------------------------

# POC requirement (MD v6): in-memory CS-4 has no TTL.
CASE_STORE_TTL_SECONDS = None


class _TTLStore:
    """
    Drop-in replacement for a plain dict that expires entries after a
    configurable TTL (CS-4 Case Session Context).

    Supports:   key in store  →  TTL-aware __contains__
                store[key]    →  __getitem__ (raises KeyError on expiry)
                store[key]=v  →  __setitem__ (resets TTL)
                store.get()   →  None on miss/expiry
    The stored dict is returned by reference — in-place .update() calls
    mutate it correctly without requiring a separate setter.
    """

    def __init__(self, ttl_seconds: Optional[int]):
        self._data: Dict[str, Dict] = {}
        self._ts:   Dict[str, float] = {}
        self._ttl = ttl_seconds

    # -- TTL helpers --------------------------------------------------

    def _alive(self, key: str) -> bool:
        if key not in self._data:
            return False
        if self._ttl is None:
            return True
        return (time.monotonic() - self._ts.get(key, 0.0)) < self._ttl

    def _evict(self, key: str) -> None:
        self._data.pop(key, None)
        self._ts.pop(key, None)

    def ttl_remaining(self, key: str) -> Optional[float]:
        """Seconds remaining before key expires, or None if not present / no TTL."""
        if not self._alive(key):
            return None
        if self._ttl is None:
            return None
        return max(0.0, self._ttl - (time.monotonic() - self._ts[key]))

    # -- Mapping interface --------------------------------------------

    def __contains__(self, key: str) -> bool:
        if not self._alive(key):
            self._evict(key)
            return False
        return True

    def __getitem__(self, key: str) -> Dict:
        if key not in self:          # triggers TTL check + eviction
            raise KeyError(key)
        return self._data[key]

    def __setitem__(self, key: str, value: Dict) -> None:
        self._data[key] = value
        self._ts[key]   = time.monotonic()

    def get(self, key: str, default=None):
        if key not in self:
            return default
        return self._data[key]


CASE_STORE: _TTLStore = _TTLStore(CASE_STORE_TTL_SECONDS)
COPILOT_HISTORY_STORE: _TTLStore = _TTLStore(CASE_STORE_TTL_SECONDS)

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "../config/manifest.yaml")



def _build_tool_to_section() -> Dict[str, str]:
    """Built dynamically from manifest at startup — never hardcoded."""
    if not os.path.exists(MANIFEST_PATH):
        return {}
    with open(MANIFEST_PATH) as f:
        manifest = yaml.safe_load(f)
    return {
        tool["name"]: tool["section"]
        for tool in manifest.get("tools", [])
        if "section" in tool
    }


TOOL_TO_SECTION = _build_tool_to_section()

# Section-name constants derived from the manifest at startup (NEW-A).
# Avoids hardcoded string literals inside _extract_tool_results — if either
# tool is renamed in manifest.yaml the constant updates automatically on restart.
_SECTION_COMPLAINT_INTEL = TOOL_TO_SECTION.get("verify_case_intake",  "complaint_intelligence")
_SECTION_CONTEXT_ENRICH  = TOOL_TO_SECTION.get("fetch_subject_history", "context_enrichment")
_SIMILAR_CASES_SECTIONS  = {"allegation_types", "similar_cases"}


# -----------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------

def _render_markdown_html(markdown_text: str) -> str:
    """Consolidated markdown-to-BSI-HTML renderer."""
    return html_converter.render_agent_summary(markdown_text)


def _render_markdown_html_with_sources(markdown_text: str, provenance_trail: List[dict]) -> str:
    """Render markdown and append data sources when the agent omitted them."""
    text = markdown_text or ""
    if not re.search(r"(?:^|\n)\s*(?:#{1,6}\s*|\*\*\s*)(?:Data\s+Sources|Data\s+Provenance|Provenance)(?:\s*\*\*)?\s*(?:\n|$)", text, re.IGNORECASE):
        text = (
            f"{text.rstrip()}\n\n"
            f"### Data Sources\n{_format_provenance_lines(provenance_trail)}"
        )
    return _render_markdown_html(text)


def _get_complaint_no(case_data: dict) -> Optional[str]:
    """Extract complaint_no from investigation section (CS-4 or ai_summary)."""
    # case_data usually has 'investigation' at top level from /investigate.
    return (
        case_data.get("investigation", {})
        .get(_SECTION_COMPLAINT_INTEL, {})
        .get("summary", {})
        .get("complaint_no")
    )


def _swap_case_id_for_complaint(text: str, case_id: str, case_data: dict) -> str:
    """Replaces occurrences of internal case_id with business complaint_no."""
    c_no = _get_complaint_no(case_data)
    if c_no and text:
        return text.replace(str(case_id), str(c_no))
    return text or ""


def _find_tool_name_by_call_id(messages: list, call_id: str) -> Optional[str]:
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if tc.get("id") == call_id:
                    return tc["function"]["name"]
    return None


def _extract_tool_results(messages: list) -> dict:
    """CS-3: Build section dict from tool result messages.

    Tool results are stored exactly as the LLM received them.
    No fields are stripped — downstream endpoints (plan, report,
    copilot) and the investigator-visible summary all need the full data.

    Two lightweight normalisations are applied:
      • Empty DOB strings from AppWorks are converted to None.
      • complaint_intelligence gets two convenience fields injected
        (subject_primary_id, fraud_types) so downstream prompts can
        reference them without parsing the nested subjects/allegations tree.
    """
    sections = {}
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tool_name = _find_tool_name_by_call_id(messages, msg["tool_call_id"])
        if tool_name and tool_name in TOOL_TO_SECTION:
            section = TOOL_TO_SECTION[tool_name]
            try:
                data = json.loads(msg["content"])

                # Normalise empty DOB strings from AppWorks
                if section == _SECTION_CONTEXT_ENRICH and isinstance(data, dict):
                    if data.get("dob") == "":
                        data["dob"] = None

                # Inject convenience top-level fields for downstream prompts.
                # subject_primary_id  — used by /plan, , /copilot prompts.
                # fraud_types         — flattened list for the same consumers.
                # Both are derived from data already present in the result;
                # nothing is fabricated.
                if section == _SECTION_COMPLAINT_INTEL and isinstance(data, dict):
                    if "subjects" in data and data["subjects"]:
                        primary = next(
                            (s for s in data["subjects"] if s.get("is_primary_subject")),
                            data["subjects"][0],
                        )
                        data["subject_primary_id"] = primary.get("subject_id")

                    if "allegations" in data and data["allegations"]:
                        ft_set = set()
                        for alg in data["allegations"]:
                            desc = alg.get("allegation_type", {}).get("description")
                            if desc:
                                ft_set.add(desc)
                        data["fraud_types"] = list(ft_set)

                sections[section] = data
            except (json.JSONDecodeError, TypeError):
                sections[section] = msg["content"]
    return sections


def _extract_agent_summary(messages: list) -> str:
    """Return the final assistant text from the last stop turn."""
    # print("*"*50)
    # print(messages)
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            # print("Agent summary extracted:", msg["content"])
            return msg["content"]
    return ""


def _escape_middle_word_underscores(markdown_text: str) -> str:
    """Prevent identifiers like deadline_days from becoming emphasis markup."""
    code_spans = []

    def stash_code_span(match: re.Match) -> str:
        code_spans.append(match.group(0))
        return f"@@CODESPAN{len(code_spans) - 1}@@"

    protected = re.sub(r"`[^`\n]*`", stash_code_span, markdown_text)
    protected = re.sub(r"(?<=\w)_(?=\w)", r"\\_", protected)

    for idx, code_span in enumerate(code_spans):
        protected = protected.replace(f"@@CODESPAN{idx}@@", code_span)
    return protected


def _safe_join(items: List[str], sep: str = ", ") -> str:
    """Join non-empty strings safely; return 'Not available' when empty."""
    clean = [str(i).strip() for i in (items or []) if str(i).strip()]
    return sep.join(clean) if clean else "Not available"


def _extract_json_object_from_text(text: str) -> Optional[dict]:
    """Extract the first valid JSON object from an assistant text response."""
    if not isinstance(text, str) or "{" not in text:
        return None
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        break
    return None


def _format_provenance_lines(provenance_trail: List[dict]) -> str:
    """Render provenance trail as readable markdown lines."""
    if not provenance_trail:
        return "- No provenance entries available."
    lines = []
    for p in provenance_trail:
        # tool = p.get("tool", "unknown_tool")
        computed_by = p.get("computed_by", "Not available")
        retrieved_at = p.get("retrieved_at", "Not available")
        sources = _safe_join(p.get("sources", []), "; ")
        lines.append(
            f"- **{computed_by}** on `{retrieved_at}` from source(s): {sources}."
        )
    return "\n".join(lines)


def _merge_provenance(existing: List[dict], new_entries: List[dict]) -> List[dict]:
    """Merge provenance lists while preserving order and removing duplicates."""
    merged: List[dict] = []
    seen = set()
    for entry in (existing or []) + (new_entries or []):
        key = (
            entry.get("tool"),
            entry.get("retrieved_at"),
            entry.get("computed_by"),
            tuple(entry.get("sources", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged



def _validate_conversation_history(
    conversation_history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """
    Validate client-supplied Copilot history before it can seed server state.

    Only user/assistant turns are accepted. System, tool, or arbitrary fields are
    intentionally rejected because the backend owns case context and tool state.
    """
    if conversation_history is None:
        return []
    if not isinstance(conversation_history, list):
        raise HTTPException(
            status_code=400,
            detail="conversation_history must be an array when provided.",
        )

    validated: List[Dict[str, str]] = []
    expected_roles = ("user", "assistant")
    for idx, message in enumerate(conversation_history):
        if not isinstance(message, dict):
            raise HTTPException(
                status_code=400,
                detail=f"conversation_history[{idx}] must be an object.",
            )
        role = message.get("role")
        content = message.get("content")
        if role not in expected_roles:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"conversation_history[{idx}].role must be 'user' or "
                    "'assistant'."
                ),
            )
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(
                status_code=400,
                detail=f"conversation_history[{idx}].content must be a non-empty string.",
            )
        validated.append({"role": role, "content": content})
    return validated

def _resolve_copilot_history(
    case_id: str,
    conversation_history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """
    Return server-owned Copilot history for case_id.

    Client history is optional and is only used to seed the backend after a
    server restart or first request for the case.
    """
    supplied_history = _validate_conversation_history(conversation_history)
    history_entry = COPILOT_HISTORY_STORE.get(case_id)
    if isinstance(history_entry, dict):
        if history_entry.get("case_id") != case_id:
            raise HTTPException(
                status_code=409,
                detail="Stored conversation history does not match the requested case_id.",
            )
        stored_messages = _validate_conversation_history(history_entry.get("messages", []))
        return stored_messages

    COPILOT_HISTORY_STORE[case_id] = {
        "case_id": case_id,
        "messages": supplied_history,
    }
    return supplied_history


def _store_copilot_turn(case_id: str, question: str, answer: str) -> List[Dict[str, str]]:
    """Append the latest Copilot exchange to the server-owned case history."""
    history_entry = COPILOT_HISTORY_STORE.get(case_id) or {"case_id": case_id, "messages": []}
    if history_entry.get("case_id") != case_id:
        raise HTTPException(
            status_code=409,
            detail="Stored conversation history does not match the requested case_id.",
        )

    messages = _validate_conversation_history(history_entry.get("messages", []))
    messages.extend([
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ])
    COPILOT_HISTORY_STORE[case_id] = {
        "case_id": case_id,
        "messages": messages,
    }
    return messages

def _build_investigation_summary(case_id: str, sections: dict, provenance_trail: List[dict]) -> str:
    """Detailed deterministic summary for /investigate response."""
    complaint = sections.get("complaint_intelligence", {}) if isinstance(sections, dict) else {}
    enrichment = sections.get("context_enrichment", {}) if isinstance(sections, dict) else {}
    similar = sections.get("similar_cases", {}) if isinstance(sections, dict) else {}
    risk = sections.get("risk_assessment", {}) if isinstance(sections, dict) else {}
    plan = sections.get("investigation_plan", {}) if isinstance(sections, dict) else {}
    final_report = sections.get("final_report", {}) if isinstance(sections, dict) else {}

    summary = complaint.get("summary", {}) if isinstance(complaint, dict) else {}
    details = complaint.get("details", {}) if isinstance(complaint, dict) else {}
    subjects = complaint.get("subjects", []) if isinstance(complaint, dict) else []
    primary_subject = next((s for s in subjects if s.get("is_primary_subject")), subjects[0] if subjects else {})
    primary_name = (
        primary_subject.get("details", {}).get("identifier")
        or details.get("identifier_name")
        or "Not recorded in AppWorks"
    )
    fraud_types = complaint.get("fraud_types", [])
    allegations = complaint.get("allegations", [])
    similar_matches = similar.get("matches", []) if isinstance(similar, dict) else []
    triggered = risk.get("risk_indicators", []) if isinstance(risk, dict) else []
    triggered_names = [
        (r.get("rule_name") or r.get("rule_id")) if isinstance(r, dict) else str(r)
        for r in triggered
    ]
    prior_count = enrichment.get("prior_case_count", 0)
    step_count = len(plan.get("investigation_steps", [])) if isinstance(plan, dict) else 0
    report_status = final_report.get("status", "Not generated")

    subject_lines = []
    for subject in subjects:
        label = "PRIMARY SUBJECT" if subject.get("is_primary_subject") else "SECONDARY SUBJECT"
        subject_details = subject.get("details", {}) if isinstance(subject, dict) else {}
        subject_name = (
            " ".join(
                part for part in [
                    subject_details.get("first_name"),
                    subject_details.get("middle_initial"),
                    subject_details.get("last_name"),
                ]
                if part
            )
            or subject_details.get("identifier")
            or "Not recorded in AppWorks"
        )
        subject_lines.append(
            f"{label}: {subject_name} (Role: {subject.get('role', 'Unknown')}, "
            f"Subject ID: {subject.get('subject_id', 'Unknown')})"
        )

    allegation_lines = []
    for idx, item in enumerate(allegations, start=1):
        allegation_lines.append(
            f"{idx}) {item.get('allegation_type', {}).get('description', 'Unknown')} "
            f"(status={item.get('status', 'Unknown')}, received={item.get('date_received', 'Unknown')}, "
            f"agency={item.get('source_agency', {}).get('name', 'Unknown')}, ref={item.get('agency_referral_no', 'Unknown')})"
        )

    similar_fraud_types = _safe_join(fraud_types)
    similar_query = similar.get('query_summary') if isinstance(similar, dict) else ''

    return (
        f"### Investigation Summary for Case {case_id}\n\n"
        f"**Case Background:** Complaint #{summary.get('complaint_no', 'Not available')} "
        f"({summary.get('case_description', 'Not available')}) is currently in "
        f"'{summary.get('destination', 'Not available')}' under team "
        f"'{summary.get('team', 'Not available')}'. Intake source is "
        f"{details.get('source', 'Not available')} with referral "
        f"{details.get('intake_referral_no', 'Not available')}.\n\n"
        f"**Subject Profile:**\n"
        f"{chr(10).join(subject_lines) if subject_lines else 'No subject information recorded.'}\n\n"
        f"**Allegations:** Fraud types linked to this case are {similar_fraud_types}.\n"
        f"{chr(10).join(allegation_lines) if allegation_lines else 'No allegations recorded.'}\n\n"
        f"**Similar Case Analysis:** {similar_query}\n"
        f"Search used allegation types: {similar_fraud_types or 'Not available'}. "
        f"Returned {len(similar_matches)} archive match(es).\n\n"
        f"**Risk Assessment:** Risk score is {risk.get('risk_score', 'Not available')} "
        f"with tier {risk.get('risk_tier', 'Not available')}. Risk Indicators: "
        f"{_safe_join(triggered_names)}.\n\n"
        f"**Downstream Readiness:** Plan step count currently available: {step_count}. "
        f"Final report status: {report_status}.\n\n"
        f"**Data Provenance:**\n{_format_provenance_lines(provenance_trail)}"
    )


def _build_similar_cases_summary(
    case_id: str,
    case_data: dict,
    similar_cases: dict,
    provenance_trail: List[dict],
) -> str:
    """Deterministic visible summary for /similar_cases from the returned contract."""
    if not isinstance(case_data, dict):
        case_data = {}
    if not isinstance(similar_cases, dict):
        similar_cases = {}

    complaint = case_data.get("complaint_intelligence", {}) if isinstance(case_data, dict) else {}
    summary = complaint.get("summary", {}) if isinstance(complaint, dict) else {}
    details = complaint.get("details", {}) if isinstance(complaint, dict) else {}
    fraud_types = complaint.get("fraud_types", []) if isinstance(complaint, dict) else []
    allegations = complaint.get("allegations", []) if isinstance(complaint, dict) else []
    subjects = complaint.get("subjects", []) if isinstance(complaint, dict) else []
    primary_subject = next((s for s in subjects if s.get("is_primary_subject")), subjects[0] if subjects else {})
    primary_details = primary_subject.get("details", {}) if isinstance(primary_subject, dict) else {}
    primary_name = (
        " ".join(
            part for part in [
                primary_details.get("first_name"),
                primary_details.get("middle_initial"),
                primary_details.get("last_name"),
            ]
            if part
        )
        or primary_details.get("identifier")
        or "Not recorded"
    )

    matches = similar_cases.get("matches", [])
    if not isinstance(matches, list):
        matches = []

    lines = [
        f"### Similar Cases for Case {case_id}",
        "",
        f"**Case Background:** Complaint #{summary.get('complaint_no', 'Not available')} "
        f"({summary.get('case_description', 'Not available')}) is currently in "
        f"'{summary.get('destination', 'Not available')}' under team "
        f"'{summary.get('team', 'Not available')}'. Intake source is "
        f"{details.get('source', 'Not recorded')} with referral "
        f"{details.get('intake_referral_no', 'Not recorded')}.",
        "",
        f"**Primary Subject:** {primary_name} (Subject ID: {primary_subject.get('subject_id', 'Not recorded')}).",
        "",
        f"**Fraud Types:** {_safe_join(fraud_types)}.",
        "",
        "**Allegations:**",
    ]

    if allegations:
        for idx, item in enumerate(allegations, start=1):
            lines.append(
                f"{idx}) {item.get('allegation_type', {}).get('description', 'Unknown')} "
                f"(status={item.get('status', 'Unknown')}, received={item.get('date_received', 'Unknown')}, "
                f"agency={item.get('source_agency', {}).get('name', 'Unknown')})."
            )
    else:
        lines.append("No allegations recorded.")

    lines.extend([
        "",
        "**Similar Case Analysis:**",
        "",
        similar_cases.get("query_summary") or "No similar case query summary was returned.",
        "",
        "### Similar Cases",
        "",
    ])

    if not matches:
        lines.append("No similar historical cases were returned.")
    else:
        for idx, match in enumerate(matches, start=1):
            if not isinstance(match, dict):
                continue
            description = match.get("description") or "Not recorded"
            financial = match.get("financial_calculated")
            financial_text = "Not recorded" if financial is None else str(financial)
            lines.extend([
                f"{idx}. **Case ID:** {match.get('case_id', 'Not recorded')}",
                f"   - **Allegation ID:** {match.get('allegation_id') or 'Not recorded'}",
                f"   - **Fraud Type:** {match.get('fraud_type') or 'Not recorded'}",
                f"   - **Date Received:** {match.get('date_received') or 'Not recorded'}",
                f"   - **Summary:** {match.get('summary') or 'Not recorded'}",
                f"   - **Description:** {description}",
                f"   - **Current Investigation Stage:** {match.get('status') or 'Not recorded'}",
                f"   - **Financial Amount:** {financial_text}",
                f"   - **Match Basis:** {match.get('outcome') or 'Not recorded'}",
                "",
            ])

    filters = similar_cases.get("manifest_filters_applied", {})
    if isinstance(filters, dict) and filters:
        lines.extend([
            "### Search Controls",
            "",
            f"- **Returned Matches:** {similar_cases.get('top_n_returned', len(matches))}",
            f"- **Raw Matches Found:** {similar_cases.get('raw_matches_found', 'Not recorded')}",
            f"- **Required Status:** {filters.get('required_status') or 'Not recorded'}",
            f"- **Lookback Years:** {filters.get('similarity_lookback_years') if filters.get('similarity_lookback_years') is not None else 'Not recorded'}",
            f"- **Max Results Per Type:** {filters.get('max_results_per_type') if filters.get('max_results_per_type') is not None else 'Not recorded'}",
            "",
        ])

    lines.extend([
        "### Data Sources",
        _format_provenance_lines(provenance_trail),
    ])
    return "\n".join(lines)


def _parse_bsi_section(text: str, header_name: str) -> List[str]:
    """Extract bullet/numbered list items under a markdown section header."""
    if not text:
        return []
    pattern = rf"(?:^|\n)(?:#+\s*)?{header_name}.*?\n(.*?)(?=\n(?:#+\s*)|$)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if not match:
        return []
    items = re.findall(r"(?:^|\n)\s*[-*•\d+.]\s*(.*)", match.group(1))
    return [item.strip() for item in items if item.strip()]


def _plan_list_field(plan: dict, key: str) -> List:
    """Return a normalized non-empty list from a plan section field."""
    if not isinstance(plan, dict):
        return []
    raw = plan.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if item is not None and str(item).strip()]


def _plan_has_substance(plan: dict) -> bool:
    """True when parsed plan includes at least one actionable section."""
    return any(
        _plan_list_field(plan, key)
        for key in ("investigation_steps", "evidence_checklist", "escalation_criteria")
    )

def _format_plan_markdown_item(item, index: int = 1) -> str:
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

def _complaint_label(case_id: str, context: dict) -> str:
    """Prefer business complaint number over internal workfolder id for display."""
    intel = context.get("complaint_intelligence") if isinstance(context, dict) else None
    if isinstance(intel, dict):
        summary = intel.get("summary", {})
        if isinstance(summary, dict) and summary.get("complaint_no") is not None:
            return str(summary["complaint_no"])
    c_no = _get_complaint_no(context) if isinstance(context, dict) else None
    return str(c_no) if c_no is not None else str(case_id)


def _build_plan_summary(
    case_id: str,
    plan: dict,
    case_data: dict,
    provenance_trail: List[dict],
) -> str:
    """Build agent_summary markdown from the same structured plan returned in ai_summary."""
    if not isinstance(plan, dict):
        return f"Plan generation for case {case_id} completed, but no plan payload was returned."

    label = _complaint_label(case_id, case_data)
    steps = _plan_list_field(plan, "investigation_steps")
    checklist = _plan_list_field(plan, "evidence_checklist")
    criteria = _plan_list_field(plan, "escalation_criteria")
    risk_tier = plan.get("risk_tier") or "Not available"
    fraud_types = _safe_join(plan.get("fraud_types", []))
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

    lines.extend(["#### Investigation Steps", ""])
    if steps:
        for idx, step in enumerate(steps, start=1):
            formatted = _format_plan_markdown_item(step, idx)
            if formatted:
                lines.append(formatted)
    else:
        lines.append("- No investigation steps were returned.")
    lines.append("")

    lines.extend(["#### Evidence Checklist", ""])
    if checklist:
        for idx, item in enumerate(checklist, start=1):
            formatted = _format_plan_markdown_item(item, idx)
            if formatted:
                lines.append(formatted)
    else:
        lines.append("- No evidence checklist items were returned.")
    lines.append("")

    lines.extend(["#### Escalation Criteria", ""])
    if criteria:
        for idx, item in enumerate(criteria, start=1):
            formatted = _format_plan_markdown_item(item, idx)
            if formatted:
                lines.append(formatted)
    else:
        lines.append("- No escalation criteria were returned.")
    lines.append("")

    lines.extend([
        "### Data Sources",
        _format_provenance_lines(provenance_trail),
    ])
    return "\n".join(lines)


def _resolve_plan_agent_summary(
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
    if _plan_has_substance(plan):
        return _build_plan_summary(case_id, plan, case_data, provenance_trail)
    return assistant_text or _build_plan_summary(case_id, plan, case_data, provenance_trail)

def _risk_rule_lookup(case_data: dict) -> dict:
    rules_section = case_data.get("risk_rules", {})
    rules = rules_section.get("active_rules", []) if isinstance(rules_section, dict) else []
    lookup = {}
    for rule in rules:
        desc = rule.get("description")
        rule_id = rule.get("rule_id")
        if desc:
            lookup[desc] = rule
        if rule_id:
            lookup[rule_id] = rule
    return lookup


def _format_subjects_from_context(case_data: dict) -> str:
    complaint = case_data.get("complaint_intelligence", {})
    enrichment = case_data.get("context_enrichment", {})
    subjects = complaint.get("subjects", []) if isinstance(complaint, dict) else []
    prior_cases = enrichment.get("prior_cases", []) if isinstance(enrichment, dict) else []
    prior_case_count = enrichment.get("prior_case_count", len(prior_cases))
    primary_id = complaint.get("subject_primary_id") if isinstance(complaint, dict) else None

    lines = []
    for subject in subjects:
        details = subject.get("details", {})
        subject_id = subject.get("subject_id")
        is_primary = subject.get("is_primary_subject") or subject_id == primary_id
        label = "PRIMARY" if is_primary else "SECONDARY"
        full_name = " ".join(
            part for part in [
                details.get("first_name"),
                details.get("middle_initial"),
                details.get("last_name"),
            ]
            if part
        ) or details.get("identifier") or "Not recorded in AppWorks"
        identifier = (
            details.get("ssn")
            or details.get("ein")
            or details.get("identifier")
            or "Not recorded in AppWorks"
        )
        dob = details.get("dob") or "Not recorded in AppWorks"
        aliases = subject.get("alias_records") or details.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]

        address_parts = []
        for address in subject.get("addresses", []):
            address_parts.append(", ".join(
                part for part in [
                    address.get("address"),
                    address.get("apt_suite"),
                    address.get("city"),
                    address.get("state"),
                    address.get("zipcode"),
                    address.get("county"),
                ]
                if part
            ))

        line = (
            f"[{label}] {full_name} (Role: {subject.get('role') or 'Subject'}, "
            f"Type: {subject.get('subject_type') or details.get('subject_type') or 'Not recorded in AppWorks'}, "
            f"Subject ID: {subject_id or 'Not recorded in AppWorks'}). "
            f"Identifier: {identifier}. DOB: {dob}. "
            f"Addresses: {'; '.join(address_parts) if address_parts else 'Not recorded in AppWorks'}. "
        )
        if aliases:
            line += f"Aliases: {', '.join(aliases)}. "
        if is_primary:
            case_refs = [
                str(pc.get("mapping_title") or pc.get("workfolder_id"))
                for pc in prior_cases
                if pc.get("mapping_title") or pc.get("workfolder_id")
            ]
            line += (
                f"Prior case count: {prior_case_count}. "
                f"Prior case references: {'; '.join(case_refs) if case_refs else 'Not recorded in AppWorks'}."
            )
        lines.append(line)
    return " | ".join(lines)


def _format_risk_from_context(case_data: dict) -> str:
    risk = case_data.get("risk_assessment", {})
    lookup = _risk_rule_lookup(case_data)
    triggered = risk.get("risk_indicators", []) if isinstance(risk, dict) else []
    parts = []
    for item in triggered:
        key = item.get("rule_id") if isinstance(item, dict) else item
        rule = lookup.get(key, {})
        rule_id = rule.get("rule_id") or key
        desc = rule.get("description")
        if not desc and isinstance(item, dict):
            desc = item.get("rule_name")
        if not desc:
            desc = key
        condition = rule.get("condition")
        parts.append(
            f"{rule_id}: {desc}" + (f" ({condition})" if condition else "")
        )
    return (
        f"Risk Score: {risk.get('risk_score')} | Risk Tier: {risk.get('risk_tier')} | "
        f"Risk Indicators: {'; '.join(parts) if parts else 'None'} | "
        "Computed by BSI configured rules evaluation."
    )


def _format_recommended_actions(case_data: dict, plan_data: dict) -> str:
    risk = case_data.get("risk_assessment", {})
    recommendation = risk.get("recommendations") if isinstance(risk, dict) else None
    steps = plan_data.get("investigation_steps", []) if isinstance(plan_data, dict) else []
    escalation_required = plan_data.get("escalation_required") if isinstance(plan_data, dict) else False
    action_bits = []
    if recommendation:
        action_bits.append(recommendation)
    if escalation_required:
        action_bits.append("Escalation is required by the investigation plan.")
    if steps:
        action_bits.append("Next plan actions: " + "; ".join(step.get("action", "") for step in steps[:3] if step.get("action")))
    return " ".join(action_bits) or "No recommended actions recorded in the verified investigation context."


def _enrich_final_report_from_context(
    sections: dict,
    case_data: dict,
    plan_data: dict,
    analyst_decision: Optional[dict],
) -> dict:
    final_report = sections.get("final_report")
    if not isinstance(final_report, dict):
        return sections

    report_sections = final_report.setdefault("sections", {})
    subject_text = _format_subjects_from_context(case_data)
    if subject_text:
        report_sections["subject_history"] = subject_text
    if case_data.get("risk_assessment"):
        report_sections["risk_assessment"] = _format_risk_from_context(case_data)
    report_sections["investigation_plan_summary"] = {
        "plan_id": plan_data.get("plan_id"),
        "risk_tier": plan_data.get("risk_tier"),
        "step_count": len(plan_data.get("investigation_steps", [])),
        "escalation_required": plan_data.get("escalation_required", False),
        "mandatory_evidence_count": len([
            item for item in plan_data.get("evidence_checklist", [])
            if item.get("mandatory")
        ]),
    }
    report_sections["analyst_decision"] = analyst_decision or {}
    report_sections["recommended_actions"] = _format_recommended_actions(case_data, plan_data)
    return {"final_report": final_report}


def _get_runner():
    from agent_service.agent_runner import BSIAgentRunner
    return BSIAgentRunner(MANIFEST_PATH)


# -----------------------------------------------------------------------
# CS-4 RE-HYDRATION CONTRACT (v6)
# ai_summary is REQUIRED on every /copilot, /plan,  request.
# Server uses CS-4 if warm. Falls back to this field if CS-4 is cold
# (restart / TTL expiry). Frontend NEVER omits ai_summary to optimise
# payload size — the server decides which source to use, not the client.
#
# ai_summary MUST include both "investigation" AND "provenance_trail"
# from the original /investigate response. Omitting provenance_trail
# silently breaks Copilot source citations on session recovery.
# -----------------------------------------------------------------------


def _rehydrate_case_store(case_id: str, ai_summary: dict) -> None:
    """Re-populate CS-4 from request body on session recovery (v6)."""
    sections = {**ai_summary.get("investigation", {})}
    
    # Also pull top-level on-demand sections (persistence rule)
    for key in ["similar_cases", "risk_rules", "risk_assessment", "investigation_plan"]:
        if key in ai_summary:
            sections[key] = ai_summary[key]

    CASE_STORE[case_id] = {
        **sections,
        "provenance_trail": ai_summary.get("provenance_trail", []),
    }
    if not ai_summary.get("provenance_trail"):
        logger.warning(
            f"CS-4 re-hydrated for {case_id} but provenance_trail is missing "
            f"— Copilot source citations will be unavailable for this session."
        )

def _validate_ai_summary_contract(ai_summary: Optional[Dict[str, Any]]) -> None:
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


def _normalize_plan_payload(raw_plan: Optional[Dict[str, Any]], case_data: dict) -> Optional[Dict[str, Any]]:
    """Accept multiple plan payload shapes and normalize to investigation_plan dict."""
    plan_data = raw_plan or case_data.get("investigation_plan")
    if not isinstance(plan_data, dict):
        return None
    if "investigation_plan" in plan_data and isinstance(plan_data["investigation_plan"], dict):
        return plan_data["investigation_plan"]
    if "investigation" in plan_data and isinstance(plan_data["investigation"], dict):
        nested = plan_data["investigation"].get("investigation_plan")
        return nested if isinstance(nested, dict) else None
    # already a plain plan shape
    if "plan_id" in plan_data or "investigation_steps" in plan_data:
        return plan_data
    return None


def _resolve_case_store(case_id: str, ai_summary: Optional[Dict[str, Any]]) -> dict:
    """
    CS-4 lookup pattern used by all ON-DEMAND handlers.
    Prioritizes ai_summary from request body as the absolute source of truth (v6).
    Updates CASE_STORE for persistence but always returns the fresh data from the request.
    """
    if ai_summary:
        _validate_ai_summary_contract(ai_summary)
        
        # Build fresh case_data from input
        case_data = {**ai_summary.get("investigation", {})}
        
        # Pull top-level on-demand sections
        for key in ["similar_cases", "risk_rules", "risk_assessment", "investigation_plan"]:
            if key in ai_summary:
                case_data[key] = ai_summary[key]
        
        case_data["provenance_trail"] = ai_summary.get("provenance_trail", [])
        
        # Update persistence store
        CASE_STORE[case_id] = case_data
        return case_data

    # Fallback to store only if request body is empty
    if case_id in CASE_STORE and CASE_STORE[case_id]:
        return CASE_STORE[case_id]

    raise HTTPException(
        status_code=400,
        detail=f"Case {case_id} session data not found. Provide ai_summary in request body."
    )


# -----------------------------------------------------------------------
# Request / response models
# -----------------------------------------------------------------------

class InvestigateRequest(BaseModel):
    case_id: str


class SimilarCasesRequest(BaseModel):
    case_id: str
    # ai_summary is REQUIRED per v6 spec — frontend always sends it.
    # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
    ai_summary: Dict[str, Any]


class PlanRequest(BaseModel):
    case_id: str
    # ai_summary is REQUIRED per v6 spec — frontend always sends it.
    # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
    ai_summary: Dict[str, Any]


class ReportRequest(BaseModel):
    case_id: str
    # ai_summary is REQUIRED per v6 spec.
    # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
    ai_summary: Dict[str, Any]
    ai_case_summary: Optional[str] = None
    ai_plan: Optional[Dict[str, Any]] = None
    analyst_decision: Optional[Dict[str, Any]] = None


class CopilotRequest(BaseModel):
    case_id: str
    question: str
    # ai_summary is REQUIRED per v6 spec — frontend always sends it.
    # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
    ai_summary: Dict[str, Any]
    conversation_history: Optional[List[Dict[str, Any]]] = None
    # Human-approved investigation plan, written by an analyst via the Modify Strategy flow.
    # When present, the copilot prompt treats these steps as authoritative over the AI-generated ones.
    # Schema: { "source": "human_approved", "steps": [...], "comment": "...", "modified_on": "...", "modified_by": "..." }
    modified_ai_investigation_plan: Optional[Dict[str, Any]] = None


def _find_tool_name_by_call_id(messages: list, call_id: str) -> Optional[str]:
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if tc.get("id") == call_id:
                    return tc["function"]["name"]
    return None


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/investigate")
def investigate(req: InvestigateRequest):
    """
    AUTO flow — Section 3.1.
    Runs AUTO tools 1-2 (intake, enrichment) in dependency order
    (LLM decides sequence). Similar cases runs via /similar_cases.
    Populates CS-4 CASE_STORE for all subsequent on-demand calls.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
    """
    start = time.time()
    try:
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")
        if not os.path.exists(MANIFEST_PATH):
            raise HTTPException(status_code=500, detail="manifest.yaml not found")

        runner = _get_runner()
        # Scope to intake + enrichment only; similar cases is a separate route.
        investigate_tools = [
            tool
            for tool in runner.auto_tools
            if tool["function"]["name"] in {"verify_case_intake", "fetch_subject_history"}
        ]
        messages, provenance_trail, _ = runner.investigate(
            case_id=req.case_id,
            tools=investigate_tools,
        )
        sections = _extract_tool_results(messages)

        # CS-4: populate store with all sections + provenance.
        CASE_STORE[req.case_id] = {**sections, "provenance_trail": provenance_trail}

        # ── Response split (v6 spec) ────────────────────────────────────────
        # ai_summary: the contract object passed to the next route in the flow.
        # Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
        # details: human-readable narrative + meta — NOT required by downstream.
        # ──────────────────────────────────────────────────────────────────────
        ai_summary = {
            "investigation":    sections,
            "provenance_trail": provenance_trail,
        }

        # BSI requirement: swap internal case_id for business complaint_no in narrative
        summary_text = _swap_case_id_for_complaint(
            _extract_agent_summary(messages),
            req.case_id,
            ai_summary,
        )

        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to /similar_cases
            "details": {
                "agent_summary": _render_markdown_html_with_sources(summary_text, provenance_trail),
                "meta": {
                    "tool_calls_made":  len(provenance_trail),
                    "duration_seconds": round(time.time() - start, 1),
                },
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Investigate route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Investigation failed: {exc}") from exc
    finally:
        logger.info("POST /investigate completed for case_id=%s", req.case_id)



@app.post("/similar_cases")
def similar_cases(req: SimilarCasesRequest):
    """
    ON-DEMAND — Similar Cases Route (Step 2 in flow).
    Calls search_similar_cases to find historical cases with matching fraud patterns.
    Requires case_data from a prior /investigate run (via CS-4 or ai_summary body).
    Explains historical case matches, pattern relevance, and archive findings.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
    """
    

    try:
        _validate_ai_summary_contract(req.ai_summary)
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
        runner = _get_runner()
        logger.info(f"[DEBUG] on_demand_tools: {[t['function']['name'] for t in runner.on_demand_tools]}")
        logger.info(f"[DEBUG] all_tools: {[t['function']['name'] for t in runner.all_tools]}") 
        # logger.info(f"[DEBUG] _sc_names: {_sc_names}")
        _sc_names = {
            name
            for sec in _SIMILAR_CASES_SECTIONS
            for name in runner.dispatcher.section_index.get(sec, [])
        }
        similar_tools = [
            tool
            for tool in runner.on_demand_tools
            if tool["function"]["name"] in _sc_names
        ]

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_similar_cases_prompt(case_data),
            user_message=(
                f"Review the case data for case {req.case_id} and execute the "
                "appropriate tools to search for similar historical cases and explain "
                "the pattern matches found."
            ),
            tools=similar_tools,
            trigger="ON-DEMAND",
        )

        sections = _extract_tool_results(messages)
        # print("11111111111111111111111111")
        # print(sections)
        agent_summary = _extract_agent_summary(messages)
        
        similar_cases_data = sections.get("similar_cases", {})
        similar_section = {
            "similar_cases": similar_cases_data
        }

        merged_provenance = _merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
        )

        # Update CS-4 but return only the route-specific section.
        CASE_STORE[req.case_id].update(similar_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        # ai_summary: updated contract — investigation sections with similar cases.
        # Pass this object to /risk_assessment.
        investigation_data = {**case_data}
        investigation_data.pop("provenance_trail", None)
        investigation_data.pop("similar_cases", None) # Dedup
        
        # Carry over other sections from store
        investigation_plan = investigation_data.pop("investigation_plan", None)
        risk_rules = investigation_data.pop("risk_rules", None)
        risk_assessment = investigation_data.pop("risk_assessment", None)

        ai_summary = {
            "investigation":    investigation_data,
        }
        if similar_cases_data is not None: ai_summary["similar_cases"] = similar_cases_data
        
        if risk_rules is not None:
            ai_summary["risk_rules"] = risk_rules
        if risk_assessment is not None:
            ai_summary["risk_assessment"] = risk_assessment
        ai_summary["provenance_trail"] = merged_provenance
        if investigation_plan is not None:
            ai_summary["investigation_plan"] = investigation_plan
        # print(req.case_id)
        # print(case_data)
        # print(similar_cases_data)
        # print(merged_provenance)
        summary_text = _build_similar_cases_summary(
            req.case_id,
            case_data,
            similar_cases_data,
            merged_provenance,
        )
        logger.info(f"SIMILAR CASES NARRATIVE LENGTH: {len(summary_text)}") 
        logger.info(f"SIMILAR CASES NARRATIVE TAIL: {summary_text[-500:]}")
        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to /risk_assessment
            "details": {
                "agent_summary": _render_markdown_html(agent_summary),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Similar cases route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Similar cases analysis failed: {exc}") from exc
    finally:
        logger.info("POST /similar_cases completed for case_id=%s", req.case_id)


@app.post("/risk_assessment")
def risk_assessment(req: PlanRequest):
    """
    ON-DEMAND — Risk Assessment Route (Step 3 in flow).
    Calls get_risk_rules and calculate_risk_metrics.
    Requires case_data from a prior /investigate + /similar_cases run
    (via CS-4 or ai_summary body).
    Explains case seriousness, triggered rules, and escalation thresholds.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
    """
    from agent_service.agent_runner import build_risk_assessment_prompt

    try:
        _validate_ai_summary_contract(req.ai_summary)
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
        runner = _get_runner()
        risk_tools = [
            tool
            for tool in runner.on_demand_tools
            if tool["function"]["name"] in {"get_risk_rules", "calculate_risk_metrics"}
        ]

        # --- EXPLICIT DEPENDENCY INJECTION ---
        # We package the backend state into a generic execution_context
        execution_context = {"ai_summary": req.ai_summary}
        # -------------------------------------

        messages, new_provenance, tool_call_log = runner.run_scoped(
            system_prompt=build_risk_assessment_prompt(case_data),
            user_message=(
                f"Review the case data for case {req.case_id} and execute the "
                "appropriate tools to calculate the risk assessment and explain why "
                "this case received its risk score."
            ),
            tools=risk_tools,
            trigger="ON-DEMAND",
            execution_context=execution_context
        )

        sections = _extract_tool_results(messages)
        risk_rules = sections.get("risk_rules", {})
        risk_assessment = sections.get("risk_assessment", {})
        if not isinstance(risk_assessment, dict) or "risk_score" not in risk_assessment:
            called_tools = [
                entry.get("tool")
                for entry in tool_call_log
                if isinstance(entry, dict) and entry.get("status") == "ok"
            ]
            raise RuntimeError(
                "Risk assessment did not complete because calculate_risk_metrics "
                f"did not return a score. Successful tools: {called_tools}"
            )
        # Normalize recommendation text: rename singular "recommendation" to plural "recommendations"
        assistant_text = _extract_agent_summary(messages)
        rec_text = None
        try:
            if isinstance(risk_assessment, dict):
                # Extract from either singular or plural field
                rec_text = risk_assessment.get("recommendation") or risk_assessment.get("recommendations")
                # Remove the singular field to avoid duplication
                risk_assessment.pop("recommendation", None)
        except Exception:
            rec_text = None

        if not rec_text and isinstance(assistant_text, str):
            # attempt to parse a recommendation section from assistant markdown
            m = re.search(
                r"(?:^|\n)#{1,6}\s*(?:Recommended Action|Recommendation|Recommendations)\s*\n(.*?)(?=\n#{1,6}\s|\Z)",
                assistant_text,
                re.DOTALL | re.IGNORECASE,
            )
            if m:
                rec_text = m.group(1).strip()

        if rec_text and isinstance(risk_assessment, dict):
            risk_assessment["recommendations"] = rec_text

        if isinstance(risk_assessment, dict):
            if "recommendations" not in risk_assessment:
                risk_assessment["recommendations"] = ""
        else:
            risk_assessment = {"recommendations": ""}
        risk_section = {
            "risk_rules":      risk_rules,
            "risk_assessment": risk_assessment
        }

        merged_provenance = _merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
        )

        # Update CS-4 but return only the route-specific section.
        CASE_STORE[req.case_id].update(risk_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        # ai_summary: updated contract — investigation sections with risk assessment.
        # Pass this object to /plan.
        # ai_summary: updated contract — investigation sections with risk assessment.
        # Pass this object to /plan.
        investigation_data = {**case_data}
        investigation_data.pop("provenance_trail", None)
        investigation_data.pop("risk_assessment", None) # Dedup
        investigation_data.pop("risk_rules", None)      # Dedup
        
        # Carry over other sections from store
        similar_cases = investigation_data.pop("similar_cases", None)
        investigation_plan = investigation_data.pop("investigation_plan", None)
        
        ai_summary = {
            "investigation":   investigation_data,
        }
        if similar_cases is not None: ai_summary["similar_cases"] = similar_cases
        
        ai_summary.update({
            "risk_rules":      risk_rules,
            "risk_assessment": risk_assessment,
            "provenance_trail": merged_provenance,
        })
        if investigation_plan is not None:
            ai_summary["investigation_plan"] = investigation_plan

        summary_text = _swap_case_id_for_complaint(
            _extract_agent_summary(messages), req.case_id, ai_summary
        )

        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to /plan
            "details": {
                "agent_summary": _render_markdown_html_with_sources(summary_text, merged_provenance),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Risk assessment route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Risk assessment failed: {exc}") from exc
    finally:
        logger.info("POST /risk_assessment completed for case_id=%s", req.case_id)


@app.post("/plan")
def plan(req: PlanRequest):
    """
    ON-DEMAND — Plan Route (Step 4 in flow).
    Calls get_investigation_plan only.
    Requires risk_tier from prior /risk_assessment run (via CS-4 or ai_summary body).
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
    """
    from agent_service.agent_runner import build_plan_prompt

    try:
        _validate_ai_summary_contract(req.ai_summary)
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
        runner = _get_runner()
        # Scope to plan retrieval only (Step 4)
        plan_tools = [
            tool
            for tool in runner.on_demand_tools
            if tool["function"]["name"] in {"get_investigation_plan"}
        ]
        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_plan_prompt(case_data),
            user_message=(
                f"Review the investigation context for case {req.case_id} and execute the "
                "appropriate on-demand tool to retrieve the investigation plan."
            ),
            tools=plan_tools,
            trigger="ON-DEMAND",
        )

        sections = _extract_tool_results(messages)
        investigation_plan = sections.get("investigation_plan", {})

        assistant_text = _extract_agent_summary(messages)

        # Parse markdown prose into structured fields (same source used for agent_summary)
        steps = _parse_bsi_section(assistant_text, "Investigation Steps")
        checklist = _parse_bsi_section(assistant_text, "Evidence Checklist")
        criteria = _parse_bsi_section(assistant_text, "Escalation Criteria")

        # Convert parsed strings to typed dicts.
        # 'owner' and 'deadline_days' are intentionally absent —
        # they are populated during the human analyst review step.
        steps_dicts     = [{"step": i + 1, "action": s} for i, s in enumerate(steps)]     if steps     else None
        checklist_dicts = [{"item": s}                  for s in checklist]                 if checklist else None
        # Build structured plan from parsed prose
        # Start with metadata from tool result if available
        plan_result = sections.get("investigation_plan", {})
        
        import re
        id_match = re.search(r"Case\s*(?:ID|#)?\s*[:\s]*(\d+)", assistant_text, re.I)
        cid = id_match.group(1) if id_match else req.case_id
        plan_id = plan_result.get("plan_id") or f"PLAN-{cid}-{datetime.now().strftime('%Y%m%d')}"

        investigation_plan = {
            "plan_id":             plan_id,
            "fraud_types":         ...,
            "risk_tier":           ...,
            "investigation_steps": steps_dicts,
            "evidence_checklist":  checklist_dicts,
            "escalation_criteria": criteria or None,
            "escalation_required": ...
        }

        try:
           
            validated_plan = InvestigationPlanContract(**investigation_plan)
            investigation_plan = validated_plan.model_dump(exclude_none=True)
        except Exception as e:
            logger.warning(
                f"Investigation plan schema validation failed — storing unvalidated: {e}"
            )


        plan_section = {
            "investigation_plan": investigation_plan
        }

        merged_provenance = _merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
        )

        # Update CS-4 but return only the route-specific section.
        CASE_STORE[req.case_id].update(plan_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        # ai_summary: updated contract — investigation sections separate from plan.
        # Pass this object to  and /copilot.
        investigation_data = {**case_data}
        investigation_data.pop("provenance_trail", None)
        investigation_data.pop("investigation_plan", None) # Dedup
        
        # Carry over other sections from store
        similar_cases = investigation_data.pop("similar_cases", None)
        risk_rules = investigation_data.pop("risk_rules", None)
        risk_assessment = investigation_data.pop("risk_assessment", None)

        ai_summary = {
            "investigation":   investigation_data,
        }
        if similar_cases is not None: ai_summary["similar_cases"] = similar_cases
        
        ai_summary.update({
            "risk_rules":              risk_rules,
            "risk_assessment":         risk_assessment,
            "investigation_plan":  investigation_plan,
            "provenance_trail":        merged_provenance,
        })

        summary_text = _resolve_plan_agent_summary(
            assistant_text,
            investigation_plan,
            req.case_id,
            case_data,
            merged_provenance,
        )
        summary_text = _swap_case_id_for_complaint(summary_text, req.case_id, ai_summary)

        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to , /copilot
            "details": {
                "agent_summary": _render_markdown_html_with_sources(
                    assistant_text,
                    merged_provenance,
                ),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Plan route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {exc}") from exc
    finally:
        logger.info("POST /plan completed for case_id=%s", req.case_id)

@app.post("/copilot")
def copilot(req: CopilotRequest):
    """
    ON-DEMAND — Copilot Route (Step 5 in flow, alongside /report).
    Answers investigator questions grounded in case context (CS-5).
    Answers from CS-4 context first; falls back to tools only if needed.
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    If provenance_trail is absent from ai_summary, source citations degrade
    gracefully — no crash.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | /report
    """
    try:
        from agent_service.agent_runner import build_copilot_prompt
        print(req)
        print(req.ai_summary)
        print(req.modified_ai_investigation_plan)
        _validate_ai_summary_contract(req.ai_summary)
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary
        cs4_warm = req.case_id in CASE_STORE
        case_data = _resolve_case_store(req.case_id, req.ai_summary)

        # If the frontend has supplied a human-approved investigation plan, merge it
        # into case_data so the copilot prompt's precedence rule can act on it.
        if req.modified_ai_investigation_plan:
            case_data["modified_ai_investigation_plan"] = req.modified_ai_investigation_plan

        conversation_history = _resolve_copilot_history(
            req.case_id,
            req.conversation_history,
        )

        runner = _get_runner()

        messages, new_provenance_trail, tool_call_log = runner.run_scoped(
            system_prompt=build_copilot_prompt(req.case_id, case_data),
            user_message=req.question,
            conversation_history=conversation_history,
        )

        answer = _extract_agent_summary(messages)
        updated_conversation_history = _store_copilot_turn(
            req.case_id,
            req.question,
            answer,
        )

        # sources_cited: include the stored provenance trail from CS-4 (so context-
        # grounded answers cite the original AppWorks sources) plus any new tool
        # calls made during this copilot turn.
        # This aligns with Section 3.4 where the response shows sources from the
        # original investigation even when tool_calls_made = 0.
        stored_provenance = case_data.get("provenance_trail", [])
        combined_provenance = _merge_provenance(stored_provenance, new_provenance_trail)

        sources_cited = [
            # f"{p['tool']} — {p.get('computed_by', '')} — "
            f"retrieved {p.get('retrieved_at', '')}"
            for p in combined_provenance
        ]
        sources_cited_details = [
            {
                # "tool": p.get("tool", ""),
                "computed_by": p.get("computed_by", ""),
                "retrieved_at": p.get("retrieved_at", ""),
                "sources": p.get("sources", []),
            }
            for p in combined_provenance
        ]

        # CS-4: Update store only if the case entry still exists (it may have
        # been evicted if TTL expires between _resolve_case_store and here).
        if new_provenance_trail and req.case_id in CASE_STORE:
            new_sections = _extract_tool_results(messages)
            CASE_STORE[req.case_id].update(new_sections)
            CASE_STORE[req.case_id]["provenance_trail"] = combined_provenance

        return {
            "answer":               _render_markdown_html(
                _swap_case_id_for_complaint(answer, req.case_id, case_data)
            ),
            "sources_cited":        sources_cited,
            "sources_cited_details": sources_cited_details,
            "provenance_trail":     combined_provenance,
            "conversation_history":  updated_conversation_history,
            # "tool_calls_made":      len(new_provenance_trail),
            "cs4_source":           "warm" if cs4_warm else "rehydrated",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Copilot route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Copilot failed: {exc}") from exc
    finally:
        logger.info("POST /copilot completed for case_id=%s", req.case_id)
