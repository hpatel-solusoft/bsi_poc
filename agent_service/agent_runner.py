"""
BSI Agent Runner — LLM agentic loop.

Responsibilities:
  - Message history management (CS-1)
  - Turn management and loop termination
  - Provenance trail accumulation (CS-7)
  - Structured tool_call_log (per-call trace with input/output/elapsed_ms)

Outside its scope:
  - HTTP concerns (status codes, request/response shaping)
  - Section names or UI structure
  - Tool selection logic (manifest owns that via dispatcher)
  - Writing to AppWorks (read-only principle)

Tool list contract:
  - runner.auto_tools     — trigger=AUTO tools only  (for /investigate)
  - runner.on_demand_tools — trigger=ON-DEMAND only   (fallback default for run_scoped)
  - runner.all_tools      — full catalogue, no trigger filter
                            USE THIS for section-based scoping in routes
                            (e.g. /similar_cases filters all_tools by name)

Empty-list vs None contract in _run_loop:
  - run_scoped uses `is not None` guard: an explicit tools=[] from the caller
    is NOT silently replaced with on_demand_tools. The caller owns scope.
  - _run_loop uses falsy guard for the OpenAI API call: tools=[] is converted
    to tools=None so the LLM receives no tools rather than an empty array,
    which the OpenAI SDK treats as ambiguous. An empty list reaching _run_loop
    is a server.py scoping error and should surface as a no-tool response,
    not as a silent full-catalogue fallback.
"""

import copy
import json
import logging
import os
import time
from typing import List, Dict, Tuple, Optional

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
# RESULT SUMMARISER — concise one-liner per tool for tool_call_log
# -----------------------------------------------------------------------

