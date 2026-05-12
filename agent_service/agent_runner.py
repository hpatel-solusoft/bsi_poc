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

def _summarise_result(tool_name: str, result: dict) -> str:
    """
    Returns a short human-readable summary of a tool result for logging.
    Uses only generic field names known from the semantic model contract —
    no hardcoded tool name checks. Renaming a tool in manifest.yaml
    requires no change here.
    """
    try:
        parts = []

        # Case/complaint identifier
        summary_block = result.get("summary")
        for key in ("complaint_no", "case_id", "workfolder_id"):
            val = (
                summary_block.get(key)
                if isinstance(summary_block, dict)
                else result.get(key)
            )
            if val:
                parts.append(f"case={val}")
                break

        # Subject name (enrichment)
        fn = result.get("first_name", "")
        ln = result.get("last_name", "")
        if fn or ln:
            parts.append(f"subject={fn} {ln}".strip())

        # Prior case count
        prior = result.get("prior_case_count")
        if prior is not None:
            parts.append(f"prior_cases={prior}")

        # Similar cases
        top_n = result.get("top_n_returned")
        if top_n is not None:
            q = result.get("query_summary", "")
            parts.append(f"matches={top_n}")
            if q:
                parts.append(q)

        # Rules list
        rules = result.get("rules")
        if isinstance(rules, list):
            parts.append(f"{len(rules)} active rule(s) loaded")

        # Risk score / tier
        score = result.get("risk_score")
        tier  = result.get("risk_tier")
        if score is not None and tier is not None:
            pts = result.get("total_points", "?")
            mx  = result.get("max_points", "?")
            parts.append(f"score={score} tier={tier} pts={pts}/{mx}")

        # Playbook
        pid = result.get("playbook_id")
        if pid is not None:
            steps = result.get("investigation_steps", [])
            parts.append(f"playbook={pid} steps={len(steps) if isinstance(steps, list) else '?'}")

        # Final report
        secs = result.get("sections")
        if isinstance(secs, dict) and result.get("report_id"):
            parts.append(f"report_sections={len(secs)}")

        if parts:
            return " | ".join(parts)

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
- Do not fabricate data. Report risk_score and risk_tier exactly as returned by the tools.
- Stop calling tools once you have gathered all the data needed for the investigation summary.
- Complete your work within 10 tool calls. If a required parameter cannot be resolved after two self-correction attempts, stop and report the data gap in the investigation brief.

EXECUTION RULES:
- The tool catalogue you have received has already been scoped to this workflow phase. Use only the tools visible in your catalogue.
- Read each tool description carefully — it specifies which parameters the tool requires and which prior tool outputs supply those values. Use those data contracts to determine call order and parameter values.
- Once you have received the risk assessment result, write the investigation brief and stop.

PARAMETER PASSING:
- Each tool description declares which parameters it requires and where those values come from. Read the description before calling the tool.
- Pass every required parameter declared in the tool description using the values returned by prior tool calls.
- If a prior tool result does not contain an expected field, pass null or zero as appropriate and note the absence in the investigation brief — do not fabricate substitute values.
- If the dispatcher rejects a call due to a missing or unrecognised parameter, read the error message, correct the parameter set from the available tool results, and retry once.
																									 
																						
																					
																			   

SUMMARY FORMAT:
After completing all tool calls, write a comprehensive investigation brief for the BSI as continous plain english. Do not miss any tool response. Your final response must be flowing prose — paragraphs and sentences. Do NOT output JSON, Python dicts, raw field names, or any structured data format. The investigator reads this brief directly — it must be immediately readable without any technical interpretation.

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

EXECUTION RULES:
- The tool catalogue you have received has already been scoped to this workflow phase. Use only the tools visible in your catalogue.
- All case data has already been gathered and is provided in the context above. Do not call data-gathering tools.
- Read the tool descriptions to identify the appropriate tool for retrieving the investigation playbook. The tool description specifies exactly which parameter values to pass and where they come from in the case context.
- The values you need are already extracted for your reference:
    fraud_types : {json.dumps(fraud_types)}
    risk_tier   : "{risk_tier}"
- Make exactly one tool call. After the tool returns, write the playbook summary and stop.

After the tool returns, produce a concise plain-English summary of the playbook steps,
tailored to the established fraud pattern and prior case findings.

GUARDRAILS:
- Do not fabricate investigation steps.
- Ground every claim in verified tool output or the case context above.
- If you reference the risk score, state that it was produced by the BSI configured rules evaluation engine, not by AI inference.
- If a required parameter is missing from the case context, state that clearly in your summary rather than passing an empty value."""


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

EXECUTION RULES:
- The tool catalogue you have received has already been scoped to this workflow phase. Use only the tools visible in your catalogue.
- All case data has already been gathered and is provided in the context above. Do not call data-gathering tools.
- Read the tool descriptions to identify the appropriate tool for generating the final investigation report. The tool description specifies exactly which parameters are required and where they come from in the context above.
- The values you need are already extracted for your reference:
    case_id         : "{case_id}"
    subject_id      : "{subject_id}"
    fraud_types     : {json.dumps(fraud_types)}
    risk_score      : {risk_score}
    risk_tier       : "{risk_tier}"
    triggered_rules : {json.dumps(triggered_rules)}
- Make exactly one tool call. After the tool returns, write the investigation report and stop.

GUARDRAILS:
- Every factual claim must reference the AppWorks source entity it came from.
- The risk score is a deterministic output of the BSI configured rules evaluation engine — never modify or re-estimate it.
- Do not fabricate data. If a field is missing from AppWorks, state "Not recorded in AppWorks".
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
- Answer from the verified context above whenever possible. State which section the answer came from.
- Only call a tool if the question requires data genuinely not present in the context. Do not call tools to confirm or restate information already present.
- When citing a finding, reference the provenance_trail entry for that section — name the AppWorks source and when it was retrieved.
- Do not fabricate case data. If data is not in the context and no tool can retrieve it, say so explicitly.
- Answer the investigator's question, cite your source from the context, and stop. Do not chain additional tool calls unless the first call's result is insufficient to answer the question."""


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
            trigger="AUTO",
        )
        self.on_demand_tools = build_openai_tools(
            self.dispatcher,
            trigger="ON-DEMAND",
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
            trigger="AUTO",
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
            trigger="ON-DEMAND",
        )

    # ------------------------------------------------------------------
    # Core agentic loop
    # ------------------------------------------------------------------
    def _run_loop(
        self,
        messages: List[Dict],
        tools: List[Dict],
        trigger: str,
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

                # No pre-dispatch parameter injection — manifest is the single
                # source of truth (Principle 6). If the LLM omits a required
                # parameter, the dispatcher's Gate 2 rejects the call and the
                # LLM self-corrects from the error message. Hardcoding tool names
                # or silently injecting parameters here would:
                #   a) violate manifest-as-single-source-of-truth principle;
                #   b) mask LLM errors silently rather than surfacing them;
                #   c) break if any tool is renamed in the manifest.

                envelope = self.dispatcher.dispatch(
                    tool_name,
                    params,
                    expected_trigger=trigger,
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