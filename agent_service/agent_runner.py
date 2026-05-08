"""
BSI Agent Runner — LLM agentic loop.
Responsibilities: message history, turn management, provenance_trail
accumulation, and structured tool_call_log (per-call trace with
input/output/elapsed_ms for every dispatcher call).
Outside its scope: HTTP concerns, section names, UI structure.
"""
import json
import logging
import os
import time
from typing import List, Dict, Tuple

from openai import OpenAI

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# RESULT SUMMARISER — concise one-liner per tool for tool_call_log
# -----------------------------------------------------------------------

def _extract_rules_from_messages(messages: list) -> list:
    """
    Scan message history for get_risk_rules tool result and return rules array.
    Used to auto-inject active_rules when LLM omits them from calculate_risk_metrics.
    """
    rules_call_id = None
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if tc.get("function", {}).get("name") == "get_risk_rules":
                    rules_call_id = tc.get("id")
    if not rules_call_id:
        return []
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == rules_call_id:
            try:
                data = json.loads(msg["content"])
                return data.get("rules", [])
            except (json.JSONDecodeError, TypeError):
                return []
    return []


def _extract_case_id_from_messages(messages: list):
    """
    Scan message history for verify_case_intake tool result and return its case_id.
    Used to auto-inject case_id into search_similar_cases when the LLM omits it.
    """
    intake_call_id = None
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if tc.get("function", {}).get("name") == "verify_case_intake":
                    intake_call_id = tc.get("id")
    if not intake_call_id:
        return None
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == intake_call_id:
            try:
                data = json.loads(msg["content"])
                return str(data.get("case_id", "")) or None
            except (json.JSONDecodeError, TypeError):
                return None
    return None


def _extract_tool_result_by_name(messages: list, tool_name: str) -> dict:
    """Return latest parsed tool result payload for a given tool name."""
    call_id = None
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if tc.get("function", {}).get("name") == tool_name:
                    call_id = tc.get("id")
    if not call_id:
        return {}
    for msg in reversed(messages):
        if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
            try:
                return json.loads(msg.get("content", "{}"))
            except (json.JSONDecodeError, TypeError):
                return {}
    return {}


def _extract_risk_context_from_messages(messages: list) -> dict:
    """
    Extract optional risk-scoring inputs from prior tool outputs.
    Ensures calculate_risk_metrics receives rich context even if omitted by LLM.
    """
    intake = _extract_tool_result_by_name(messages, "verify_case_intake")
    history = _extract_tool_result_by_name(messages, "fetch_subject_history")
    similar = _extract_tool_result_by_name(messages, "search_similar_cases")

    allegations = intake.get("allegations", []) if isinstance(intake, dict) else []
    subjects = intake.get("subjects", []) if isinstance(intake, dict) else []
    prior_cases = history.get("prior_cases", []) if isinstance(history, dict) else []

    primary_in_prior_cases = sum(
        1 for pc in prior_cases
        if pc.get("is_primary_subject") is True
    )
    distinct_types = len({
        (a.get("allegation_type", {}) or {}).get("id")
        for a in allegations
        if (a.get("allegation_type", {}) or {}).get("id")
    })
    has_open_allegation = any(
        (a.get("date_closed") in (None, "")) for a in allegations
    )

    # Prefer broad candidate count when available; fallback to filtered count.
    raw_matches = similar.get("raw_matches_found")
    top_n = similar.get("top_n_returned")
    similar_case_volume = raw_matches if isinstance(raw_matches, int) else top_n

    return {
        "prior_case_count": history.get("prior_case_count"),
        "primary_in_prior_cases": primary_in_prior_cases,
        "similar_case_volume": similar_case_volume,
        "distinct_types": distinct_types,
        "has_open_allegation": has_open_allegation,
        "subject_count": len(subjects),
        "received_age": (intake.get("details", {}) or {}).get("date_received_age"),
        "total_calculated": (intake.get("financials", {}) or {}).get("total_calculated", 0.0),
        "total_ordered": (intake.get("financials", {}) or {}).get("total_ordered", 0.0),
    }