def _summarise_result(tool_name: str, result: dict) -> str:
    """
    Returns a short human-readable summary of a tool result for logging.
    Uses only generic field names from the semantic model contract —
    no hardcoded tool name checks. Renaming a tool in manifest.yaml
    requires no change here.
    """
    try:
        parts = []

        # Case / complaint identifier
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

        # Subject name (enrichment tools)
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

        # Investigation plan
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
    Render a prompt template from config/prompts.py with runtime values.
    config/prompts.py is the single prompt source. agent_runner supplies
    runtime case data and pre-extracted parameters only.
    """
    prompt = template
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", value)
    return prompt


# Module-level alias — keeps callers from importing CONFIG_ variant directly.
INVESTIGATE_SYSTEM_PROMPT = CONFIG_INVESTIGATE_SYSTEM_PROMPT


# -----------------------------------------------------------------------
# PROMPT BUILDERS — one per ON-DEMAND route
# Each builder reads its template from config/prompts.py and injects
# runtime values. No prompt text lives here.
# -----------------------------------------------------------------------

def build_similar_cases_prompt(case_data: dict) -> str:
    """
    ON-DEMAND /similar_cases prompt.
    Injects full case context so the agent can scope its archive search
    to the conduct described in the active investigation.
    """
    return _render_prompt(
        SIMILAR_CASES_PROMPT,
        {
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
        },
    )


def build_risk_assessment_prompt(case_data: dict) -> str:
    """
    ON-DEMAND /risk_assessment prompt.
    Injects full case context for get_risk_rules → calculate_risk_metrics
    two-step sequence.
    """
    return _render_prompt(
        RISK_ASSESSMENT_PROMPT,
        {
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
        },
    )


def build_plan_prompt(case_data: dict) -> str:
    """
    ON-DEMAND /plan prompt.
    Extracts fraud_types and risk_tier from case_data for template
    substitution; full context is also injected for strategy generation.
    """
    risk      = case_data.get("risk_assessment") or {}
    complaint = case_data.get("complaint_intelligence") or {}

    fraud_types = complaint.get("fraud_types") or []
    risk_tier   = risk.get("risk_tier") or ""

    return _render_prompt(
        PLAN_PROMPT,
        {
            "json.dumps(case_data, indent=2)":  json.dumps(case_data, indent=2),
            "json.dumps(fraud_types)":           json.dumps(fraud_types),
            "risk_tier":                         risk_tier,
            'case_data.get("case_id")':          str(case_data.get("case_id")),
        },
    )


def build_copilot_prompt(case_id: str, case_data: dict) -> str:
    """
    ON-DEMAND /copilot prompt.

    If a human-approved investigation plan is present in case_data, the
    AI-generated steps are replaced with the human-approved steps BEFORE
    the context is serialised into the prompt. The LLM sees exactly one
    set of steps — no ambiguity, no instruction-following required to
    choose between two competing lists.
    """
    context = copy.deepcopy(case_data)

    human_plan = context.get("modified_ai_investigation_plan")
    if (
        isinstance(human_plan, dict)
        and human_plan.get("source") == "human_approved"
        and isinstance(human_plan.get("steps"), list)
        and len(human_plan["steps"]) > 0
    ):
        if not isinstance(context.get("investigation_plan"), dict):
            context["investigation_plan"] = {}
        context["investigation_plan"]["investigation_steps"]  = human_plan["steps"]
        context["investigation_plan"]["_steps_source"]        = "human_approved"
        context["investigation_plan"]["_approved_by"]         = human_plan.get("modified_by", "")
        context["investigation_plan"]["_approved_on"]         = human_plan.get("modified_on", "")
        context["investigation_plan"]["_approval_comment"]    = human_plan.get("comment", "")

    return _render_prompt(
        COPILOT_TOOL_PROMPT,
        {
            "case_id":                           case_id,
            "json.dumps(case_data, indent=2)":   json.dumps(context, indent=2),
        },
    )


# -----------------------------------------------------------------------
# RUNNER
# -----------------------------------------------------------------------

class BSIAgentRunner:
    """
    Wraps the OpenAI client and the SemanticDispatcher into a single
    agentic loop that callers interact with via two public methods:

        investigate(case_id)         — AUTO flow (/investigate)
        run_scoped(prompt, message)  — ON-DEMAND flow (all other routes)

    Tool catalogue pools are pre-built at construction time:
        self.auto_tools       — trigger=AUTO only
        self.on_demand_tools  — trigger=ON-DEMAND only (run_scoped default)
        self.all_tools        — no trigger filter (use for section-based scoping)
    """

    def __init__(self, manifest_path: str, api_key: str = None):
        from semantic_layer.dispatcher import SemanticDispatcher
        from agent_service.tool_builder import build_openai_tools

        self.dispatcher      = SemanticDispatcher(manifest_path)
        self.auto_tools      = build_openai_tools(self.dispatcher, trigger="AUTO")
        self.on_demand_tools = build_openai_tools(self.dispatcher, trigger="ON-DEMAND")
        self.all_tools       = build_openai_tools(self.dispatcher)  # no filter — for section scoping
        self.client          = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    # ------------------------------------------------------------------
    # AUTO flow — /investigate
    # ------------------------------------------------------------------
    def investigate(
        self,
        case_id: str,
        tools: Optional[List[Dict]] = None,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        Run the AUTO investigation loop for a single case.

        `tools` is optional; when provided it is a pre-filtered subset of
        auto_tools (e.g. /investigate scopes to intake + enrichment only).
        When None, the full auto_tools catalogue is used.

        Returns (messages, provenance_trail, tool_call_log).
        """
        messages = [
            {"role": "system", "content": INVESTIGATE_SYSTEM_PROMPT},
            {"role": "user",   "content": f"Investigate case {case_id}."},
        ]
        return self._run_loop(
            messages,
            tools=tools if tools is not None else self.auto_tools,
            trigger="AUTO",
        )

    # ------------------------------------------------------------------
    # ON-DEMAND flow — /similar_cases, /risk_assessment, /plan, /copilot
    # ------------------------------------------------------------------
    def run_scoped(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: Optional[List[Dict]] = None,
        tools: Optional[List[Dict]] = None,
        trigger: str = "ON-DEMAND",
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        Run an ON-DEMAND scoped agent loop.

        `tools` contract:
          - None  → fall back to full on_demand_tools catalogue (copilot default)
          - []    → empty list passes through unchanged; _run_loop converts it to
                    None for the API call, so the LLM gets no tools. An empty list
                    here is a server.py scoping error — it surfaces honestly as a
                    no-tool response rather than silently using the full catalogue.
          - [...]  → the scoped subset the route computed (normal path)

        Returns (messages, provenance_trail, tool_call_log).
        """
        messages: List[Dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        return self._run_loop(
            messages,
            tools=tools if tools is not None else self.on_demand_tools,
            trigger=trigger,
        )

    # ------------------------------------------------------------------
    # Core agentic loop
    # ------------------------------------------------------------------
    def _run_loop(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]],
        trigger: str,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        The core turn-by-turn agentic loop.

        Returns (messages, provenance_trail, tool_call_log).

        tool_call_log entries contain:
            turn, tool, input, status, output_summary, output,
            elapsed_ms, retrieved_at, sources, computed_by

        Empty-list handling:
          tools=[] and tools=None both result in the OpenAI API call
          receiving tools=None / tool_choice=None, so the LLM produces
          a plain text response without tool calls.
          Use `is not None` semantics ABOVE this method (in run_scoped /
          investigate) to control fallback behaviour. Use falsy semantics
          HERE for the API boundary.
        """
        provenance_trail: List[Dict] = []
        tool_call_log:    List[Dict] = []

        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

        # Falsy check is intentional here: [] and None both mean "no tools for the API".
        tools_for_api  = tools if tools else None
        tool_choice    = "auto" if tools_for_api else None

        logger.info(
            f"[AGENT] Starting loop | model={model!r} "
            f"tools_scoped={len(tools) if tools else 0} "
            f"tools_to_api={len(tools_for_api) if tools_for_api else 0} "
            f"trigger={trigger!r}"
        )

        turn = 0
        while True:
            turn += 1

            response = self.client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_for_api,
                tool_choice=tool_choice,
            )

            choice = response.choices[0]
            msg    = choice.message

            # Append assistant turn to message history (CS-1 GROWS)
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

            # Loop exit: LLM finished or produced no tool calls
            if choice.finish_reason == "stop" or not msg.tool_calls:
                logger.info(
                    f"[AGENT] Turn {turn}: finish_reason={choice.finish_reason!r} — loop complete"
                )
                break

            # ── Process all tool calls in this turn ───────────────────
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    params = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    params = {}

                call_start = time.monotonic()
                logger.info(
                    f"[TOOL CALL] turn={turn} tool={tool_name!r} "
                    f"params={json.dumps(params, default=str)[:600]}"
                )
                if tool_name == "search_similar_cases":
                    logger.info(
                        f"[LLM INFERENCE] search_similar_cases called with "
                        f"fraud_types={params.get('fraud_types')}"
                    )

                # No pre-dispatch parameter injection — manifest is the single
                # source of truth (Principle 6). If the LLM omits a required
                # parameter, Gate 2 rejects the call and the LLM self-corrects
                # from the returned error message. Injecting params here would:
                #   a) violate manifest-as-single-source-of-truth;
                #   b) mask LLM errors silently;
                #   c) break on any tool rename in manifest.yaml.
                envelope = self.dispatcher.dispatch(
                    tool_name,
                    params,
                    expected_trigger=trigger,
                )
                elapsed_ms = round((time.monotonic() - call_start) * 1000)

                if envelope.get("status") == "ok":
                    result_data  = envelope.get("result", {})
                    # LLM receives only the result — never the provenance block (CS-2)
                    tool_content = json.dumps(result_data)

                    # Provenance trail (CS-7)
                    prov = envelope.get("provenance", {})
                    provenance_trail.append({
                        "sources":      prov.get("sources", []),
                        "retrieved_at": prov.get("retrieved_at", ""),
                        "computed_by":  prov.get("computed_by", ""),
                    })

                    summary   = _summarise_result(tool_name, result_data)
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
                    # Gate or execution error — LLM sees it and can self-correct
                    tool_content = json.dumps(envelope)
                    error_msg    = envelope.get("message", "unknown error")
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

            # Safety: prevent runaway loops
            if turn >= 10:
                logger.warning(f"[AGENT] Turn limit reached ({turn}) — forcing exit")
                break

        return messages, provenance_trail, tool_call_log