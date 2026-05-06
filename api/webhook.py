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
import time
import yaml
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="BSI Fraud Investigation Platform")

# -----------------------------------------------------------------------
# CS-4: Case session context — in-memory for POC with 15-minute TTL.
# Falls back to ai_summary sent in the request body when the entry has
# expired or the server has restarted (stateless-session pattern).
# ai_summary is a REQUIRED field on all ON-DEMAND requests (v6 spec).
# -----------------------------------------------------------------------

CASE_STORE_TTL_SECONDS = 15 * 60  # 15 minutes (CS-4 lifespan per spec)


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

    def __init__(self, ttl_seconds: int):
        self._data: Dict[str, Dict] = {}
        self._ts:   Dict[str, float] = {}
        self._ttl = ttl_seconds

    # -- TTL helpers --------------------------------------------------

    def _alive(self, key: str) -> bool:
        return (
            key in self._data
            and (time.monotonic() - self._ts.get(key, 0.0)) < self._ttl
        )

    def _evict(self, key: str) -> None:
        self._data.pop(key, None)
        self._ts.pop(key, None)

    def ttl_remaining(self, key: str) -> Optional[float]:
        """Seconds remaining before key expires, or None if not present."""
        if not self._alive(key):
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
                if section == "context_enrichment" and isinstance(data, dict):
                    if data.get("dob") == "":
                        data["dob"] = None

                # Inject convenience top-level fields for downstream prompts.
                # subject_primary_id  — used by /playbook, /report, /copilot prompts.
                # fraud_types         — flattened list for the same consumers.
                # Both are derived from data already present in the result;
                # nothing is fabricated.
                if section == "complaint_intelligence" and isinstance(data, dict):
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
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            return msg["content"]
    return ""


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
    triggered = risk.get("triggered_rules", []) if isinstance(risk, dict) else []
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
        f"Triggered Rules: {'; '.join(parts) if parts else 'None'} | "
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
    AUTO flow — runs tools 1-5 in dependency order.
    Populates CS-4 CASE_STORE for all subsequent on-demand calls.
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")
    if not os.path.exists(MANIFEST_PATH):
        raise HTTPException(status_code=500, detail="manifest.yaml not found")

    start  = time.time()
    runner = _get_runner()
    messages, provenance_trail = runner.investigate(req.case_id)
    duration = round(time.time() - start, 1)

    sections      = _extract_tool_results(messages)
    agent_summary = _extract_agent_summary(messages)

    # CS-4: Populate store with all sections + provenance (TTL = 15 min)
    CASE_STORE[req.case_id] = {
        **sections,
        "provenance_trail": provenance_trail,
    }

    return {
        "case_id":          req.case_id,
        "status":           "completed",
        # agent_summary is the LLM-generated narrative covering all 4 agents.
        # This is the ONLY content shown to the investigator on screen.
        "agent_summary":    agent_summary,
        # investigation contains structured JSON sections — one per tool.
        # NOT displayed directly; the frontend saves the full response as
        # ai_summary and sends it in request bodies to /playbook, /report,
        # /copilot (ai_summary = { investigation: sections, provenance_trail }).
        "investigation":    sections,
        "provenance_trail": provenance_trail,
        "meta": {
            "tool_calls_made":  len(provenance_trail),
            "duration_seconds": duration,
            "cs4_ttl_seconds":  CASE_STORE_TTL_SECONDS,
        },
    }


@app.post("/playbook")
def playbook(req: PlaybookRequest):
    """
    ON-DEMAND — calls get_investigation_playbook only.
    Requires risk_tier from a prior /investigate run (via CS-4 or ai_summary body).
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    """
    from agent_service.agent_runner import build_playbook_prompt

    # CS-4 pattern (v6): use store if warm, fall back to ai_summary on miss.
    if req.case_id not in CASE_STORE:
        _rehydrate_case_store(req.case_id, req.ai_summary)
    case_data = CASE_STORE[req.case_id]

    runner = _get_runner()

    messages, _ = runner.run_scoped(
        system_prompt=build_playbook_prompt(case_data),
        user_message=(
            f"Retrieve the investigation playbook for case {req.case_id}. "
            f"Extract fraud_types and risk_tier from the context provided in the system prompt "
            f"and call get_investigation_playbook."
        ),
        allowed_tool_names=["get_investigation_playbook"],
    )

    sections = _extract_tool_results(messages)
    CASE_STORE[req.case_id].update(sections)

    return {
        "case_id":       req.case_id,
        "status":        "completed",
        "investigation": sections,
    }


