"""
Converts the manifest tool catalogue into OpenAI function-calling schema.
Intentionally generic — has no knowledge of specific tool names.
"""
from typing import List


# Map manifest type strings to OpenAI JSON Schema types
_TYPE_MAP = {
    "string":  {"type": "string"},
    "integer": {"type": "integer"},
    "number":  {"type": "number"},
}


def _param_schema(param: dict) -> dict:
    """Builds an OpenAI-compatible JSON Schema entry for a single param."""
    param_type = param.get("type", "string")
    desc = param.get("description", "")

    # list[string] → array of strings
    if param_type == "list[string]":
        return {
            "type": "array",
            "items": {"type": "string"},
            "description": desc,
        }

    # list[dict] → array of objects (free-form)
    if param_type == "list[dict]":
        return {
            "type": "array",
            "items": {"type": "object"},
            "description": desc,
        }

    # Any other list[*] variant
    if "list" in param_type:
        return {
            "type": "array",
            "items": {"type": "string"},
            "description": desc,
        }

    # Scalar types: string, integer, number
    base = _TYPE_MAP.get(param_type, {"type": "string"})
    return {**base, "description": desc}


def build_openai_tools(dispatcher, allowed_tool_names: list[str] | None = None) -> List[dict]:
    tools = []
    allowed = set(allowed_tool_names or [])
    for tool in dispatcher.get_tool_catalogue():
        if allowed and tool["name"] not in allowed:
            continue

        properties = {}
        required_names = []

        # Required params — included in both properties and required list
        for param in tool.get("required_params", []):
            param_name = param["name"]
            properties[param_name] = _param_schema(param)
            required_names.append(param_name)

        # Optional params — included in properties only (not required)
        for param in tool.get("optional_params", []):
            param_name = param["name"]
            properties[param_name] = _param_schema(param)
            # Not appended to required_names

        tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"].strip(),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required_names,
                },
            },
        })

    return tools
