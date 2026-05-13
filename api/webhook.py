# """
# HTTP endpoints for the BSI Fraud Investigation Platform.
# Responsibilities: endpoints, CASE_STORE (CS-4), response shaping,
# provenance trail extraction and persistence.
# Outside its scope: calling appworks_services directly, knowing tool names
# beyond what TOOL_TO_SECTION provides.
# """
# import json
# import logging
# import os
# import time
# import yaml
# from datetime import datetime, timezone
# from typing import Dict, List, Optional, Any
# import markdown2 
# from fastapi import FastAPI, HTTPException
# from pydantic import BaseModel
# from dotenv import load_dotenv

# load_dotenv()

# logger = logging.getLogger(__name__)

# app = FastAPI(title="BSI Fraud Investigation Platform")

# # -----------------------------------------------------------------------
# # CS-4: Case session context — in-memory for POC with no TTL.
# # Entries live for the lifetime of the server process.
# # Falls back to ai_summary sent in the request body only if the server has restarted.
# # ai_summary is a REQUIRED field on all ON-DEMAND requests (v6 spec).
# # -----------------------------------------------------------------------

# # POC requirement (MD v6): in-memory CS-4 has no TTL.
# CASE_STORE_TTL_SECONDS = None


# class _TTLStore:
#     """
#     Drop-in replacement for a plain dict that expires entries after a
#     configurable TTL (CS-4 Case Session Context).

#     Supports:   key in store  →  TTL-aware __contains__
#                 store[key]    →  __getitem__ (raises KeyError on expiry)
#                 store[key]=v  →  __setitem__ (resets TTL)
#                 store.get()   →  None on miss/expiry
#     The stored dict is returned by reference — in-place .update() calls
#     mutate it correctly without requiring a separate setter.
#     """

#     def __init__(self, ttl_seconds: Optional[int]):
#         self._data: Dict[str, Dict] = {}
#         self._ts:   Dict[str, float] = {}
#         self._ttl = ttl_seconds

#     # -- TTL helpers --------------------------------------------------

#     def _alive(self, key: str) -> bool:
#         if key not in self._data:
#             return False
#         if self._ttl is None:
#             return True
#         return (time.monotonic() - self._ts.get(key, 0.0)) < self._ttl

#     def _evict(self, key: str) -> None:
#         self._data.pop(key, None)
#         self._ts.pop(key, None)

#     def ttl_remaining(self, key: str) -> Optional[float]:
#         """Seconds remaining before key expires, or None if not present / no TTL."""
#         if not self._alive(key):
#             return None
#         if self._ttl is None:
#             return None
#         return max(0.0, self._ttl - (time.monotonic() - self._ts[key]))

#     # -- Mapping interface --------------------------------------------

#     def __contains__(self, key: str) -> bool:
#         if not self._alive(key):
#             self._evict(key)
#             return False
#         return True

#     def __getitem__(self, key: str) -> Dict:
#         if key not in self:          # triggers TTL check + eviction
#             raise KeyError(key)
#         return self._data[key]

#     def __setitem__(self, key: str, value: Dict) -> None:
#         self._data[key] = value
#         self._ts[key]   = time.monotonic()

#     def get(self, key: str, default=None):
#         if key not in self:
#             return default
#         return self._data[key]


# CASE_STORE: _TTLStore = _TTLStore(CASE_STORE_TTL_SECONDS)

# MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "../config/manifest.yaml")


# def _build_tool_to_section() -> Dict[str, str]:
#     """Built dynamically from manifest at startup — never hardcoded."""
#     if not os.path.exists(MANIFEST_PATH):
#         return {}
#     with open(MANIFEST_PATH) as f:
#         manifest = yaml.safe_load(f)
#     return {
#         tool["name"]: tool["section"]
#         for tool in manifest.get("tools", [])
#         if "section" in tool
#     }


# TOOL_TO_SECTION = _build_tool_to_section()


# # -----------------------------------------------------------------------
# # Internal helpers
# # -----------------------------------------------------------------------

# def _find_tool_name_by_call_id(messages: list, call_id: str) -> Optional[str]:
#     for msg in messages:
#         if msg.get("role") == "assistant":
#             for tc in msg.get("tool_calls", []):
#                 if tc.get("id") == call_id:
#                     return tc["function"]["name"]
#     return None


# def _extract_tool_results(messages: list) -> dict:
#     """CS-3: Build section dict from tool result messages.

#     Tool results are stored exactly as the LLM received them.
#     No fields are stripped — downstream endpoints (playbook, report,
#     copilot) and the investigator-visible summary all need the full data.

#     Two lightweight normalisations are applied:
#       • Empty DOB strings from AppWorks are converted to None.
#       • complaint_intelligence gets two convenience fields injected
#         (subject_primary_id, fraud_types) so downstream prompts can
#         reference them without parsing the nested subjects/allegations tree.
#     """
#     sections = {}
#     for msg in messages:
#         if msg.get("role") != "tool":
#             continue
#         tool_name = _find_tool_name_by_call_id(messages, msg["tool_call_id"])
#         if tool_name and tool_name in TOOL_TO_SECTION:
#             section = TOOL_TO_SECTION[tool_name]
#             try:
#                 data = json.loads(msg["content"])

#                 # Normalise empty DOB strings from AppWorks
#                 if section == "context_enrichment" and isinstance(data, dict):
#                     if data.get("dob") == "":
#                         data["dob"] = None

#                 # Inject convenience top-level fields for downstream prompts.
#                 # subject_primary_id  — used by /playbook, /report, /copilot prompts.
#                 # fraud_types         — flattened list for the same consumers.
#                 # Both are derived from data already present in the result;
#                 # nothing is fabricated.
#                 if section == "complaint_intelligence" and isinstance(data, dict):
#                     if "subjects" in data and data["subjects"]:
#                         primary = next(
#                             (s for s in data["subjects"] if s.get("is_primary_subject")),
#                             data["subjects"][0],
#                         )
#                         data["subject_primary_id"] = primary.get("subject_id")

#                     if "allegations" in data and data["allegations"]:
#                         ft_set = set()
#                         for alg in data["allegations"]:
#                             desc = alg.get("allegation_type", {}).get("description")
#                             if desc:
#                                 ft_set.add(desc)
#                         data["fraud_types"] = list(ft_set)

#                 sections[section] = data
#             except (json.JSONDecodeError, TypeError):
#                 sections[section] = msg["content"]
#     return sections


# def _extract_agent_summary(messages: list) -> str:
#     """Return the final assistant text from the last stop turn."""
#     # print("*"*50)
#     # print(messages)
#     for msg in reversed(messages):
#         if msg.get("role") == "assistant" and msg.get("content"):
#             # print("Agent summary extracted:", msg["content"])
#             return msg["content"]
#     return ""


# def _safe_join(items: List[str], sep: str = ", ") -> str:
#     """Join non-empty strings safely; return 'Not available' when empty."""
#     clean = [str(i).strip() for i in (items or []) if str(i).strip()]
#     return sep.join(clean) if clean else "Not available"