@app.post("/report")
def report(req: ReportRequest):
    """
    ON-DEMAND — calls generate_final_report only.
    Requires: risk_assessment in case data, playbook, and analyst approval.
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    """
    from agent_service.agent_runner import build_report_prompt

    # CS-4 pattern (v6): use store if warm, fall back to ai_summary on miss.
    if req.case_id not in CASE_STORE:
        _rehydrate_case_store(req.case_id, req.ai_summary)
    case_data = CASE_STORE[req.case_id]

    if "risk_assessment" not in case_data:
        raise HTTPException(
            status_code=400,
            detail="risk_assessment section missing — run /investigate first.",
        )

    playbook_data = req.ai_playbook or case_data.get("investigation_playbook")
    # Normalize: if ai_playbook is the full /playbook response, extract the playbook
    if playbook_data and isinstance(playbook_data, dict) and "investigation" in playbook_data:
        playbook_data = playbook_data["investigation"].get("investigation_playbook")

    if not playbook_data:
        raise HTTPException(
            status_code=400,
            detail="Playbook required — run /playbook first.",
        )

    if not req.analyst_decision or req.analyst_decision.get("decision") != "APPROVED":
        raise HTTPException(
            status_code=400,
            detail=(
                "Report requires analyst approval. "
                "analyst_decision.decision must be 'APPROVED'."
            ),
        )

    runner = _get_runner()

    messages, _ = runner.run_scoped(
        system_prompt=build_report_prompt(
            case_data        = case_data,
            ai_case_summary  = req.ai_case_summary or "",
            playbook_data    = playbook_data,
            analyst_decision = req.analyst_decision,
        ),
        user_message=(
            f"Generate the investigation summary report for case {req.case_id}. "
            f"Call generate_final_report first, then synthesise all findings "
            f"into the director-ready narrative as instructed."
        ),
        allowed_tool_names=["generate_final_report"],
    )

    sections = _extract_tool_results(messages)
    sections = _enrich_final_report_from_context(
        sections         = sections,
        case_data        = case_data,
        playbook_data    = playbook_data,
        analyst_decision = req.analyst_decision,
    )

    CASE_STORE[req.case_id].update(sections)

    return {
        "case_id":       req.case_id,
        "status":        "completed",
        "investigation": sections,
    }


@app.post("/copilot")
def copilot(req: CopilotRequest):
    """
    ON-DEMAND — answers investigator questions grounded in case context.
    Answers from CS-4 context first; falls back to tools only if needed.
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    If provenance_trail is absent from ai_summary, source citations degrade
    gracefully — no crash.
    """
    from agent_service.agent_runner import build_copilot_prompt

    # CS-4 pattern (v6): use store if warm, fall back to ai_summary on miss.
    if req.case_id not in CASE_STORE:
        _rehydrate_case_store(req.case_id, req.ai_summary)
    case_data = CASE_STORE[req.case_id]

    runner = _get_runner()

    messages, provenance_trail = runner.run_scoped(
        system_prompt=build_copilot_prompt(req.case_id, case_data),
        user_message=req.question,
        conversation_history=req.conversation_history or [],
    )

    answer = _extract_agent_summary(messages)

    sources_cited = [
        f"{p['tool']} — {p.get('computed_by', '')} — "
        f"retrieved {p.get('retrieved_at', '')}"
        for p in provenance_trail
    ]

    # CS-4: Update store if a tool was called during this Copilot turn
    if provenance_trail:
        new_sections = _extract_tool_results(messages)
        CASE_STORE[req.case_id].update(new_sections)

    return {
        "answer":          answer,
        "sources_cited":   sources_cited,
        "tool_calls_made": len(provenance_trail),
    }
