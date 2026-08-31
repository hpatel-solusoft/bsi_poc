"""
Shared lazy singleton for the BSIAgentRunner — every route that calls
the LLM agent (/intake, /similar_cases, /risk_assessment, /plan,
/copilot) needs one instance of this, and they must all share the same
instance rather than each constructing its own (BSIAgentRunner owns the
OpenAI client and tool dispatcher, which are safe and intended to be
reused across requests).

Factored out of api/server.py during the service-layer refactor so
api/services/*.py modules can obtain a runner without importing
FastAPI or any route-handling machinery just to reach this one
function — this module has no FastAPI dependency at all.
"""

from typing import Optional

from agent_service.agent_runner import BSIAgentRunner

_runner: Optional[BSIAgentRunner] = None


def get_runner() -> BSIAgentRunner:
    """
    Returns the shared BSIAgentRunner instance.

    Initialized once on first call — deferred (rather than constructed
    at import time) to ensure environment variables are loaded before
    the OpenAI client is created.
    """
    global _runner
    if _runner is None:
        _runner = BSIAgentRunner()
    return _runner