# def _format_provenance_lines(provenance_trail: List[dict]) -> str:
#     """Render provenance trail as readable markdown lines."""
#     if not provenance_trail:
#         return "- No provenance entries available."
#     lines = []
#     for p in provenance_trail:
#         # tool = p.get("tool", "unknown_tool")
#         computed_by = p.get("computed_by", "Not available")
#         retrieved_at = p.get("retrieved_at", "Not available")
#         sources = _safe_join(p.get("sources", []), "; ")
#         lines.append(
#             f"- {computed_by}` on `{retrieved_at}` from source(s): {sources}."
#         )
#     return "\n".join(lines)


# def _merge_provenance(existing: List[dict], new_entries: List[dict]) -> List[dict]:
#     """Merge provenance lists while preserving order and removing duplicates."""
#     merged: List[dict] = []
#     seen = set()
#     for entry in (existing or []) + (new_entries or []):
#         key = (
#             entry.get("tool"),
#             entry.get("retrieved_at"),
#             entry.get("computed_by"),
#             tuple(entry.get("sources", [])),
#         )
#         if key in seen:
#             continue
#         seen.add(key)
#         merged.append(entry)
#     return merged


# def _build_investigation_summary(case_id: str, sections: dict, provenance_trail: List[dict]) -> str:
#     """Detailed deterministic summary for /investigate response."""
#     complaint = sections.get("complaint_intelligence", {}) if isinstance(sections, dict) else {}
#     enrichment = sections.get("context_enrichment", {}) if isinstance(sections, dict) else {}
#     similar = sections.get("similar_cases", {}) if isinstance(sections, dict) else {}
#     risk = sections.get("risk_assessment", {}) if isinstance(sections, dict) else {}
#     playbook = sections.get("investigation_playbook", {}) if isinstance(sections, dict) else {}
#     final_report = sections.get("final_report", {}) if isinstance(sections, dict) else {}

#     summary = complaint.get("summary", {}) if isinstance(complaint, dict) else {}
#     details = complaint.get("details", {}) if isinstance(complaint, dict) else {}
#     subjects = complaint.get("subjects", []) if isinstance(complaint, dict) else []
#     primary_subject = next((s for s in subjects if s.get("is_primary_subject")), subjects[0] if subjects else {})
#     primary_name = (
#         primary_subject.get("details", {}).get("identifier")
#         or details.get("identifier_name")
#         or "Not recorded in AppWorks"
#     )
#     co_subjects = [s.get("details", {}).get("identifier") for s in subjects if not s.get("is_primary_subject")]
#     fraud_types = complaint.get("fraud_types", [])
#     allegations = complaint.get("allegations", [])
#     similar_matches = similar.get("matches", []) if isinstance(similar, dict) else []
#     triggered = risk.get("risk_indicators", []) if isinstance(risk, dict) else []
#     triggered_names = [
#         (r.get("rule_name") or r.get("rule_id")) if isinstance(r, dict) else str(r)
#         for r in triggered
#     ]
#     prior_count = enrichment.get("prior_case_count", 0)
#     recommendation = risk.get("recommendation", "No recommendation recorded.")
#     step_count = len(playbook.get("investigation_steps", [])) if isinstance(playbook, dict) else 0
#     report_status = final_report.get("status", "Not generated")
#     allegation_lines = []
#     for idx, item in enumerate(allegations, start=1):
#         allegation_lines.append(
#             f"{idx}) {item.get('allegation_type', {}).get('description', 'Unknown')} "
#             f"(status={item.get('status', 'Unknown')}, received={item.get('date_received', 'Unknown')}, "
#             f"agency={item.get('source_agency', {}).get('name', 'Unknown')}, ref={item.get('agency_referral_no', 'Unknown')})"
#         )

#     return (
#         f"### Investigation Summary for Case {case_id}\n\n"
#         f"**Case Background:** Complaint #{summary.get('complaint_no', 'Not available')} "
#         f"({summary.get('case_description', 'Not available')}) is currently in "
#         f"'{summary.get('destination', 'Not available')}' under team "
#         f"'{summary.get('team', 'Not available')}'. Intake source is "
#         f"{details.get('source', 'Not available')} with referral "
#         f"{details.get('intake_referral_no', 'Not available')}.\n\n"
#         f"**Subject Profile:** Primary subject is {primary_name}. Co-subjects recorded: "
#         f"{_safe_join(co_subjects)}. The primary subject currently has {prior_count} prior case(s) in history.\n\n"
#         f"**Allegation Details:** Fraud types linked to this case are {_safe_join(fraud_types)}. "
#         f"Recorded allegations: {_safe_join(allegation_lines, ' | ')}.\n\n"
#         f"**Similar Case Analysis:** {_safe_join([similar.get('query_summary', '')])} "
#         f"with {len(similar_matches)} match(es) returned from archive checks.\n\n"
#         f"**Risk Assessment:** Risk score is {risk.get('risk_score', 'Not available')} "
#         f"with tier {risk.get('risk_tier', 'Not available')}. Risk Indicators: "
#         f"{_safe_join(triggered_names)}. Recommendation from rules evaluation: {recommendation}\n\n"
#         f"**Downstream Readiness:** Playbook step count currently available: {step_count}. "
#         f"Final report status: {report_status}.\n\n"
#         f"**Data Provenance:**\n{_format_provenance_lines(provenance_trail)}"
#     )


# def _build_playbook_summary(case_id: str, playbook: dict, provenance_trail: List[dict]) -> str:
#     """Detailed deterministic summary for /playbook response."""
#     if not isinstance(playbook, dict):
#         return f"Playbook generation for case {case_id} completed, but no playbook payload was returned."
#     steps = playbook.get("investigation_steps", [])
#     owners = sorted({s.get("owner") for s in steps if isinstance(s, dict) and s.get("owner")})
#     mandatory = len([
#         item for item in playbook.get("evidence_checklist", [])
#         if isinstance(item, dict) and item.get("mandatory")
#     ])
#     step_lines = []
#     for step in steps:
#         step_lines.append(
#             f"Step {step.get('step', '?')}: {step.get('action', 'Not available')} "
#             f"(owner={step.get('owner', 'Not available')}, "
#             f"deadline_days={step.get('deadline_days', 'Not available')})"
#         )
#     return (
#         f"### Investigation Playbook for Case {case_id}\n\n"
#         f"Playbook `{playbook.get('playbook_id', 'Not available')}` was generated for risk tier "
#         f"{playbook.get('risk_tier', 'Not available')} and fraud types "
#         f"{_safe_join(playbook.get('fraud_types', []))}. "
#         f"The workflow includes {len(steps)} investigation step(s): {_safe_join(step_lines, ' | ')}. "
#         f"Mandatory evidence item count is {mandatory}, escalation_required is "
#         f"{playbook.get('escalation_required', False)}, and owner group(s) involved are "
#         f"{_safe_join(owners)}.\n\n"
#         f"**Data Provenance:**\n{_format_provenance_lines(provenance_trail)}"
#     )


