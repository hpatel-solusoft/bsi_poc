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
# SYSTEM PROMPT — /investigate (AUTO flow)
# The LLM decides which available tools to call based on their input/output contracts.
# No ordering instructions — Principle 1: the LLM is the router.
# -----------------------------------------------------------------------
INVESTIGATE_SYSTEM_PROMPT = """You are the BSI Fraud Investigation AI Agent for the Bureau of Special Investigations, Massachusetts.

You have access to a set of approved tools connected to the AppWorks case management system. Read each tool description carefully — it defines what data it needs as input and what it produces as output. Use those data dependencies to determine which tools to call and in what order.

GUARDRAILS:
- Do not fabricate any data. If a tool returns an error, report it honestly.
- Do not call tools not listed in your tool catalogue.
- Treat risk_score, risk_tier, and triggered_rules as deterministic outputs produced by the BSI configured rules evaluation engine — report them exactly as returned, never modify or re-interpret them.
- Stop calling tools once you have gathered all the data needed for the investigation summary.

SUMMARY FORMAT:
After completing all necessary tool calls, write a comprehensive investigation brief suitable for the Bureau of Special Investigations. This is a formal case document — not a short summary.

Structure:
- Use bold section headers to organise the brief.
- Present all data from tool outputs as readable prose and key-value pairs — never output raw JSON.
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
# SCOPED SYSTEM PROMPTS — ON-DEMAND flows
# -----------------------------------------------------------------------

def build_playbook_prompt(case_data: dict) -> str:
    return f"""You are the BSI Investigation Agent. You have access to the AppWorks tool catalogue.

Here is the verified investigation context for this case:

{json.dumps(case_data, indent=2)}

Use the tool descriptions to determine which tool to call and with what parameters, based on the data available in the context above. After the tool returns, produce a concise plain-English summary of the playbook for the investigator.

GUARDRAILS:
- Do not call tools not in your catalogue.
- Do not fabricate investigation steps.
- Ground every claim in verified tool output or the case context above."""


def build_report_prompt(
    case_data: dict,
    ai_case_summary: str,
    playbook_data: dict,
    analyst_decision: dict,
) -> str:
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

Use the tool descriptions to determine which tool to call and with what parameters, using the verified values from the context above. After the tool returns, synthesise all findings into a formal, director-ready investigation report.

GUARDRAILS:
- Every factual claim must reference the AppWorks source entity it came from.
- The risk score is a deterministic output of the BSI configured rules evaluation engine — never modify or re-estimate it.
- Do not fabricate data. If a field is missing from AppWorks, state "Not recorded in AppWorks".
- Write in formal plain English suitable for a Director of Special Investigations."""


def build_copilot_prompt(case_id: str, case_data: dict) -> str:
    return f"""You are the BSI Investigation Copilot for Case {case_id}.

The following investigation data has already been retrieved and verified from AppWorks. Use it to answer investigator questions.

--- VERIFIED CASE CONTEXT ---
{json.dumps(case_data, indent=2)}
--- END CONTEXT ---

GUARDRAILS:
- Answer from the verified context above whenever possible. State which section the answer came from.
- Only call a tool if the question requires data genuinely not present in the context.
- When citing a finding, reference the AppWorks source and when it was retrieved.
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
        self.tools = build_openai_tools(self.dispatcher)
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    # ------------------------------------------------------------------
    # AUTO flow — /investigate
    # ------------------------------------------------------------------
    def investigate(
        self, 
        case_id: str, 
        allowed_tool_names: list[str] | None = None
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        messages = [
            {"role": "system", "content": INVESTIGATE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Investigate case {case_id}."},
        ]
        return self._run_loop(messages, allowed_tool_names=allowed_tool_names)

    # ------------------------------------------------------------------
    # ON-DEMAND flow — /playbook, /report, /copilot
    # ------------------------------------------------------------------
    def run_scoped(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list | None = None,
        allowed_tool_names: list[str] | None = None,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        messages: List[Dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})
        return self._run_loop(messages, allowed_tool_names=allowed_tool_names)

    # ------------------------------------------------------------------
    # Core agentic loop
    # ------------------------------------------------------------------
    def _run_loop(
        self,
        messages: List[Dict],
        allowed_tool_names: list[str] | None = None,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        Returns (messages, provenance_trail, tool_call_log).

        tool_call_log: list of per-call dicts containing:
            turn, tool, input, status, output_summary, output,
            elapsed_ms, retrieved_at, sources, computed_by
        """
        provenance_trail: List[Dict] = []
        tool_call_log:    List[Dict] = []   # CS-LOG: full per-call trace

        tools = self.tools
        if allowed_tool_names:
            allowed = set(allowed_tool_names)
            tools = [
                tool for tool in self.tools
                if tool.get("function", {}).get("name") in allowed
            ]

        turn = 0
        while True:
            turn += 1
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=tools,
                tool_choice="auto",
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
                    if "active_rules" not in params:
                        rules_from_history = _extract_rules_from_messages(messages)
                        if rules_from_history:
                            params = {**params, "active_rules": rules_from_history}
                            logger.info(
                                f"[PRE-INJECT] active_rules injected before dispatch "
                                f"({len(rules_from_history)} rules from message history)"
                            )
                    if "total_calculated" not in params:
                        params = {**params, "total_calculated": 0.0, "total_ordered": 0.0}
                        logger.info(
                            "[PRE-INJECT] total_calculated/total_ordered defaulted to 0.0 "
                            "(no financial figures forwarded by LLM)"
                        )
                # ── end pre-inject ───────────────────────────────────────────

                envelope = self.dispatcher.dispatch(tool_name, params)
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