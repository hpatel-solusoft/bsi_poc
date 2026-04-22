# agent_service/tool_builder.py
# ----------------------------------------------------------------
# Converts the SemanticDispatcher tool catalogue into the exact
# format OpenAI's API expects for function/tool calling.
#
# This is the bridge between the semantic layer and the LLM.
# ----------------------------------------------------------------

import sys, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from semantic_layer.dispatcher import SemanticDispatcher


def build_openai_tools(dispatcher: SemanticDispatcher) -> list[dict]:
    """
    Takes the tool catalogue from the semantic layer and wraps
    each entry in OpenAI's tool schema format.

    OpenAI schema:
      { type: "function", function: { name, description, parameters } }

    The LLM only sees what the manifest allows — nothing more.
    """
    openai_tools = []
    for tool in dispatcher.get_tool_catalogue():
        openai_tools.append({
            "type": "function",
            "function": {
                "name":        tool["name"],
                "description": tool["description"],
                "parameters":  tool["parameters"]   # JSON Schema object, same structure
            }
        })
    return openai_tools