# def _build_report_summary(case_id: str, final_report: dict, case_data: dict, provenance_trail: List[dict]) -> str:
#     """Detailed deterministic summary for /report response."""
#     sections = final_report.get("sections", {}) if isinstance(final_report, dict) else {}
#     complaint = case_data.get("complaint_intelligence", {}) if isinstance(case_data, dict) else {}
#     risk = case_data.get("risk_assessment", {}) if isinstance(case_data, dict) else {}
#     summary = complaint.get("summary", {}) if isinstance(complaint, dict) else {}
#     recommendation = risk.get("recommendation", "No recommendation recorded.")
#     return (
#         f"### Final Report Summary for Case {case_id}\n\n"
#         f"Report `{final_report.get('report_id', 'Not available')}` generated with status "
#         f"{final_report.get('status', 'Not available')}. "
#         f"Complaint #{summary.get('complaint_no', 'Not available')} "
#         f"({summary.get('case_description', 'Not available')}). "
#         f"Risk score {risk.get('risk_score', 'Not available')} / {risk.get('risk_tier', 'Not available')}. "
#         f"Recommended action: {recommendation}. "
#         f"The final report includes {len(sections)} populated section(s), covering case summary, "
#         f"subject history, allegation summary, financial summary, risk rationale, recommended actions, "
#         f"playbook roll-up, and analyst decision status.\n\n"
#         f"**Data Provenance:**\n{_format_provenance_lines(provenance_trail)}"
#     )


# def _risk_rule_lookup(case_data: dict) -> dict:
#     rules_section = case_data.get("risk_rules", {})
#     rules = rules_section.get("rules", []) if isinstance(rules_section, dict) else []
#     lookup = {}
#     for rule in rules:
#         desc = rule.get("description")
#         rule_id = rule.get("rule_id")
#         if desc:
#             lookup[desc] = rule
#         if rule_id:
#             lookup[rule_id] = rule
#     return lookup


# def _format_subjects_from_context(case_data: dict) -> str:
#     complaint = case_data.get("complaint_intelligence", {})
#     enrichment = case_data.get("context_enrichment", {})
#     subjects = complaint.get("subjects", []) if isinstance(complaint, dict) else []
#     prior_cases = enrichment.get("prior_cases", []) if isinstance(enrichment, dict) else []
#     prior_case_count = enrichment.get("prior_case_count", len(prior_cases))
#     primary_id = complaint.get("subject_primary_id") if isinstance(complaint, dict) else None

#     lines = []
#     for subject in subjects:
#         details = subject.get("details", {})
#         subject_id = subject.get("subject_id")
#         is_primary = subject.get("is_primary_subject") or subject_id == primary_id
#         label = "PRIMARY" if is_primary else "SECONDARY"
#         full_name = " ".join(
#             part for part in [
#                 details.get("first_name"),
#                 details.get("middle_initial"),
#                 details.get("last_name"),
#             ]
#             if part
#         ) or details.get("identifier") or "Not recorded in AppWorks"
#         identifier = (
#             details.get("ssn")
#             or details.get("ein")
#             or details.get("identifier")
#             or "Not recorded in AppWorks"
#         )
#         dob = details.get("dob") or "Not recorded in AppWorks"
#         aliases = subject.get("alias_records") or details.get("aliases") or []
#         if isinstance(aliases, str):
#             aliases = [a.strip() for a in aliases.split(",") if a.strip()]

#         address_parts = []
#         for address in subject.get("addresses", []):
#             address_parts.append(", ".join(
#                 part for part in [
#                     address.get("address"),
#                     address.get("apt_suite"),
#                     address.get("city"),
#                     address.get("state"),
#                     address.get("zipcode"),
#                     address.get("county"),
#                 ]
#                 if part
#             ))

#         line = (
#             f"[{label}] {full_name} (Role: {subject.get('role') or 'Subject'}, "
#             f"Type: {subject.get('subject_type') or details.get('subject_type') or 'Not recorded in AppWorks'}, "
#             f"Subject ID: {subject_id or 'Not recorded in AppWorks'}). "
#             f"Identifier: {identifier}. DOB: {dob}. "
#             f"Addresses: {'; '.join(address_parts) if address_parts else 'Not recorded in AppWorks'}. "
#         )
#         if aliases:
#             line += f"Aliases: {', '.join(aliases)}. "
#         if is_primary:
#             case_refs = [
#                 str(pc.get("mapping_title") or pc.get("workfolder_id"))
#                 for pc in prior_cases
#                 if pc.get("mapping_title") or pc.get("workfolder_id")
#             ]
#             line += (
#                 f"Prior case count: {prior_case_count}. "
#                 f"Prior case references: {'; '.join(case_refs) if case_refs else 'Not recorded in AppWorks'}."
#             )
#         lines.append(line)
#     return " | ".join(lines)


# def _format_risk_from_context(case_data: dict) -> str:
#     risk = case_data.get("risk_assessment", {})
#     lookup = _risk_rule_lookup(case_data)
#     triggered = risk.get("risk_indicators", []) if isinstance(risk, dict) else []
#     parts = []
#     for item in triggered:
#         key = item.get("rule_id") if isinstance(item, dict) else item
#         rule = lookup.get(key, {})
#         rule_id = rule.get("rule_id") or key
#         desc = rule.get("description")
#         if not desc and isinstance(item, dict):
#             desc = item.get("rule_name")
#         if not desc:
#             desc = key
#         condition = rule.get("condition")
#         parts.append(
#             f"{rule_id}: {desc}" + (f" ({condition})" if condition else "")
#         )
#     return (
#         f"Risk Score: {risk.get('risk_score')} | Risk Tier: {risk.get('risk_tier')} | "
#         f"Risk Indicators: {'; '.join(parts) if parts else 'None'} | "
#         "Computed by BSI configured rules evaluation."
#     )


# def _format_recommended_actions(case_data: dict, playbook_data: dict) -> str:
#     risk = case_data.get("risk_assessment", {})
#     recommendation = risk.get("recommendation") if isinstance(risk, dict) else None
#     steps = playbook_data.get("investigation_steps", []) if isinstance(playbook_data, dict) else []
#     escalation_required = playbook_data.get("escalation_required") if isinstance(playbook_data, dict) else False
#     action_bits = []
#     if recommendation:
#         action_bits.append(recommendation)
#     if escalation_required:
#         action_bits.append("Escalation is required by the investigation playbook.")
#     if steps:
#         action_bits.append("Next playbook actions: " + "; ".join(step.get("action", "") for step in steps[:3] if step.get("action")))
#     return " ".join(action_bits) or "No recommended actions recorded in the verified investigation context."


# def _enrich_final_report_from_context(
#     sections: dict,
#     case_data: dict,
#     playbook_data: dict,
#     analyst_decision: Optional[dict],
# ) -> dict:
#     final_report = sections.get("final_report")
#     if not isinstance(final_report, dict):
#         return sections

