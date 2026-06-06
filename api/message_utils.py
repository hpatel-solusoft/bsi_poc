from config.settings import  TOP_LEVEL_SECTIONS
import json
from typing import List, Dict, Optional

_SECTION_COMPLAINT_INTEL = "complaint_intelligence"
_SECTION_CONTEXT_ENRICH  = "context_enrichment"


def find_tool_name_by_call_id(messages: list, call_id: str) -> Optional[str]:
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if tc.get("id") == call_id:
                    return tc["function"]["name"]
    return None


def extract_tool_results(messages: list,  tool_section_map: Dict[str, str]) -> dict:
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
        tool_name = find_tool_name_by_call_id(messages, msg["tool_call_id"])
        if tool_name and tool_name in tool_section_map:       # ← uses parameter
            section = tool_section_map[tool_name] 
            try:
                data = json.loads(msg["content"])

             
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


def merge_provenance(existing: List[dict], new_entries: List[dict]) -> List[dict]:
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

def extract_agent_summary(messages: list) -> str:
    """Return the final assistant text from the last stop turn."""
    # print("*"*50)
    # print(messages)
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            # print("Agent summary extracted:", msg["content"])
            return msg["content"]
    return ""




def build_ai_summary(
    case_data: dict,
    new_sections: dict,
    provenance_trail: List[dict],
) -> dict:
    """
    Build the ai_summary contract object passed between routes.

    Separates investigation sections (complaint_intelligence,
    context_enrichment) from top-level sections (similar_cases,
    risk_assessment, etc.). Carries over existing top-level sections
    from case_data, replacing any keys in new_sections with the
    freshly computed values from this route.

    case_data       — flat CS-4 dict from _resolve_case_store
    new_sections    — sections produced by this route
                      e.g. {"similar_cases": similar_cases_data}
    provenance_trail — merged provenance for this route's response
    """
    # Separate: investigation sections stay nested, top-level sections float up
    investigation_data = {
        k: v for k, v in case_data.items()
        if k not in TOP_LEVEL_SECTIONS
    }

    ai_summary: dict = {"investigation": investigation_data}

    # Carry over existing top-level sections from prior routes
    for key in ("similar_cases", "risk_rules", "risk_assessment", "investigation_plan"):
        existing = case_data.get(key)
        if existing is not None:
            ai_summary[key] = existing

    # Override/add sections produced by this route
    ai_summary.update(new_sections)

    # Provenance always set last — merged trail is authoritative
    ai_summary["provenance_trail"] = provenance_trail

    return ai_summary
