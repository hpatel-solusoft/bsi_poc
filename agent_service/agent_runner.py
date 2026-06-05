"""
BSI Agent Runner — LLM agentic loop.
This module defines the BSIAgentRunner class, which encapsulates the OpenAI client and the SemanticDispatcher to execute an agentic loop. 
The runner processes LLM messages, executes tool calls with strict gatekeeping, and maintains a provenance trail and tool call log for each interaction.
"""


import json
import logging
import os
import time
from typing import List, Dict, Tuple, Optional
from openai import OpenAI
from semantic_layer.dispatcher import SemanticDispatcher
from agent_service.tool_builder import build_openai_tools
from agent_service.prompt_builders import build_investigate_system_prompt

logger = logging.getLogger(__name__)



# -----------------------------------------------------------------------
# RUNNER
# -----------------------------------------------------------------------

class BSIAgentRunner:
    """
    Wraps the OpenAI client and the SemanticDispatcher into a single
    agentic loop that callers interact with via two public methods:

        run_scoped(prompt, message)  — all routes

    Tool catalogue pools are pre-built at construction time:
        self.auto_tools       — trigger=AUTO only
        self.on_demand_tools  — trigger=ON-DEMAND only (run_scoped default)
        self.all_tools        — no trigger filter (use for section-based scoping)
    """

    def __init__(self, manifest_path: str, api_key: str |None = None):
        

        self.dispatcher      = SemanticDispatcher(manifest_path)
        self.auto_tools      = build_openai_tools(self.dispatcher, trigger="AUTO")
        self.on_demand_tools = build_openai_tools(self.dispatcher, trigger="ON-DEMAND")
        self.all_tools       = build_openai_tools(self.dispatcher)  # no filter — for section scoping
        self.client          = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.max_turns = int(os.environ.get("BSI_MAX_TURNS", "10"))

    def run_scoped(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: Optional[List[Dict]] = None,
        tools: Optional[List[Dict]] = None,
        trigger: str = "ON-DEMAND",
        execution_context: Optional[Dict] = None,
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
            execution_context=execution_context,
        )

    # ------------------------------------------------------------------
    # Core agentic loop
    # ------------------------------------------------------------------
    def _run_loop(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]],
        trigger: str,
        execution_context: Optional[Dict] = None,
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
          Use `is not None` semantics ABOVE this method (in run_scoped ) to control fallback behaviour. Use falsy semantics
          HERE for the API boundary.
        """
        provenance_trail: List[Dict] = []
        tool_call_log:    List[Dict] = []

        

        # Falsy check is intentional here: [] and None both mean "no tools for the API".
        tools_for_api  = tools if tools else None
        tool_choice    = "auto" if tools_for_api else None

        logger.info(
            f"[AGENT] Starting loop | model={self.model!r} "
            f"tools_scoped={len(tools) if tools else 0} "
            f"tools_to_api={len(tools_for_api) if tools_for_api else 0} "
            f"trigger={trigger!r}"
        )

        turn = 0
        while True:
            turn += 1

            response = self.client.chat.completions.create(
                model=self.model,
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
                    execution_context=execution_context
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

                    summary   = _summarise_result(result_data)
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
            if turn >= self.max_turns:
                logger.warning(f"[AGENT] Turn limit reached ({turn}) — forcing exit")
                break

        return messages, provenance_trail, tool_call_log

# -----------------------------------------------------------------------
# RESULT SUMMARISER — concise one-liner per tool for tool_call_log
# -----------------------------------------------------------------------

def _summarise_result(result: dict) -> str:
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
       
        if parts:
            return " | ".join(parts)

    except Exception:
        pass

    return json.dumps(result, default=str)[:120]