#     report_sections = final_report.setdefault("sections", {})
#     subject_text = _format_subjects_from_context(case_data)
#     if subject_text:
#         report_sections["subject_history"] = subject_text
#     if case_data.get("risk_assessment"):
#         report_sections["risk_assessment"] = _format_risk_from_context(case_data)
#     report_sections["investigation_playbook_summary"] = {
#         "playbook_id": playbook_data.get("playbook_id"),
#         "risk_tier": playbook_data.get("risk_tier"),
#         "step_count": len(playbook_data.get("investigation_steps", [])),
#         "escalation_required": playbook_data.get("escalation_required", False),
#         "mandatory_evidence_count": len([
#             item for item in playbook_data.get("evidence_checklist", [])
#             if item.get("mandatory")
#         ]),
#     }
#     report_sections["analyst_decision"] = analyst_decision or {}
#     report_sections["recommended_actions"] = _format_recommended_actions(case_data, playbook_data)
#     return {"final_report": final_report}


# def _get_runner():
#     from agent_service.agent_runner import BSIAgentRunner
#     return BSIAgentRunner(MANIFEST_PATH)


# # -----------------------------------------------------------------------
# # CS-4 RE-HYDRATION CONTRACT (v6)
# # ai_summary is REQUIRED on every /copilot, /playbook, /report request.
# # Server uses CS-4 if warm. Falls back to this field if CS-4 is cold
# # (restart / TTL expiry). Frontend NEVER omits ai_summary to optimise
# # payload size — the server decides which source to use, not the client.
# #
# # ai_summary MUST include both "investigation" AND "provenance_trail"
# # from the original /investigate response. Omitting provenance_trail
# # silently breaks Copilot source citations on session recovery.
# # -----------------------------------------------------------------------


# def _rehydrate_case_store(case_id: str, ai_summary: dict) -> None:
#     """Re-populate CS-4 from request body on session recovery (v6)."""
#     CASE_STORE[case_id] = {
#         **ai_summary.get("investigation", {}),           # all tool result sections
#         "provenance_trail": ai_summary.get("provenance_trail", []),  # must be present
#     }
#     if not ai_summary.get("provenance_trail"):
#         logger.warning(
#             f"CS-4 re-hydrated for {case_id} but provenance_trail is missing "
#             f"— Copilot source citations will be unavailable for this session."
#         )


# def _validate_ai_summary_contract(ai_summary: Optional[Dict[str, Any]]) -> None:
#     """Validate required v6 ai_summary payload shape for ON-DEMAND requests."""
#     if not isinstance(ai_summary, dict):
#         raise HTTPException(
#             status_code=400,
#             detail="ai_summary is required and must be an object.",
#         )
#     if "investigation" not in ai_summary or not isinstance(ai_summary.get("investigation"), dict):
#         raise HTTPException(
#             status_code=400,
#             detail="ai_summary.investigation is required and must be an object.",
#         )
#     # v6 session-recovery rule: if provenance_trail is absent, continue with warning
#     # and degrade source citations gracefully (no crash).
#     if "provenance_trail" in ai_summary and not isinstance(ai_summary.get("provenance_trail"), list):
#         raise HTTPException(
#             status_code=400,
#             detail="ai_summary.provenance_trail must be an array when provided.",
#         )


# def _normalize_playbook_payload(raw_playbook: Optional[Dict[str, Any]], case_data: dict) -> Optional[Dict[str, Any]]:
#     """Accept multiple playbook payload shapes and normalize to investigation_playbook dict."""
#     playbook_data = raw_playbook or case_data.get("investigation_playbook")
#     if not isinstance(playbook_data, dict):
#         return None
#     if "investigation_playbook" in playbook_data and isinstance(playbook_data["investigation_playbook"], dict):
#         return playbook_data["investigation_playbook"]
#     if "investigation" in playbook_data and isinstance(playbook_data["investigation"], dict):
#         nested = playbook_data["investigation"].get("investigation_playbook")
#         return nested if isinstance(nested, dict) else None
#     # already a plain playbook shape
#     if "playbook_id" in playbook_data or "investigation_steps" in playbook_data:
#         return playbook_data
#     return None


# def _resolve_case_store(case_id: str, ai_summary: Optional[Dict[str, Any]]) -> dict:
#     """
#     CS-4 lookup pattern used by all ON-DEMAND handlers.
#     Returns warm case_data from CS-4, or re-hydrates from ai_summary if cold.
#     Raises HTTPException if neither source is available.
#     """
#     if case_id in CASE_STORE and CASE_STORE[case_id]:
#         return CASE_STORE[case_id]

#     # CS-4 cold — fall back to ai_summary sent in request body (v6 contract)
#     if ai_summary:
#         _validate_ai_summary_contract(ai_summary)
#         _rehydrate_case_store(case_id, ai_summary)
#         return CASE_STORE[case_id]

#     raise HTTPException(
#         status_code=400,
#         detail=(
#             "No investigation data available for this case. "
#             "Run POST /investigate first, or provide ai_summary "
#             "(with investigation sections and provenance_trail) in the request body."
#         ),
#     )


# # -----------------------------------------------------------------------
# # Request / response models
# # -----------------------------------------------------------------------

# class InvestigateRequest(BaseModel):
#     case_id: str


# class PlaybookRequest(BaseModel):
#     case_id: str
#     # ai_summary is REQUIRED per v6 spec — frontend always sends it.
#     # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
#     ai_summary: Dict[str, Any]


# class ReportRequest(BaseModel):
#     case_id: str
#     # ai_summary is REQUIRED per v6 spec.
#     # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
#     ai_summary: Dict[str, Any]
#     ai_case_summary: Optional[str] = None
#     ai_playbook: Optional[Dict[str, Any]] = None
#     analyst_decision: Optional[Dict[str, Any]] = None


# class CopilotRequest(BaseModel):
#     case_id: str
#     question: str
#     # ai_summary is REQUIRED per v6 spec — frontend always sends it.
#     # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
#     ai_summary: Dict[str, Any]
#     conversation_history: Optional[List[Dict[str, Any]]] = None


# # -----------------------------------------------------------------------
# # Endpoints
# # -----------------------------------------------------------------------

# @app.get("/health")
# def health():
#     return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# @app.post("/investigate")
# def investigate(req: InvestigateRequest):
#     """
#     AUTO flow — Section 3.1.
#     Runs tools 1-5 in dependency order (LLM decides sequence).
#     Populates CS-4 CASE_STORE for all subsequent on-demand calls.
#     """
#     start = time.time()
#     try:
#         if not os.getenv("OPENAI_API_KEY"):
#             raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")
#         if not os.path.exists(MANIFEST_PATH):
#             raise HTTPException(status_code=500, detail="manifest.yaml not found")

#         runner = _get_runner()
#         messages, provenance_trail, _ = runner.investigate(case_id=req.case_id)
#         # print("-"*50)
#         # print(messages)
#         sections = _extract_tool_results(messages)

#         # CS-4: populate store with all sections + provenance.
#         CASE_STORE[req.case_id] = {**sections, "provenance_trail": provenance_trail}