def _summarise_result(tool_name: str, result: dict) -> str:
    """Returns a short human-readable summary of a tool result for logging."""
    try:
        if tool_name == "verify_case_intake":
            ci = result
            complaint_no = ci.get("summary", {}).get("complaint_no") or ci.get("case_id", "?")
            subjects = ci.get("subjects", [])
            fraud_types = ci.get("fraud_types", [])
            return (
                f"case={complaint_no} subjects={len(subjects)} "
                f"fraud_types={fraud_types}"
            )
        if tool_name == "fetch_subject_history":
            fn = result.get("first_name", "")
            ln = result.get("last_name", "")
            prior = result.get("prior_case_count", "?")
            return f"subject={fn} {ln} prior_cases={prior}"
        if tool_name == "search_similar_cases":
            n = result.get("top_n_returned", 0)
            q = result.get("query_summary", "")
            return f"matches={n} — {q}"
        if tool_name == "get_risk_rules":
            rules = result.get("rules", [])
            return f"{len(rules)} active rule(s) loaded"
        if tool_name == "calculate_risk_metrics":
            score = result.get("risk_score", "?")
            tier  = result.get("risk_tier", "?")
            pts   = result.get("total_points", "?")
            mx    = result.get("max_points", "?")
            return f"score={score} tier={tier} pts={pts}/{mx}"
        if tool_name == "get_investigation_playbook":
            pid = result.get("playbook_id", "?")
            steps = len(result.get("investigation_steps", []))
            return f"playbook={pid} steps={steps}"
        if tool_name == "generate_final_report":
            secs = len(result.get("sections", {}))
            return f"report_sections={secs}"
    except Exception:
        pass
    return json.dumps(result, default=str)[:120]


# -----------------------------------------------------------------------
# SYSTEM PROMPT — /investigate (AUTO flow — Section 3.1)
# The LLM decides which available tools to call based on their input/output contracts.
# No ordering instructions — Principle 1: the LLM is the router.
# -----------------------------------------------------------------------
INVESTIGATE_SYSTEM_PROMPT = """You are the BSI Fraud Investigation AI Agent for the Bureau of Special Investigations, Massachusetts.

You have access to a set of approved tools connected to the AppWorks case management system. Read each tool description carefully — it defines what data it needs as input and what it produces as output. Use those data dependencies to determine which tools to call and in what order.

GUARDRAILS:
- Do not fabricate data. report risk_score/tier exactly as returned.
- Stop calling tools once you have gathered all the data needed for the investigation summary.

EXECUTION RULES:
- You are in the AUTO flow. Use only tools marked as "[Execution Mode: trigger: AUTO]".
- Once you have called all AUTO tools and have the risk assessment, write the investigation brief and stop.

SUMMARY FORMAT:
After completing all trigger tool calls, write a comprehensive investigation brief for the BSI.

Structure:
- Use bold section headers to organise the brief.
- Include every relevant field returned by the tools. Do not omit data to be concise — investigators need the full picture.
- Write in clear, professional English. Use paragraphs for narrative sections (case background, subject history, risk assessment) and bullet points or key-value pairs for structured data (dates, identifiers, addresses, allegation details).

Risk Assessment:
- State the risk score, tier, and which BSI fraud detection rules were triggered.
- Explain on what BASIS each rule triggered (e.g. "the subject has 2 prior substantiated cases", "1 similar case was identified with matching billing patterns") — cite the underlying case data that caused the trigger.
- Do NOT explain HOW the scoring engine works or show point calculations. The investigator needs to know WHAT drove the risk, not the scoring mechanics.
- State that the risk score was computed by the BSI configured rules evaluation engine, not by AI inference.

Provenance:
- At the end of the brief, include a "Data Sources" section listing the AppWorks entities and records that were consulted, along with when they were retrieved. Present this in a readable format, not as raw JSON."""


# -----------------------------------------------------------------------
# SCOPED SYSTEM PROMPTS — ON-DEMAND flows (Sections 3.2, 3.3, 3.4)
# -----------------------------------------------------------------------

