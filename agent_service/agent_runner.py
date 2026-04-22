# agent_service/agent_runner.py
# ----------------------------------------------------------------
# The AI Agent loop — powered by OpenAI.
#
# 1. OpenAI receives the task + tool catalogue from manifest
# 2. OpenAI DECIDES which tool to call and with what params
# 3. We intercept the tool_call → send to SemanticDispatcher
# 4. Dispatcher validates + routes to appworks_services function
# 5. Result goes back as a role:"tool" message
# 6. OpenAI reasons over the result, decides next tool or finishes
#
# Loop continues until OpenAI finish_reason is "stop" (no more tools).
# ----------------------------------------------------------------

import json
import openai
import sys, os

# Resolve bsi-agents-poc root absolutely so it works on Windows and Mac/Linux
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from semantic_layer.dispatcher import SemanticDispatcher
from agent_service.tool_builder import build_openai_tools

# ── Formatting helpers ──────────────────────────────────────────

DIVIDER = "=" * 72
SEP     = "-" * 72

def banner(text: str):
    print(f"\n{DIVIDER}")
    print(f"  {text}")
    print(DIVIDER)

def sub(text: str):
    print(f"\n  {SEP}")
    print(f"  {text}")
    print(f"  {SEP}")


# ── Core Agent Loop ─────────────────────────────────────────────

class BSIAgentRunner:

    SYSTEM_PROMPT = """You are the BSI Fraud Investigation AI Agent for the
Bureau of Special Investigations, Massachusetts.

You have a set of approved tools available to you. Each tool description
tells you what it does and when it should be used. Read the tool
descriptions carefully and use them to decide which tool to call next
and in what order.

Rules:
- Only call tools from your approved catalogue. Never make up tool names.
- Never fabricate case data. Only use what tools return.
- After each tool result, briefly state what you found before deciding your next action.
- When all investigation steps are complete, provide a concise final summary for the analyst."""

    def __init__(self, manifest_path: str, api_key: str = None):
        self.dispatcher = SemanticDispatcher(manifest_path)
        self.tools      = build_openai_tools(self.dispatcher)
        self.client     = openai.OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY")
        )

        print(f"\n[BSIAgentRunner] Initialized.")
        print(f"[BSIAgentRunner] Tools available to LLM: "
              f"{[t['function']['name'] for t in self.tools]}\n")

    # ----------------------------------------------------------
    def investigate(self, case_id: str):
        """
        Entry point. Give the LLM the case ID and let it drive.
        Runs the full agentic loop until OpenAI returns stop.
        """
        banner(f"BSI AI AGENT – Investigating Case: {case_id}")

        # Conversation history — grows with each turn
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user",   "content": f"Please investigate fraud complaint case {case_id} "
                                          f"and provide a full investigation summary."}
        ]

        turn = 0

        while True:
            turn += 1
            sub(f"Agent Turn {turn} — Calling OpenAI API")
            print(f"  Messages in context: {len(messages)}")

            # ── Call OpenAI ─────────────────────────────────
            response = self.client.chat.completions.create(
                model       = "gpt-4o",
                tools       = self.tools,
                tool_choice = "auto",       # LLM decides when to stop calling tools
                messages    = messages
            )

            message     = response.choices[0].message
            stop_reason = response.choices[0].finish_reason

            print(f"  Stop reason: {stop_reason}")

            # Append assistant message to history
            messages.append(message)

            # ── LLM has text to share (reasoning) ───────────
            if message.content:
                print(f"\n  ┌─ LLM says ────────────────────────────────────")
                for line in message.content.strip().split("\n"):
                    print(f"  │ {line}")
                print(f"  └───────────────────────────────────────────────")

            # ── LLM wants to call one or more tools ─────────
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    fn_name = tool_call.function.name
                    fn_args = json.loads(tool_call.function.arguments)

                    print(f"\n  ┌─ LLM calls tool ──────────────────────────────")
                    print(f"  │  Tool   : {fn_name}")
                    print(f"  │  Params : {json.dumps(fn_args, indent=2).replace(chr(10), chr(10) + '  │           ')}")
                    print(f"  └───────────────────────────────────────────────")

                    # ── Semantic Dispatcher intercepts here ──
                    print(f"\n  [SemanticDispatcher] → dispatching '{fn_name}'")
                    dispatch_result = self.dispatcher.dispatch(fn_name, fn_args)

                    if dispatch_result["status"] == "ok":
                        print(f"  [SemanticDispatcher] ✓ '{fn_name}' executed via "
                              f"[{dispatch_result['agent']}]")
                        tool_content = json.dumps(dispatch_result["data"])
                    else:
                        print(f"  [SemanticDispatcher] ✗ BLOCKED: {dispatch_result['message']}")
                        tool_content = json.dumps(dispatch_result)

                    # Send tool result back as role:"tool"
                    messages.append({
                        "role":         "tool",
                        "tool_call_id": tool_call.id,
                        "content":      tool_content
                    })

            # ── LLM is done — no more tool calls ────────────
            if stop_reason == "stop":
                banner("AGENT COMPLETED – Investigation Finished")
                print(f"  Total turns      : {turn}")
                print(f"  Total messages   : {len(messages)}")
                print(f"  Tools registered : {len(self.tools)}")
                break

        return messages