#         # ── Response split (v6 spec) ────────────────────────────────────────
#         # ai_summary: the contract object passed as-is to /playbook, /report,
#         #   /copilot. Contains only what downstream routes need.
#         # details: human-readable narrative + meta — NOT required by downstream.
#         # ──────────────────────────────────────────────────────────────────────
#         ai_summary = {
#             "investigation":    sections,
#             "provenance_trail": provenance_trail,
#         }
#         # print("="*50)
#         # print(messages)
#         return {
#             "case_id":    req.case_id,
#             "status":     "completed",
#             "ai_summary": ai_summary,          # ← pass this object to /playbook, /report, /copilot
#             "details": {
#                 "agent_summary": markdown2.markdown(_extract_agent_summary(messages)),
#                 "meta": {
#                     "tool_calls_made":  len(provenance_trail),
#                     "duration_seconds": round(time.time() - start, 1),
#                 },
#             },
#         }
#     except HTTPException:
#         raise
#     except Exception as exc:
#         logger.exception("Investigate route failed for case_id=%s", req.case_id)
#         raise HTTPException(status_code=500, detail=f"Investigation failed: {exc}") from exc
#     finally:
#         logger.info("POST /investigate completed for case_id=%s", req.case_id)


# @app.post("/playbook")
# def playbook(req: PlaybookRequest):
#     """
#     ON-DEMAND — Section 3.2.
#     Calls get_investigation_playbook only.
#     Requires risk_tier from a prior /investigate run (via CS-4 or ai_summary body).
#     ai_summary is REQUIRED per v6 spec — server decides which source to use.
#     """
#     from agent_service.agent_runner import build_playbook_prompt

#     try:
#         _validate_ai_summary_contract(req.ai_summary)
#         # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
#         case_data = _resolve_case_store(req.case_id, req.ai_summary)
#         runner = _get_runner()

#         messages, new_provenance, _ = runner.run_scoped(
#             system_prompt=build_playbook_prompt(case_data),
#             user_message=(
#                 f"Review the investigation context for case {req.case_id} and execute the "
#                 "appropriate on-demand tool to retrieve the investigation playbook."
#             ),
#         )

#         sections = _extract_tool_results(messages)
#         investigation_playbook = sections.get("investigation_playbook", {})
#         playbook_section = {
#             "investigation_playbook": investigation_playbook
#         }

#         merged_provenance = _merge_provenance(
#             case_data.get("provenance_trail", []),
#             new_provenance,
#         )

#         # Update CS-4 but return only the route-specific section.
#         CASE_STORE[req.case_id].update(playbook_section)
#         CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

#         # ai_summary: updated contract — investigation sections separate from playbook.
#         # Pass this object to /report and /copilot.
#         investigation_data = {**case_data}
#         investigation_data.pop("provenance_trail", None)
#         investigation_data.pop("investigation_playbook", None)
#         ai_summary = {
#             "investigation":           investigation_data,
#             "investigation_playbook": investigation_playbook,
#             "provenance_trail":        merged_provenance,
#         }

#         return {
#             "case_id":    req.case_id,
#             "status":     "completed",
#             "ai_summary": ai_summary,          # ← pass this object to /report, /copilot
#             "details": {
#                 "agent_summary": markdown2.markdown(_build_playbook_summary(
#                     req.case_id,
#                     playbook_section.get("investigation_playbook", {}),
#                     merged_provenance,
#                 )),
#             },
#         }
#     except HTTPException:
#         raise
#     except Exception as exc:
#         logger.exception("Playbook route failed for case_id=%s", req.case_id)
#         raise HTTPException(status_code=500, detail=f"Playbook generation failed: {exc}") from exc
#     finally:
#         logger.info("POST /playbook completed for case_id=%s", req.case_id)


# @app.post("/report")
# def report(req: ReportRequest):
#     """
#     ON-DEMAND — Section 3.3.
#     Calls generate_final_report with all six v6 params:
#       case_id, subject_id, fraud_types, risk_score, risk_tier, risk_indicators.
#     Requires: risk_assessment in case data, playbook, and analyst approval.
#     ai_summary is REQUIRED per v6 spec — server decides which source to use.
#     """
#     from agent_service.agent_runner import build_report_prompt

#     try:
#         _validate_ai_summary_contract(req.ai_summary)
#         # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
#         case_data = _resolve_case_store(req.case_id, req.ai_summary)
#         if "risk_assessment" not in case_data:
#             raise HTTPException(
#                 status_code=400,
#                 detail="risk_assessment section missing — run /investigate first.",
#             )

#         playbook_data = _normalize_playbook_payload(req.ai_playbook, case_data)
#         if not playbook_data:
#             raise HTTPException(status_code=400, detail="Playbook required — run /playbook first.")
#         if not req.analyst_decision or req.analyst_decision.get("decision") != "APPROVED":
#             raise HTTPException(
#                 status_code=400,
#                 detail="Report requires analyst approval. analyst_decision.decision must be 'APPROVED'.",
#             )

#         runner = _get_runner()
#         messages, new_provenance, _ = runner.run_scoped(
#             system_prompt=build_report_prompt(
#                 case_id=req.case_id,
#                 case_data=case_data,
#                 ai_case_summary=req.ai_case_summary or "",
#                 playbook_data=playbook_data,
#                 analyst_decision=req.analyst_decision,
#             ),
#             user_message=(
#                 f"Review the investigation context for case {req.case_id} and execute the "
#                 "appropriate on-demand tool to generate the final investigation report."
#             ),
#         )

#         sections = _extract_tool_results(messages)
#         investigation_final_report = sections.get("final_report", {})
#         report_section = _enrich_final_report_from_context(
#             sections=sections,
#             case_data=case_data,
#             playbook_data=playbook_data,
#             analyst_decision=req.analyst_decision,
#         )

#         merged_provenance = _merge_provenance(
#             case_data.get("provenance_trail", []),
#             new_provenance,
#         )

#         # Update CS-4 but return only the route-specific section.
#         CASE_STORE[req.case_id].update(report_section)
#         CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance
#         final_report = report_section.get("final_report", {})

#         # ai_summary: final complete contract — investigation, playbook, and report all at top level.
#         # Pass to /copilot for grounded Q&A on the final report.
#         investigation_data = {**case_data}
#         investigation_data.pop("provenance_trail", None)
#         investigation_data.pop("final_report", None)
#         investigation_data.pop("investigation_playbook", None)
#         investigation_playbook = case_data.get("investigation_playbook", {})
#         ai_summary = {
#             "investigation":           investigation_data,
#             "investigation_playbook":  investigation_playbook,
#             "final_report":            final_report,
#             "provenance_trail":        merged_provenance,
#         }
#         return {
#             "case_id":    req.case_id,
#             "status":     "completed",
#             "ai_summary": ai_summary,          # ← pass to /copilot
#             "details": {
#                 "agent_summary": markdown2.markdown(_build_report_summary(
#                     req.case_id,
#                     final_report,
#                     case_data,
#                     merged_provenance,
#                 )),
#             },
#         }
#     except HTTPException:
#         raise
#     except Exception as exc:
#         logger.exception("Report route failed for case_id=%s", req.case_id)
#         raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc
#     finally:
#         logger.info("POST /report completed for case_id=%s", req.case_id)