def build_playbook_prompt(case_data: dict) -> str:
    """
    Section 3.2 — ON-DEMAND /playbook prompt.
    Pre-extracts fraud_types and risk_tier so the LLM receives them as
    explicit constants rather than having to traverse the nested JSON.
    """
    risk      = case_data.get("risk_assessment") or {}
    complaint = case_data.get("complaint_intelligence") or {}

    fraud_types = complaint.get("fraud_types") or []
    risk_tier   = risk.get("risk_tier") or ""

    return f"""You are the BSI Investigation Agent. You have access to the AppWorks tool catalogue.

Here is the verified investigation context for this case:

{json.dumps(case_data, indent=2)}

PARAMETER EXTRACTION — use these exact values when calling the tool:
  fraud_types : {json.dumps(fraud_types)}
  risk_tier   : "{risk_tier}"

EXECUTION RULES:
- You are in the ON-DEMAND flow. Use only tools marked as "[Execution Mode: trigger: ON-DEMAND]".
- Do not call AUTO tools (verify_case_intake, fetch_subject_history, search_similar_cases,
  get_risk_rules, calculate_risk_metrics).
- Call get_investigation_playbook with fraud_types and risk_tier exactly as listed above.

After the tool returns, produce a concise plain-English summary of the playbook steps,
tailored to the established fraud pattern and prior case findings.

GUARDRAILS:
- Do not fabricate investigation steps.
- Ground every claim in verified tool output or the case context above."""


def build_report_prompt(
    case_id: str,
    case_data: dict,
    ai_case_summary: str,
    playbook_data: dict,
    analyst_decision: dict,
) -> str:
    """
    Section 3.3 — ON-DEMAND /report prompt.
    Pre-extracts all six required params for generate_final_report (v6 spec:
    case_id, subject_id, fraud_types, risk_score, risk_tier, triggered_rules).
    Injecting them explicitly prevents the LLM from having to derive them from
    deeply nested JSON, which is the primary cause of missing-param errors.
    """
    risk      = case_data.get("risk_assessment") or {}
    complaint = case_data.get("complaint_intelligence") or {}

    subject_id      = complaint.get("subject_primary_id") or risk.get("subject_id") or ""
    fraud_types     = complaint.get("fraud_types") or []
    risk_score      = risk.get("risk_score", 0.0)
    risk_tier       = risk.get("risk_tier") or ""
    triggered_rules = risk.get("triggered_rules") or []

    return f"""You are the BSI Investigation Report Agent. You have access to the AppWorks tool catalogue.

Here is the full verified investigation context for this case:

=== INVESTIGATION DATA ===
{json.dumps(case_data, indent=2)}

=== AI CASE SUMMARY ===
{ai_case_summary or "Not provided."}

=== INVESTIGATION PLAYBOOK ===
{json.dumps(playbook_data, indent=2)}

=== ANALYST DECISION ===
{json.dumps(analyst_decision, indent=2)}

PARAMETER EXTRACTION — use these exact values when calling the tool.
All six parameters are REQUIRED by the v6 spec (generate_final_report was expanded
from case_id-only to the full set below):
  case_id         : "{case_id}"
  subject_id      : "{subject_id}"
  fraud_types     : {json.dumps(fraud_types)}
  risk_score      : {risk_score}
  risk_tier       : "{risk_tier}"
  triggered_rules : {json.dumps(triggered_rules)}

EXECUTION RULES:
- You are in the ON-DEMAND flow. Use only tools marked as "[Execution Mode: trigger: ON-DEMAND]".
- Do not call AUTO tools.
- Call generate_final_report with ALL SIX parameters listed above — omitting any one
  will cause a dispatcher gate error.

After the tool returns, synthesise all findings into a formal investigation report
weaving together intake summary, enrichment patterns, risk rationale, playbook steps
taken, and analyst decision.

GUARDRAILS:
- Every factual claim must reference the AppWorks source entity it came from.
- The risk score is a deterministic output of the BSI configured rules evaluation
  engine — never modify or re-estimate it.
- Do not fabricate data. If a field is missing from AppWorks, state
  "Not recorded in AppWorks".
- Write in formal plain English suitable for a Director of Special Investigations."""


