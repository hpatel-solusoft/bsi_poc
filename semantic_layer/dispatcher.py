# semantic_layer/dispatcher.py
# ----------------------------------------------------------------
# BSI Fraud Investigation Platform — Semantic Dispatcher
#
# The ONLY gateway between LLM agent tool calls and backend
# services. Loads manifest.yaml, validates every call, routes
# to Python functions in appworks_services.py.
#
# The LLM never touches appworks_services.py directly.
#
# THREE GATES on every call:
#   Gate 1 — Tool must be registered in manifest.yaml
#   Gate 2 — All required params must be present in the call
#   Gate 3 — Python function must be resolvable via importlib
#
# CANONICAL MODEL INTEGRATION:
#   appworks_services.py validates and returns clean canonical
#   dicts. The dispatcher receives already-validated data and
#   passes it directly to the agent runner.
#
#   If a service function raises ValidationError (AppWorks
#   returned unexpected data), the dispatcher catches it here
#   and returns a structured error dict to the agent runner.
#   The LLM receives the error and can reason over it — the
#   agent loop continues cleanly rather than crashing.
#
#   If a service function raises ValueError (business logic
#   error — e.g. case not found), that is also caught here
#   and returned as a distinct structured error.
# ----------------------------------------------------------------

import yaml
import importlib
import sys
import os
from pydantic import ValidationError

# ── Path resolution ───────────────────────────────────────────────
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)


class SemanticDispatcher:

    def __init__(self, manifest_path: str):
        with open(manifest_path, "r") as f:
            self.manifest = yaml.safe_load(f)

        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        if pkg_dir not in sys.path:
            sys.path.insert(0, pkg_dir)

        # Build flat registry: { tool_name → tool_definition }
        # Loaded once at startup from manifest.yaml.
        # Adding a new tool to the manifest means it is automatically
        # registered here — no code change to the dispatcher needed.
        self.registry = {}
        for agent in self.manifest["agents"]:
            for tool in agent["tools"]:
                tool["_agent"] = agent["name"]
                self.registry[tool["name"]] = tool

    # ----------------------------------------------------------
    def get_tool_catalogue(self) -> list[dict]:
        """
        Returns the tool list the LLM agent is allowed to see.
        Names, descriptions, and param schemas only.
        No function references. No implementation details.

        Called by tool_builder.py to build the OpenAI tool schema.
        The LLM sees exactly what is in manifest.yaml — nothing more.
        """
        catalogue = []
        for tool in self.registry.values():
            catalogue.append({
                "name":        tool["name"],
                "description": tool["description"].strip(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        p["name"]: {
                            "type":        p["type"],
                            "description": p["description"]
                        }
                        for p in tool["required_params"]
                    },
                    "required": [p["name"] for p in tool["required_params"]]
                }
            })
        return catalogue

    # ----------------------------------------------------------
    def dispatch(self, tool_name: str, params: dict) -> dict:
        """
        Called by the agent loop for every LLM tool_use request.

        EXECUTION:
          Gate 1 → Gate 2 → Gate 3 → Execute service function
          → Return validated canonical dict to agent runner

        SUCCESS RETURN:
          { status: "ok", tool: tool_name, agent: agent_name,
            data: <validated canonical dict from service fn> }

        ERROR RETURNS (all structured — agent loop never crashes):
          Gate 1 fail: unknown tool name
          Gate 2 fail: missing required param
          Gate 3 fail: function not resolvable
          ValidationError: AppWorks response failed schema validation
          ValueError: business logic error (e.g. case not found)
          Exception: unexpected runtime error
        """

        # ── Gate 1 — Semantic gate ────────────────────────────────
        # Tool must exist in manifest.yaml. Unknown tool names are
        # blocked completely — the LLM cannot call anything that
        # is not explicitly declared in the manifest.
        if tool_name not in self.registry:
            return {
                "status":  "error",
                "message": (
                    f"Tool '{tool_name}' is not registered in the semantic "
                    f"manifest. Only approved tools may be called: "
                    f"{list(self.registry.keys())}"
                )
            }

        tool_def = self.registry[tool_name]

        # ── Gate 2 — Parameter validation ────────────────────────
        # All required params declared in manifest must be present.
        # Returns a structured error listing missing param names.
        missing = [
            p["name"] for p in tool_def["required_params"]
            if p["name"] not in params
        ]
        if missing:
            return {
                "status":  "error",
                "message": (
                    f"Missing required params for '{tool_name}': {missing}"
                )
            }

        # ── Gate 3 — Function resolution ─────────────────────────
        # manifest.yaml declares python_function as "module.function".
        # importlib resolves it at runtime — no hardcoded imports.
        # This is what allows adding new tools via manifest only.
        fn_path          = tool_def["python_function"]
        mod_name, fn_name = fn_path.rsplit(".", 1)

        try:
            module = importlib.import_module(mod_name)
            fn     = getattr(module, fn_name)
        except (ImportError, AttributeError) as e:
            return {
                "status":  "error",
                "message": f"Cannot resolve '{fn_path}': {e}"
            }

        # ── Execute ───────────────────────────────────────────────
        # The service function returns a validated canonical dict.
        # Three specific exception types are caught and handled:
        #
        #   ValidationError — AppWorks returned data that failed
        #     the canonical Pydantic schema. Caught specifically
        #     so the LLM receives a precise, actionable error.
        #
        #   ValueError — business logic error from the service fn
        #     e.g. "Case BSI-2024-99999 not found in AppWorks"
        #     Handled distinctly from technical/schema failures.
        #
        #   Exception — any other unexpected runtime error.
        #     Caught as a safety net so the agent loop never crashes.

        try:
            result = fn(**params)
            return {
                "status": "ok",
                "tool":   tool_name,
                "agent":  tool_def["_agent"],
                "data":   result          # validated canonical dict
            }

        except ValidationError as e:
            # AppWorks response failed canonical schema validation.
            # Surface as structured error — LLM can reason over it.
            print(f"\n  [SemanticDispatcher] ✗ CANONICAL VALIDATION FAILED "
                  f"for '{tool_name}': {e}")
            return {
                "status":  "error",
                "tool":    tool_name,
                "message": (
                    f"Tool '{tool_name}' returned data that failed canonical "
                    f"schema validation. AppWorks returned an unexpected "
                    f"response structure — a field may have been renamed, "
                    f"removed, or changed type. "
                    f"Details: {str(e)}"
                )
            }

        except ValueError as e:
            # Business logic error — e.g. case or subject not found.
            # Distinct from schema validation failures.
            print(f"\n  [SemanticDispatcher] ✗ SERVICE ERROR for "
                  f"'{tool_name}': {e}")
            return {
                "status":  "error",
                "tool":    tool_name,
                "message": str(e)
            }

        except Exception as e:
            # Unexpected runtime error — safety net.
            print(f"\n  [SemanticDispatcher] ✗ UNEXPECTED ERROR for "
                  f"'{tool_name}': {e}")
            return {
                "status":  "error",
                "message": f"'{fn_path}' raised an unexpected error: {e}"
            }