# @app.post("/copilot")
# def copilot(req: CopilotRequest):
#     """
#     ON-DEMAND — Section 3.4.
#     Answers investigator questions grounded in case context (CS-5).
#     Answers from CS-4 context first; falls back to tools only if needed.
#     ai_summary is REQUIRED per v6 spec — server decides which source to use.
#     If provenance_trail is absent from ai_summary, source citations degrade
#     gracefully — no crash.
#     """
#     try:
#         from agent_service.agent_runner import build_copilot_prompt

#         _validate_ai_summary_contract(req.ai_summary)
#         # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary
#         cs4_warm = req.case_id in CASE_STORE
#         case_data = _resolve_case_store(req.case_id, req.ai_summary)

#         runner = _get_runner()

#         messages, new_provenance_trail, tool_call_log = runner.run_scoped(
#             system_prompt=build_copilot_prompt(req.case_id, case_data),
#             user_message=req.question,
#             conversation_history=req.conversation_history or [],
#         )

#         answer = _extract_agent_summary(messages)

#         # sources_cited: include the stored provenance trail from CS-4 (so context-
#         # grounded answers cite the original AppWorks sources) plus any new tool
#         # calls made during this copilot turn.
#         # This aligns with Section 3.4 where the response shows sources from the
#         # original investigation even when tool_calls_made = 0.
#         stored_provenance = case_data.get("provenance_trail", [])
#         combined_provenance = _merge_provenance(stored_provenance, new_provenance_trail)

#         sources_cited = [
#             # f"{p['tool']} — {p.get('computed_by', '')} — "
#             f"retrieved {p.get('retrieved_at', '')}"
#             for p in combined_provenance
#         ]
#         sources_cited_details = [
#             {
#                 # "tool": p.get("tool", ""),
#                 "computed_by": p.get("computed_by", ""),
#                 "retrieved_at": p.get("retrieved_at", ""),
#                 "sources": p.get("sources", []),
#             }
#             for p in combined_provenance
#         ]

#         # CS-4: Update store only if the case entry still exists (it may have
#         # been evicted if TTL expires between _resolve_case_store and here).
#         if new_provenance_trail and req.case_id in CASE_STORE:
#             new_sections = _extract_tool_results(messages)
#             CASE_STORE[req.case_id].update(new_sections)
#             CASE_STORE[req.case_id]["provenance_trail"] = combined_provenance

#         return {
#             "answer":               markdown2.markdown(answer),
#             "sources_cited":        sources_cited,
#             "sources_cited_details": sources_cited_details,
#             "provenance_trail":     combined_provenance,
#             # "tool_calls_made":      len(new_provenance_trail),
#             "cs4_source":           "warm" if cs4_warm else "rehydrated",
#         }
#     except HTTPException:
#         raise
#     except Exception as exc:
#         logger.exception("Copilot route failed for case_id=%s", req.case_id)
#         raise HTTPException(status_code=500, detail=f"Copilot failed: {exc}") from exc
#     finally:
#         logger.info("POST /copilot completed for case_id=%s", req.case_id)









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
import markdown2 
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

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


