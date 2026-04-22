# semantic_layer/dispatcher.py
# ----------------------------------------------------------------
# The ONLY gateway between LLM agent tool calls and backend services.
# Loads manifest.yaml, validates every call, routes to Python functions.
# The LLM never touches appworks_services.py directly.
# ----------------------------------------------------------------

import yaml
import importlib
import sys
import os

# Resolve package root absolutely so imports work on Windows and Mac/Linux
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

        # Ensure semantic_layer package is importable
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

        1. Semantic gate  – unknown tools are blocked cold
        2. Param check    – missing required params are rejected
        3. Function resolve – manifest maps tool → python function
        4. Execute        – call the function, return result
        """

        # 1. Semantic gate
        if tool_name not in self.registry:
            return {
                "status":  "error",
                "message": (
                    f"Tool '{tool_name}' is not registered in the semantic manifest. "
                    f"You must only call tools from the approved catalogue: "
                    f"{list(self.registry.keys())}"
                )
            }

        tool_def = self.registry[tool_name]

        # 2. Param validation
        missing = [
            p["name"] for p in tool_def["required_params"]
            if p["name"] not in params
        ]
        if missing:
            return {
                "status":  "error",
                "message": f"Missing required params for '{tool_name}': {missing}"
            }

        # 3. Resolve module.function
        fn_path    = tool_def["python_function"]
        mod_name, fn_name = fn_path.rsplit(".", 1)

        try:
            module = importlib.import_module(mod_name)
            fn     = getattr(module, fn_name)
        except (ImportError, AttributeError) as e:
            return {"status": "error", "message": f"Cannot resolve '{fn_path}': {e}"}

        # 4. Execute
        try:
            result = fn(**params)
            return {"status": "ok", "tool": tool_name, "agent": tool_def["_agent"], "data": result}
        except Exception as e:
            return {"status": "error", "message": f"'{fn_path}' raised: {e}"}