def build_copilot_prompt(case_id: str, case_data: dict) -> str:
    """
    Section 3.4 — ON-DEMAND /copilot prompt (CS-5 Copilot Injected Context).
    Full CASE_STORE[case_id] is serialised into the system prompt so the LLM
    can answer from context. provenance_trail is included for source citations.
    A tool is called only when the question requires data not present in context.
    """
    return f"""You are the BSI Investigation Copilot for Case {case_id}.

The following investigation data has already been retrieved and verified from AppWorks.
Use it to answer investigator questions.

--- VERIFIED CASE CONTEXT ---
{json.dumps(case_data, indent=2)}
--- END CONTEXT ---

GUARDRAILS:
- Answer from the verified context above whenever possible. State which section the
  answer came from.
- Only call a tool if the question requires data genuinely not present in the context.
- When citing a finding, reference the provenance_trail entry for that section —
  name the AppWorks source and when it was retrieved.
- Do not fabricate case data.
- If data is not in the context and no tool can retrieve it, say so explicitly."""


# -----------------------------------------------------------------------
# RUNNER
# -----------------------------------------------------------------------

class BSIAgentRunner:

    def __init__(self, manifest_path: str, api_key: str = None):
        from semantic_layer.dispatcher import SemanticDispatcher
        from agent_service.tool_builder import build_openai_tools

        self.dispatcher = SemanticDispatcher(manifest_path)
        self.auto_tools = build_openai_tools(
            self.dispatcher,
            execution_mode="trigger: AUTO",
        )
        self.on_demand_tools = build_openai_tools(
            self.dispatcher,
            execution_mode="trigger: ON-DEMAND",
        )
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    # ------------------------------------------------------------------
    # AUTO flow — /investigate  (Section 3.1)
    # ------------------------------------------------------------------
    def investigate(
        self,
        case_id: str,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        messages = [
            {"role": "system", "content": INVESTIGATE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Investigate case {case_id}."},
        ]
        return self._run_loop(
            messages,
            tools=self.auto_tools,
            execution_mode="trigger: AUTO",
        )

    # ------------------------------------------------------------------
    # ON-DEMAND flow — /playbook, /report, /copilot  (Sections 3.2–3.4)
    # ------------------------------------------------------------------
    def run_scoped(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list | None = None,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        messages: List[Dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        return self._run_loop(
            messages,
            tools=self.on_demand_tools,
            execution_mode="trigger: ON-DEMAND",
        )

    # ------------------------------------------------------------------
    # Core agentic loop
    # ------------------------------------------------------------------
    def _run_loop(
        self,
        messages: List[Dict],
        tools: List[Dict],
        execution_mode: str,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        Returns (messages, provenance_trail, tool_call_log).

        tool_call_log: list of per-call dicts containing:
            turn, tool, input, status, output_summary, output,
            elapsed_ms, retrieved_at, sources, computed_by
        """
        provenance_trail: List[Dict] = []
        tool_call_log:    List[Dict] = []   # CS-LOG: full per-call trace

        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        logger.info(f"[AGENT] Starting loop with model={model!r}, tools={len(tools)}")

        turn = 0
        while True:
            turn += 1
            response = self.client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
            )

            choice = response.choices[0]
            msg    = choice.message

            # Append assistant turn (CS-1 GROWS)
            assistant_entry: Dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id":   tc.id,
                        "type": "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_entry)

            # Stop if LLM is done
            if choice.finish_reason == "stop" or not msg.tool_calls:
                logger.info(
                    f"[AGENT] Turn {turn}: finish_reason={choice.finish_reason!r} — loop complete"
                )
                break

            # Process every tool call in this turn
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    params = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    params = {}

                call_start = time.monotonic()
                logger.info(
                    f"[TOOL CALL] turn={turn} tool={tool_name!r} "
                    f"input={json.dumps(params, default=str)[:600]}"
                )

                # ── pre-dispatch enrichment ──────────────────────────────────
                # Inject case_id into search_similar_cases if the LLM omitted it.
                # The traversal strategy in f3 requires case_id to resolve subjects
                # and allegation type IDs — without it the call returns zero results.
                if tool_name == "search_similar_cases" and "case_id" not in params:
                    case_id_from_history = _extract_case_id_from_messages(messages)
                    if case_id_from_history:
                        params = {**params, "case_id": case_id_from_history}
                        logger.info(
                            f"[PRE-INJECT] case_id={case_id_from_history!r} injected "
                            f"into search_similar_cases from verify_case_intake history"
                        )

                # Inject missing context for calculate_risk_metrics:
                #   1. active_rules — prevents f4 from making a redundant
                #      AppWorks AgentRulesTable fetch on every risk call.
                #   2. total_calculated / total_ordered defaults — prevents a
                #      redundant Workfolder_FinancialRelationship fetch when
                #      the LLM did not forward financial figures from intake.
                if tool_name == "calculate_risk_metrics":
                    risk_ctx = _extract_risk_context_from_messages(messages)
                    if "active_rules" not in params:
                        rules_from_history = _extract_rules_from_messages(messages)
                        if rules_from_history:
                            params = {**params, "active_rules": rules_from_history}
                            logger.info(
                                f"[PRE-INJECT] active_rules injected before dispatch "
                                f"({len(rules_from_history)} rules from message history)"
                            )
                    for k in (
                        "prior_case_count",
                        "primary_in_prior_cases",
                        "similar_case_volume",
                        "distinct_types",
                        "has_open_allegation",
                        "subject_count",
                        "received_age",
                        "total_calculated",
                        "total_ordered",
                    ):
                        if k not in params and risk_ctx.get(k) is not None:
                            params[k] = risk_ctx[k]
                    # If LLM passed a sparse/filtered volume (often 0 after manifest
                    # filtering), promote it to broad candidate volume when available.
                    if (
                        "similar_case_volume" in params
                        and isinstance(params.get("similar_case_volume"), (int, float))
                        and params.get("similar_case_volume", 0) <= 0
                        and isinstance(risk_ctx.get("similar_case_volume"), int)
                        and risk_ctx.get("similar_case_volume", 0) > 0
                    ):
                        params["similar_case_volume"] = risk_ctx["similar_case_volume"]
                    logger.info(
                        "[PRE-INJECT] risk context enriched: "
                        f"similar_case_volume={params.get('similar_case_volume')} "
                        f"prior_case_count={params.get('prior_case_count')} "
                        f"distinct_types={params.get('distinct_types')}"
                    )
                # ── end pre-inject ───────────────────────────────────────────

                envelope = self.dispatcher.dispatch(
                    tool_name,
                    params,
                    expected_execution_mode=execution_mode,
                )
                elapsed_ms = round((time.monotonic() - call_start) * 1000)

                if envelope.get("status") == "ok":
                    result_data = envelope.get("result", {})
                    # LLM receives only the result — never the provenance block (CS-2)
                    tool_content = json.dumps(result_data)

                    # Provenance trail (CS-7)
                    prov = envelope.get("provenance", {})
                    provenance_trail.append({
                        "tool":         tool_name,
                        "sources":      prov.get("sources", []),
                        "retrieved_at": prov.get("retrieved_at", ""),
                        "computed_by":  prov.get("computed_by", ""),
                    })

                    # Structured tool call log entry
                    summary = _summarise_result(tool_name, result_data)
                    log_entry = {
                        "turn":           turn,
                        "tool":           tool_name,
                        "input":          params,
                        "status":         "ok",
                        "output_summary": summary,
                        "output":         result_data,
                        "elapsed_ms":     elapsed_ms,
                        "retrieved_at":   prov.get("retrieved_at", ""),
                        "sources":        prov.get("sources", []),
                        "computed_by":    prov.get("computed_by", ""),
                    }
                    tool_call_log.append(log_entry)
                    logger.info(
                        f"[TOOL OK]  tool={tool_name!r} elapsed={elapsed_ms}ms "
                        f"summary={summary!r}"
                    )

                else:
                    # Gate/execution error — LLM sees it and can self-correct
                    tool_content = json.dumps(envelope)
                    error_msg = envelope.get("message", "unknown error")
                    log_entry = {
                        "turn":           turn,
                        "tool":           tool_name,
                        "input":          params,
                        "status":         "error",
                        "output_summary": f"ERROR: {error_msg}",
                        "output":         envelope,
                        "elapsed_ms":     elapsed_ms,
                    }
                    tool_call_log.append(log_entry)
                    logger.warning(
                        f"[TOOL ERR] tool={tool_name!r} elapsed={elapsed_ms}ms "
                        f"error={error_msg!r}"
                    )

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      tool_content,
                })

        return messages, provenance_trail, tool_call_log