# -----------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------

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
    No fields are stripped — downstream endpoints (playbook, report,
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
                # subject_primary_id  — used by /playbook, /report, /copilot prompts.
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


def _render_markdown_html(markdown_text: str) -> str:
    """Render Markdown to safer, more complete HTML for API responses."""
    normalized = _escape_middle_word_underscores(markdown_text or "")
    html = markdown2.markdown(
        normalized,
        extras=[
            "break-on-newline",
            "cuddled-lists",
            "fenced-code-blocks",
            "tables",
            "task_list",
        ],
        safe_mode="escape",
    )
    html = re.sub(r">\s+<", "><", html)
    html = re.sub(r"\s*\n\s*", " ", html)
    return html.strip()


def _safe_join(items: List[str], sep: str = ", ") -> str:
    """Join non-empty strings safely; return 'Not available' when empty."""
    clean = [str(i).strip() for i in (items or []) if str(i).strip()]
    return sep.join(clean) if clean else "Not available"


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
    playbook = sections.get("investigation_playbook", {}) if isinstance(sections, dict) else {}
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
    co_subjects = [s.get("details", {}).get("identifier") for s in subjects if not s.get("is_primary_subject")]
    fraud_types = complaint.get("fraud_types", [])
    allegations = complaint.get("allegations", [])
    similar_matches = similar.get("matches", []) if isinstance(similar, dict) else []
    triggered = risk.get("risk_indicators", []) if isinstance(risk, dict) else []
    triggered_names = [
        (r.get("rule_name") or r.get("rule_id")) if isinstance(r, dict) else str(r)
        for r in triggered
    ]
    prior_count = enrichment.get("prior_case_count", 0)
    recommendation = risk.get("recommendation", "No recommendation recorded.")
    step_count = len(playbook.get("investigation_steps", [])) if isinstance(playbook, dict) else 0
    report_status = final_report.get("status", "Not generated")
    allegation_lines = []
    for idx, item in enumerate(allegations, start=1):
        allegation_lines.append(
            f"{idx}) {item.get('allegation_type', {}).get('description', 'Unknown')} "
            f"(status={item.get('status', 'Unknown')}, received={item.get('date_received', 'Unknown')}, "
            f"agency={item.get('source_agency', {}).get('name', 'Unknown')}, ref={item.get('agency_referral_no', 'Unknown')})"
        )

    return (
        f"### Investigation Summary for Case {case_id}\n\n"
        f"**Case Background:** Complaint #{summary.get('complaint_no', 'Not available')} "
        f"({summary.get('case_description', 'Not available')}) is currently in "
        f"'{summary.get('destination', 'Not available')}' under team "
        f"'{summary.get('team', 'Not available')}'. Intake source is "
        f"{details.get('source', 'Not available')} with referral "
        f"{details.get('intake_referral_no', 'Not available')}.\n\n"
        f"**Subject Profile:** Primary subject is {primary_name}. Co-subjects recorded: "
        f"{_safe_join(co_subjects)}. The primary subject currently has {prior_count} prior case(s) in history.\n\n"
        f"**Allegation Details:** Fraud types linked to this case are {_safe_join(fraud_types)}. "
        f"Recorded allegations: {_safe_join(allegation_lines, ' | ')}.\n\n"
        f"**Similar Case Analysis:** {_safe_join([similar.get('query_summary', '')])} "
        f"with {len(similar_matches)} match(es) returned from archive checks.\n\n"
        f"**Risk Assessment:** Risk score is {risk.get('risk_score', 'Not available')} "
        f"with tier {risk.get('risk_tier', 'Not available')}. Risk Indicators: "
        f"{_safe_join(triggered_names)}. Recommendation from rules evaluation: {recommendation}\n\n"
        f"**Downstream Readiness:** Playbook step count currently available: {step_count}. "
        f"Final report status: {report_status}.\n\n"
        f"**Data Provenance:**\n{_format_provenance_lines(provenance_trail)}"
    )


def _build_playbook_summary(case_id: str, playbook: dict, provenance_trail: List[dict]) -> str:
    """Detailed deterministic summary for /playbook response."""
    if not isinstance(playbook, dict):
        return f"Playbook generation for case {case_id} completed, but no playbook payload was returned."
    steps = playbook.get("investigation_steps", [])
    owners = sorted({s.get("owner") for s in steps if isinstance(s, dict) and s.get("owner")})
    mandatory = len([
        item for item in playbook.get("evidence_checklist", [])
        if isinstance(item, dict) and item.get("mandatory")
    ])
    step_lines = []
    for step in steps:
        step_lines.append(
            f"- **Step {step.get('step', '?')}:** {step.get('action', 'Not available')} "
            f"Owner: {step.get('owner', 'Not available')}. "
            f"Deadline: {step.get('deadline_days', 'Not available')} day(s)."
        )
    return (
        f"### Investigation Playbook for Case {case_id}\n\n"
        f"Playbook `{playbook.get('playbook_id', 'Not available')}` was generated for risk tier "
        f"{playbook.get('risk_tier', 'Not available')} and fraud types "
        f"{_safe_join(playbook.get('fraud_types', []))}. "
        f"The workflow includes {len(steps)} investigation step(s):\n\n"
        f"{chr(10).join(step_lines) if step_lines else '- No investigation steps returned.'}\n\n"
        f"Mandatory evidence item count is {mandatory}, escalation_required is "
        f"{playbook.get('escalation_required', False)}, and owner group(s) involved are "
        f"{_safe_join(owners)}.\n\n"
        f"**Data Provenance:**\n{_format_provenance_lines(provenance_trail)}"
    )


def _build_report_summary(case_id: str, final_report: dict, case_data: dict, provenance_trail: List[dict]) -> str:
    """Detailed deterministic summary for /report response."""
    sections = final_report.get("sections", {}) if isinstance(final_report, dict) else {}
    complaint = case_data.get("complaint_intelligence", {}) if isinstance(case_data, dict) else {}
    risk = case_data.get("risk_assessment", {}) if isinstance(case_data, dict) else {}
    summary = complaint.get("summary", {}) if isinstance(complaint, dict) else {}
    recommendation = risk.get("recommendation", "No recommendation recorded.")
    return (
        f"### Final Report Summary for Case {case_id}\n\n"
        f"Report `{final_report.get('report_id', 'Not available')}` generated with status "
        f"{final_report.get('status', 'Not available')}. "
        f"Complaint #{summary.get('complaint_no', 'Not available')} "
        f"({summary.get('case_description', 'Not available')}). "
        f"Risk score {risk.get('risk_score', 'Not available')} / {risk.get('risk_tier', 'Not available')}. "
        f"Recommended action: {recommendation}. "
        f"The final report includes {len(sections)} populated section(s), covering case summary, "
        f"subject history, allegation summary, financial summary, risk rationale, recommended actions, "
        f"playbook roll-up, and analyst decision status.\n\n"
        f"**Data Provenance:**\n{_format_provenance_lines(provenance_trail)}"
    )


def _risk_rule_lookup(case_data: dict) -> dict:
    rules_section = case_data.get("risk_rules", {})
    rules = rules_section.get("rules", []) if isinstance(rules_section, dict) else []
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


def _format_recommended_actions(case_data: dict, playbook_data: dict) -> str:
    risk = case_data.get("risk_assessment", {})
    recommendation = risk.get("recommendation") if isinstance(risk, dict) else None
    steps = playbook_data.get("investigation_steps", []) if isinstance(playbook_data, dict) else []
    escalation_required = playbook_data.get("escalation_required") if isinstance(playbook_data, dict) else False
    action_bits = []
    if recommendation:
        action_bits.append(recommendation)
    if escalation_required:
        action_bits.append("Escalation is required by the investigation playbook.")
    if steps:
        action_bits.append("Next playbook actions: " + "; ".join(step.get("action", "") for step in steps[:3] if step.get("action")))
    return " ".join(action_bits) or "No recommended actions recorded in the verified investigation context."


def _enrich_final_report_from_context(
    sections: dict,
    case_data: dict,
    playbook_data: dict,
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
    report_sections["investigation_playbook_summary"] = {
        "playbook_id": playbook_data.get("playbook_id"),
        "risk_tier": playbook_data.get("risk_tier"),
        "step_count": len(playbook_data.get("investigation_steps", [])),
        "escalation_required": playbook_data.get("escalation_required", False),
        "mandatory_evidence_count": len([
            item for item in playbook_data.get("evidence_checklist", [])
            if item.get("mandatory")
        ]),
    }
    report_sections["analyst_decision"] = analyst_decision or {}
    report_sections["recommended_actions"] = _format_recommended_actions(case_data, playbook_data)
    return {"final_report": final_report}


def _get_runner():
    from agent_service.agent_runner import BSIAgentRunner
    return BSIAgentRunner(MANIFEST_PATH)


# -----------------------------------------------------------------------
# CS-4 RE-HYDRATION CONTRACT (v6)
# ai_summary is REQUIRED on every /copilot, /playbook, /report request.
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
    CASE_STORE[case_id] = {
        **ai_summary.get("investigation", {}),           # all tool result sections
        "provenance_trail": ai_summary.get("provenance_trail", []),  # must be present
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


def _normalize_playbook_payload(raw_playbook: Optional[Dict[str, Any]], case_data: dict) -> Optional[Dict[str, Any]]:
    """Accept multiple playbook payload shapes and normalize to investigation_playbook dict."""
    playbook_data = raw_playbook or case_data.get("investigation_playbook")
    if not isinstance(playbook_data, dict):
        return None
    if "investigation_playbook" in playbook_data and isinstance(playbook_data["investigation_playbook"], dict):
        return playbook_data["investigation_playbook"]
    if "investigation" in playbook_data and isinstance(playbook_data["investigation"], dict):
        nested = playbook_data["investigation"].get("investigation_playbook")
        return nested if isinstance(nested, dict) else None
    # already a plain playbook shape
    if "playbook_id" in playbook_data or "investigation_steps" in playbook_data:
        return playbook_data
    return None


def _resolve_case_store(case_id: str, ai_summary: Optional[Dict[str, Any]]) -> dict:
    """
    CS-4 lookup pattern used by all ON-DEMAND handlers.
    Returns warm case_data from CS-4, or re-hydrates from ai_summary if cold.
    Raises HTTPException if neither source is available.
    """
    if case_id in CASE_STORE and CASE_STORE[case_id]:
        return CASE_STORE[case_id]

    # CS-4 cold — fall back to ai_summary sent in request body (v6 contract)
    if ai_summary:
        _validate_ai_summary_contract(ai_summary)
        _rehydrate_case_store(case_id, ai_summary)
        return CASE_STORE[case_id]

    raise HTTPException(
        status_code=400,
        detail=(
            "No investigation data available for this case. "
            "Run POST /investigate first, or provide ai_summary "
            "(with investigation sections and provenance_trail) in the request body."
        ),
    )


# -----------------------------------------------------------------------
# Request / response models
# -----------------------------------------------------------------------

class InvestigateRequest(BaseModel):
    case_id: str


class PlaybookRequest(BaseModel):
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
    ai_playbook: Optional[Dict[str, Any]] = None
    analyst_decision: Optional[Dict[str, Any]] = None


class CopilotRequest(BaseModel):
    case_id: str
    question: str
    # ai_summary is REQUIRED per v6 spec — frontend always sends it.
    # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
    ai_summary: Dict[str, Any]
    conversation_history: Optional[List[Dict[str, Any]]] = None


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
    Runs tools 1-5 in dependency order (LLM decides sequence).
    Populates CS-4 CASE_STORE for all subsequent on-demand calls.
    """
    start = time.time()
    try:
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")
        if not os.path.exists(MANIFEST_PATH):
            raise HTTPException(status_code=500, detail="manifest.yaml not found")

        runner = _get_runner()
        messages, provenance_trail, _ = runner.investigate(case_id=req.case_id)
        # print("-"*50)
        # print(messages)
        sections = _extract_tool_results(messages)

        # CS-4: populate store with all sections + provenance.
        CASE_STORE[req.case_id] = {**sections, "provenance_trail": provenance_trail}

        # ── Response split (v6 spec) ────────────────────────────────────────
        # ai_summary: the contract object passed as-is to /playbook, /report,
        #   /copilot. Contains only what downstream routes need.
        # details: human-readable narrative + meta — NOT required by downstream.
        # ──────────────────────────────────────────────────────────────────────
        ai_summary = {
            "investigation":    sections,
            "provenance_trail": provenance_trail,
        }
        # print("="*50)
        # print(messages)
        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to /playbook, /report, /copilot
            "details": {
                "agent_summary": _render_markdown_html(_extract_agent_summary(messages)),
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


@app.post("/playbook")
def playbook(req: PlaybookRequest):
    """
    ON-DEMAND — Section 3.2.
    Calls get_investigation_playbook only.
    Requires risk_tier from a prior /investigate run (via CS-4 or ai_summary body).
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    """
    from agent_service.agent_runner import build_playbook_prompt

    try:
        _validate_ai_summary_contract(req.ai_summary)
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
        runner = _get_runner()

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_playbook_prompt(case_data),
            user_message=(
                f"Review the investigation context for case {req.case_id} and execute the "
                "appropriate on-demand tool to retrieve the investigation playbook."
            ),
        )

        sections = _extract_tool_results(messages)
        investigation_playbook = sections.get("investigation_playbook", {})
        playbook_section = {
            "investigation_playbook": investigation_playbook
        }

        merged_provenance = _merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
        )

        # Update CS-4 but return only the route-specific section.
        CASE_STORE[req.case_id].update(playbook_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        # ai_summary: updated contract — investigation sections separate from playbook.
        # Pass this object to /report and /copilot.
        investigation_data = {**case_data}
        investigation_data.pop("provenance_trail", None)
        investigation_data.pop("investigation_playbook", None)
        ai_summary = {
            "investigation":           investigation_data,
            "investigation_playbook": investigation_playbook,
            "provenance_trail":        merged_provenance,
        }

        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to /report, /copilot
            "details": {
                "agent_summary": _render_markdown_html(_build_playbook_summary(
                    req.case_id,
                    playbook_section.get("investigation_playbook", {}),
                    merged_provenance,
                )),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Playbook route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Playbook generation failed: {exc}") from exc
    finally:
        logger.info("POST /playbook completed for case_id=%s", req.case_id)


@app.post("/report")
def report(req: ReportRequest):
    """
    ON-DEMAND — Section 3.3.
    Calls generate_final_report with all six v6 params:
      case_id, subject_id, fraud_types, risk_score, risk_tier, risk_indicators.
    Requires: risk_assessment in case data, playbook, and analyst approval.
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    """
    from agent_service.agent_runner import build_report_prompt

    try:
        _validate_ai_summary_contract(req.ai_summary)
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
        if "risk_assessment" not in case_data:
            raise HTTPException(
                status_code=400,
                detail="risk_assessment section missing — run /investigate first.",
            )

        playbook_data = _normalize_playbook_payload(req.ai_playbook, case_data)
        if not playbook_data:
            raise HTTPException(status_code=400, detail="Playbook required — run /playbook first.")
        if not req.analyst_decision or req.analyst_decision.get("decision") != "APPROVED":
            raise HTTPException(
                status_code=400,
                detail="Report requires analyst approval. analyst_decision.decision must be 'APPROVED'.",
            )

        runner = _get_runner()
        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_report_prompt(
                case_id=req.case_id,
                case_data=case_data,
                ai_case_summary=req.ai_case_summary or "",
                playbook_data=playbook_data,
                analyst_decision=req.analyst_decision,
            ),
            user_message=(
                f"Review the investigation context for case {req.case_id} and execute the "
                "appropriate on-demand tool to generate the final investigation report."
            ),
        )

        sections = _extract_tool_results(messages)
        investigation_final_report = sections.get("final_report", {})
        report_section = _enrich_final_report_from_context(
            sections=sections,
            case_data=case_data,
            playbook_data=playbook_data,
            analyst_decision=req.analyst_decision,
        )

        merged_provenance = _merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
        )

        # Update CS-4 but return only the route-specific section.
        CASE_STORE[req.case_id].update(report_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance
        final_report = report_section.get("final_report", {})

        # ai_summary: final complete contract — investigation, playbook, and report all at top level.
        # Pass to /copilot for grounded Q&A on the final report.
        investigation_data = {**case_data}
        investigation_data.pop("provenance_trail", None)
        investigation_data.pop("final_report", None)
        investigation_data.pop("investigation_playbook", None)
        investigation_playbook = case_data.get("investigation_playbook", {})
        ai_summary = {
            "investigation":           investigation_data,
            "investigation_playbook":  investigation_playbook,
            "final_report":            final_report,
            "provenance_trail":        merged_provenance,
        }
        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass to /copilot
            "details": {
                "agent_summary": _render_markdown_html(_build_report_summary(
                    req.case_id,
                    final_report,
                    case_data,
                    merged_provenance,
                )),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Report route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc
    finally:
        logger.info("POST /report completed for case_id=%s", req.case_id)


@app.post("/copilot")
def copilot(req: CopilotRequest):
    """
    ON-DEMAND — Section 3.4.
    Answers investigator questions grounded in case context (CS-5).
    Answers from CS-4 context first; falls back to tools only if needed.
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    If provenance_trail is absent from ai_summary, source citations degrade
    gracefully — no crash.
    """
    try:
        from agent_service.agent_runner import build_copilot_prompt

        _validate_ai_summary_contract(req.ai_summary)
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary
        cs4_warm = req.case_id in CASE_STORE
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
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
            "answer":               _render_markdown_html(answer),
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
