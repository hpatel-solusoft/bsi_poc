# semantic_layer/dispatcher.py
# ----------------------------------------------------------------
# The ONLY gateway between LLM agent tool calls and backend services.
# Loads manifest.yaml, validates every call, routes to Python functions.
# The LLM never touches appworks_services.py directly.
#
# CANONICAL MODEL INTEGRATION:
#   appworks_services.py now validates and returns clean dicts.
#   The dispatcher receives already-validated data and passes it
#   directly to the agent runner. If the service function raises
#   a ValidationError (AppWorks returned unexpected data), the
#   dispatcher catches it here and returns a structured error
#   to the agent — the LLM sees the error and can reason over it.
#
# THREE GATES (unchanged):
#   Gate 1 — Tool must be registered in manifest.yaml
#   Gate 2 — All required params must be present
#   Gate 3 — Python function must be resolvable
# ----------------------------------------------------------------

import yaml
import importlib
import sys
import os
from pydantic import ValidationError

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
        No function references. No internals.
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

        GATES:
        1. Semantic gate  — unknown tools blocked cold
        2. Param check    — missing required params rejected
        3. Function resolve — manifest maps tool → python function
        4. Execute        — call the function, return result

        CANONICAL MODEL:
        The service function (appworks_services.py) validates its
        return data against the Pydantic canonical entity before
        returning it here. If validation fails, a ValidationError
        is raised in the service function and caught here — returned
        as a structured error to the agent runner so the LLM can
        reason over it rather than crashing the loop.
        """

        # Gate 1 — Semantic gate
        if tool_name not in self.registry:
            return {
                "status":  "error",
                "message": (
                    f"Tool '{tool_name}' is not registered in the semantic "
                    f"manifest. Approved tools: {list(self.registry.keys())}"
                )
            }

        tool_def = self.registry[tool_name]

        # Gate 2 — Param validation
        missing = [
            p["name"] for p in tool_def["required_params"]
            if p["name"] not in params
        ]
        if missing:
            return {
                "status":  "error",
                "message": f"Missing required params for '{tool_name}': {missing}"
            }

        # Gate 3 — Resolve module.function
        fn_path   = tool_def["python_function"]
        mod_name, fn_name = fn_path.rsplit(".", 1)

        try:
            module = importlib.import_module(mod_name)
            fn     = getattr(module, fn_name)
        except (ImportError, AttributeError) as e:
            return {"status": "error", "message": f"Cannot resolve '{fn_path}': {e}"}

        # Gate 4 — Execute
        # The service function returns a validated canonical dict.
        # ValidationError is caught here if AppWorks returned bad data.
        try:
            result = fn(**params)
            return {
                "status": "ok",
                "tool":   tool_name,
                "agent":  tool_def["_agent"],
                "data":   result          # Already validated canonical dict
            }

        except ValidationError as e:
            # AppWorks returned data that failed canonical schema validation.
            # Surface this as a structured error — the LLM can reason over it.
            print(f"\n  [SemanticDispatcher] ✗ CANONICAL VALIDATION FAILED: {e}")
            return {
                "status":  "error",
                "tool":    tool_name,
                "message": (
                    f"Tool '{tool_name}' returned data that failed canonical "
                    f"schema validation. This indicates AppWorks returned an "
                    f"unexpected response structure. Details: {str(e)}"
                )
            }

        except ValueError as e:
            # Service function raised a business logic error (e.g. case not found)
            return {
                "status":  "error",
                "tool":    tool_name,
                "message": str(e)
            }

        except Exception as e:
            return {
                "status":  "error",
                "message": f"'{fn_path}' raised an unexpected error: {e}"
            }