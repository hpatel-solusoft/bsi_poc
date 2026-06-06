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
        
        self.all_tools        — no filter (use for section-based scoping)
    """

    def __init__(self,  api_key: str |None = None):
        

        self.dispatcher      = SemanticDispatcher()
        self.all_tools       = build_openai_tools(self.dispatcher)  # no filter — for section scoping
        self.client          = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.max_turns = int(os.environ.get("BSI_MAX_TURNS", "10"))

    def run_scoped(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: Optional[List[Dict]] = None,
        scope: str = "ALL",
        execution_context: Optional[Dict] = None,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        Run an ON-DEMAND scoped agent loop.
        Tools available to the LLM are determined by the 'scope' parameter, which is checked against each tool's declared scope in the manifest.
        Returns (messages, provenance_trail, tool_call_log).
        """
        messages: List[Dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        return self._run_loop(
            messages,
            tools=self.scoped_tools(scope),
            scope=scope,
            execution_context=execution_context,
        )

    def scoped_tools(self, scope: str) -> List[Dict]:
        """
        Return OpenAI tool dicts declared for this scope in manifest.yaml.
        Copilot passes scope='ALL' to receive the full all pool —
        an intentional design decision: copilot is the open conversational
        interface and needs access to the full on_demand catalogue.
        Manifest is the single source of truth for all other scopes.
        """
        logger.info(f"Scoping tools for scope={scope!r}")
        logger.debug(f"scope_index: {self.dispatcher.scope_index}")
        if scope == "ALL":
            return self.all_tools
        names = set(self.dispatcher.scope_index.get(scope, []))
        return [t for t in self.all_tools if t["function"]["name"] in names]
        
    # ------------------------------------------------------------------
    # Core agentic loop
    # ------------------------------------------------------------------
    def _run_loop(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]],
        scope: str,
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
                    requested_scope=scope,
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

                    log_entry = {
                        "turn":           turn,
                        "tool":           tool_name,
                        "input":          params,
                        "status":         "ok",
                        "output":         result_data.lstrip() if isinstance(result_data, str) else "non-str",
                        "elapsed_ms":     elapsed_ms,
                        "retrieved_at":   prov.get("retrieved_at", ""),
                        "sources":        prov.get("sources", []),
                        "computed_by":    prov.get("computed_by", ""),
                    }
                    tool_call_log.append(log_entry)
                    logger.info(
                        f"[TOOL OK]  tool={tool_name!r} elapsed={elapsed_ms}ms"
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
