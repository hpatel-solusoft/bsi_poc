"""
BSI Agent Runner â€” LLM agentic loop.
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
from config.prompts import (
    COPILOT_TOOL_PROMPT,
    INVESTIGATE_SYSTEM_PROMPT as CONFIG_INVESTIGATE_SYSTEM_PROMPT,
    PLAYBOOK_PROMPT,
    RISK_ASSESSMENT_PROMPT,
    REPORT_GENERATION_TOOL,
)
from openai import OpenAI

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# RESULT SUMMARISER â€” concise one-liner per tool for tool_call_log
# -----------------------------------------------------------------------

def _summarise_result(tool_name: str, result: dict) -> str:
    """
    Returns a short human-readable summary of a tool result for logging.
    Uses only generic field names known from the semantic model contract â€”
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
# PROMPT RENDERING
# -----------------------------------------------------------------------


def _render_prompt(template: str, values: dict) -> str:
    """
    Render a centralized prompt template from config/prompts.py.
    Keeps config as the single prompt source while agent_runner supplies
    runtime case data and pre-extracted tool parameters.
    """
    prompt = template
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", value)
    return prompt


INVESTIGATE_SYSTEM_PROMPT = CONFIG_INVESTIGATE_SYSTEM_PROMPT


def build_playbook_prompt(case_data: dict) -> str:
    """
    Section 3.2 - ON-DEMAND /playbook prompt.
    Prompt text lives in config/prompts.py; dynamic values are injected here.
    """
    risk = case_data.get("risk_assessment") or {}
    complaint = case_data.get("complaint_intelligence") or {}

    fraud_types = complaint.get("fraud_types") or []
    risk_tier = risk.get("risk_tier") or ""

    return _render_prompt(
        PLAYBOOK_PROMPT,
        {
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
            "json.dumps(fraud_types)": json.dumps(fraud_types),
            "risk_tier": risk_tier,
            "case_data.get(\"case_id\")": str(case_data.get("case_id")),
        },
    )


def build_risk_assessment_prompt(case_data: dict) -> str:
    """
    Section 3.x - ON-DEMAND /risk_assessment prompt.
    Prompt text lives in config/prompts.py; dynamic values are injected here.
    Calls get_risk_rules and calculate_risk_metrics to explain case seriousness.
    """
    return _render_prompt(
        RISK_ASSESSMENT_PROMPT,
        {
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
        },
    )


def build_report_prompt(
    case_id: str,
    case_data: dict,
    ai_case_summary: str,
    playbook_data: dict,
    analyst_decision: dict,
) -> str:
    """
    Section 3.3 - ON-DEMAND /report prompt.
    Prompt text lives in config/prompts.py; dynamic values are injected here.
    """
    risk = case_data.get("risk_assessment") or {}
    complaint = case_data.get("complaint_intelligence") or {}

    subject_id = complaint.get("subject_primary_id") or risk.get("subject_id") or ""
    fraud_types = complaint.get("fraud_types") or []
    risk_score = risk.get("risk_score", 0.0)
    risk_tier = risk.get("risk_tier") or ""
    risk_indicators = risk.get("risk_indicators") or []

    return _render_prompt(
        REPORT_GENERATION_TOOL,
        {
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
            "ai_case_summary or \"Not provided.\"": ai_case_summary or "Not provided.",
            "json.dumps(playbook_data, indent=2)": json.dumps(playbook_data, indent=2),
            "json.dumps(analyst_decision, indent=2)": json.dumps(analyst_decision, indent=2),
            "case_id": case_id,
            "subject_id": subject_id,
            "json.dumps(fraud_types)": json.dumps(fraud_types),
            "risk_score": str(risk_score),
            "risk_tier": risk_tier,
            "json.dumps(triggered_rules)": json.dumps(risk_indicators),
            "json.dumps(risk_indicators)": json.dumps(risk_indicators),
        },
    )


def build_copilot_prompt(case_id: str, case_data: dict) -> str:
    """
    Section 3.4 - ON-DEMAND /copilot prompt.
    Prompt text lives in config/prompts.py; dynamic values are injected here.
    """
    return _render_prompt(
        COPILOT_TOOL_PROMPT,
        {
            "case_id": case_id,
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
        },
    )
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
    # AUTO flow â€” /investigate  (Section 3.1)
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
    # ON-DEMAND flow â€” /playbook, /report, /copilot  (Sections 3.2â€“3.4)
    # ------------------------------------------------------------------
    def run_scoped(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list | None = None,
        tools: list | None = None,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        messages: List[Dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        return self._run_loop(
            messages,
            tools=tools or self.on_demand_tools,
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
                    f"[AGENT] Turn {turn}: finish_reason={choice.finish_reason!r} â€” loop complete"
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

                # No pre-dispatch parameter injection â€” manifest is the single
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
                    # LLM receives only the result â€” never the provenance block (CS-2)
                    tool_content = json.dumps(result_data)

                    # Provenance trail (CS-7)
                    prov = envelope.get("provenance", {})
                    provenance_trail.append({
                        # "tool":         tool_name,
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
                    # Gate/execution error â€” LLM sees it and can self-correct
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

