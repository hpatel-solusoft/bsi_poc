import importlib
import yaml
import os
from typing import Any, Dict


class SemanticDispatcher:

    def __init__(self, manifest_path: str):
        with open(manifest_path, "r") as f:
            self.manifest = yaml.safe_load(f)
        # Build tool registry keyed by name
        self.tool_registry: Dict[str, dict] = {
            tool["name"]: tool
            for tool in self.manifest.get("tools", [])
        }

    def get_tool_catalogue(self) -> list:
        return self.manifest.get("tools", [])

    def dispatch(self, tool_name: str, params: Dict[str, Any]) -> dict:
        # --- Gate 1: Registry check ---
        if tool_name not in self.tool_registry:
            available = list(self.tool_registry.keys())
            return {
                "status": "error",
                "message": (
                    f"Tool '{tool_name}' is not registered. "
                    f"Available tools: {', '.join(available)}"
                ),
            }

        tool_config = self.tool_registry[tool_name]

        # --- Gate 2: Parameter check & Security ---
        required  = [p["name"] for p in tool_config.get("required_params", [])]
        optional  = [p["name"] for p in tool_config.get("optional_params", [])]
        allowed_params = set(required) | set(optional)

        # Security: reject any extra parameters not declared in manifest
        extra = [p for p in params if p not in allowed_params]
        if extra:
            return {
                "status": "error",
                "message": (
                    f"Unrecognised parameters for '{tool_name}': "
                    f"{', '.join(extra)}. "
                    f"Declared params: {', '.join(sorted(allowed_params))}."
                ),
            }

        missing = [p for p in required if p not in params]
        if missing:
            return {
                "status": "error",
                "message": (
                    f"Missing required parameters for '{tool_name}': "
                    f"{', '.join(missing)}"
                ),
            }

        # --- Gate 3: Function resolution & Security ---
        python_function = tool_config["python_function"]
        
        # Security: prevent directory traversal or private module imports
        if ".." in python_function or python_function.startswith("_") or "._" in python_function:
            return {
                "status": "error",
                "message": f"Illegal function path '{python_function}'"
            }
            
        module_name, func_name = python_function.rsplit(".", 1)
        try:
            # Note: The spec says 'semantic_layer.{module_name}'
            module = importlib.import_module(f"semantic_layer.{module_name}")
            func = getattr(module, func_name)
        except (ImportError, AttributeError) as exc:
            return {
                "status": "error",
                "message": f"Cannot resolve function '{python_function}': {exc}",
            }

        # --- Execute and pass envelope through unchanged ---
        try:
            envelope = func(**params)
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Execution error in '{tool_name}': {exc}",
            }

        # IMPORTANT: pass the full {result, provenance} envelope back.
        # Do NOT strip or modify provenance here.
        return {"status": "ok", **envelope}