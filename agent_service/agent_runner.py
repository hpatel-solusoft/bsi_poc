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
    PLAN_PROMPT,
    RISK_ASSESSMENT_PROMPT,
    SIMILAR_CASES_PROMPT,
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
        rules = result.get("active_rules")
        if isinstance(rules, list):
            parts.append(f"{len(rules)} active rule(s) loaded")

        # Risk score / tier
        score = result.get("risk_score")
        tier  = result.get("risk_tier")
        if score is not None and tier is not None:
            pts = result.get("total_points", "?")
            mx  = result.get("max_points", "?")
            parts.append(f"score={score} tier={tier} pts={pts}/{mx}")

        # Plan
        pid = result.get("plan_id")
        if pid is not None:
            steps = result.get("investigation_steps", [])
            parts.append(f"plan={pid} steps={len(steps) if isinstance(steps, list) else '?'}")

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


def build_plan_prompt(case_data: dict) -> str:
    """
    Section 3.2 - ON-DEMAND /plan prompt.
    Prompt text lives in config/prompts.py; dynamic values are injected here.
    """
    risk = case_data.get("risk_assessment") or {}
    complaint = case_data.get("complaint_intelligence") or {}

    fraud_types = complaint.get("fraud_types") or []
    risk_tier = risk.get("risk_tier") or ""

    return _render_prompt(
        PLAN_PROMPT,
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


def build_similar_cases_prompt(case_data: dict) -> str:
    """
    ON-DEMAND /similar_cases prompt.
    Prompt text lives in config/prompts.py; dynamic values are injected here.
    Calls search_similar_cases to find historical cases with matching patterns.
    """
    return _render_prompt(
        SIMILAR_CASES_PROMPT,
        {
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
        },
    )

def build_copilot_prompt(case_id: str, case_data: dict) -> str:
    """
    Section 3.4 - ON-DEMAND /copilot prompt.
    Prompt text lives in config/prompts.py; dynamic values are injected here.

    If a human-approved plan is present, we overwrite investigation_plan.investigation_steps
    with the human steps BEFORE serialising into the prompt context. This means the LLM
    only ever sees one set of steps — no ambiguity, no reliance on instruction-following
    to choose between two competing step lists.
    """
    import copy
    context = copy.deepcopy(case_data)

    human_plan = context.get("modified_ai_investigation_plan")
    if (
        isinstance(human_plan, dict)
        and human_plan.get("source") == "human_approved"
        and isinstance(human_plan.get("steps"), list)
        and len(human_plan["steps"]) > 0
    ):
        # Replace AI steps with human-approved steps directly in the context.
        # Also annotate so the LLM can cite approver name and date correctly.
        if "investigation_plan" not in context or not isinstance(context["investigation_plan"], dict):
            context["investigation_plan"] = {}
        context["investigation_plan"]["investigation_steps"] = human_plan["steps"]
        context["investigation_plan"]["_steps_source"] = "human_approved"
        context["investigation_plan"]["_approved_by"] = human_plan.get("modified_by", "")
        context["investigation_plan"]["_approved_on"] = human_plan.get("modified_on", "")
        context["investigation_plan"]["_approval_comment"] = human_plan.get("comment", "")

    return _render_prompt(
        COPILOT_TOOL_PROMPT,
        {
            "case_id": case_id,
            "json.dumps(case_data, indent=2)": json.dumps(context, indent=2),
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
        self.all_tools = build_openai_tools(
            self.dispatcher,
            # No trigger filter — full catalogue for section-based scoping
        )
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    # ------------------------------------------------------------------
    # AUTO flow â€” /investigate  (Section 3.1)
    # ------------------------------------------------------------------
    def investigate(
        self,
        case_id: str,
        tools: list = None,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        messages = [
            {"role": "system", "content": INVESTIGATE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Investigate case {case_id}."},
        ]
        return self._run_loop(
            messages,
            tools=tools or self.auto_tools,
            trigger="AUTO",
        )

    # ------------------------------------------------------------------
    # ON-DEMAND flow — /similar_cases, /risk_assessment, /plan,
    #                   /report, /copilot
    # ------------------------------------------------------------------
    def run_scoped(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list | None = None,
        tools: list | None = None,
        trigger: str = "ON-DEMAND",
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        messages: List[Dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        return self._run_loop(
             messages,
            tools=tools if tools is not None else self.on_demand_tools,  # ← explicit None check
            trigger=trigger,
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
                if tool_name == "search_similar_cases":
                    logger.info(
                        f"[LLM INFERENCE] search_similar_cases called with "
                        f"fraud_types={params.get('fraud_types')}"
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

