import importlib
import os
import yaml
from typing import Any, Dict, List, Optional



MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "../config/manifest.yaml")

class SemanticDispatcher:

    def __init__(self):
        with open(MANIFEST_PATH, "r") as f:
            self.manifest = yaml.safe_load(f)

        self.tools = self.manifest.get("tools", [])
        # Build tool registry keyed by name
        self.tool_registry: Dict[str, dict] = {
            tool["name"]: tool
            for tool in self.tools
        }

        self.tool_to_section: Dict[str, str] = {
            tool["name"]: tool["section"]
            for tool in self.tools
            if "section" in tool
        }

        # Build scope index: scope → [tool_names]
        self.scope_index: Dict[str, List[str]] = {}
        for tool in self.tools:
            scopes = tool.get("scope", [])
            if isinstance(scopes, str):
                scopes = [scopes]   # defensive: handle legacy single string
            for s in scopes:
                self.scope_index.setdefault(s, []).append(tool["name"])

        # Build section index: section → [tool_names]
        # Enables endpoints to scope tool catalogues by section
        # without hardcoded tool names anywhere in application code.
        self.section_index: Dict[str, List[str]] = {}
        for tool in self.tools:
            section = tool.get("section", "")
            if section:
                self.section_index.setdefault(section, []).append(tool["name"])

    def get_tool_catalogue(self) -> list:
        return self.tools

    def dispatch(
        self,
        tool_name: str,
        params: Dict[str, Any],
        requested_scope: Optional[str] = None,
        execution_context: dict | None = None
    ) -> dict:
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
        tool_scope = tool_config.get("scope")

        # Defense-in-depth: enforce execution mode at dispatcher level too.
        if requested_scope == "ALL":
            # Allow all tools in the "ALL" scope
            pass
        elif tool_scope and requested_scope not in tool_scope:
            return {
                "status": "error",
                "message": (
                    f"Tool '{tool_name}' is not allowed in this flow. "
                    f"Expected scope '{requested_scope}', tool scope is '{tool_scope}'."
                ),
            }

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
            module = importlib.import_module(module_name)
            func = getattr(module, func_name)
        except (ImportError, AttributeError) as exc:
            return {
                "status": "error",
                "message": f"Cannot resolve function '{python_function}': {exc}",
            }

        # --- Execute and pass envelope through unchanged ---
        try:
            #envelope = func(**params)
            # 1. Safely handle cases where execution_context is None
            context_kwargs = execution_context or {}
            
            # 2. Unpack BOTH the LLM's params AND the backend context
            envelope = func(**params, **context_kwargs)
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Execution error in '{tool_name}': {exc}",
            }

        # IMPORTANT: pass the full {result, provenance} envelope back.
        # Do NOT strip or modify provenance here.
        return {"status": "ok", **envelope}