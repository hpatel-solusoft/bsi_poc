# api/webhook.py
# ----------------------------------------------------------------
# BSI Fraud Investigation Platform — FastAPI Webhook
#
# This is the ONLY entry point for the AI agentic workflow.
# AppWorks (or Postman / browser for POC testing) submits a
# complaint case_id here. The AI Agent takes it from there.
#
# WHY ONE ENDPOINT?
# ─────────────────
# The LLM receives the full tool catalogue from manifest.yaml
# and autonomously decides which tools to call and in what order.
# There is NO per-agent, per-tool, or per-tab endpoint.
# The frontend reads named sections from the response to populate
# UI tabs — it never triggers a specific agent directly.
#
# Endpoints:
#   GET  /health      → Liveness check (testable in browser)
#   POST /investigate → Trigger full agentic investigation loop
#
# Test with Postman:
#   POST http://localhost:8000/investigate
#   Body (JSON): { "case_id": "BSI-2024-00421" }
#
# Run server:
#   uvicorn api.webhook:app --reload
#   OR: python run_server.py
# ----------------------------------------------------------------

import json
import os
import sys
import traceback
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Resolve project root so imports work regardless of working directory ──
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_service.agent_runner import BSIAgentRunner


# ── FastAPI app ───────────────────────────────────────────────────────────

app = FastAPI(
    title       = "BSI Fraud Investigation Webhook",
    description = (
        "Single-entry-point webhook that triggers the BSI AI Agentic "
        "Investigation workflow. The LLM autonomously calls all tools "
        "via the Semantic Dispatcher. No per-agent or per-tab endpoints."
    ),
    version     = "1.0-POC",
    docs_url    = "/docs",      # Swagger UI — open in browser to test
    redoc_url   = "/redoc"
)

# Allow browser-based frontends (AppWorks UI, local dev) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],  # Tighten in production to AppWorks domain
    allow_methods     = ["GET", "POST"],
    allow_headers     = ["*"],
)


# ── Request / Response models ─────────────────────────────────────────────

class InvestigateRequest(BaseModel):
    """
    Payload sent by AppWorks when a complaint form is submitted.
    In production: AppWorks triggers this webhook automatically.
    For POC testing: send from Postman or the /docs Swagger UI.
    """
    case_id: str

    model_config = {
        "json_schema_extra": {
            "examples": [{"case_id": "BSI-2024-00421"}]
        }
    }


# ── Tool → UI section mapping ─────────────────────────────────────────────
#
# This is the ONLY place where tool names are mapped to UI section names.
# Each section name corresponds to a tab in the BSI investigation screen.
# The frontend reads response.investigation.<section_name> to populate
# its tab — no tab ever calls a specific agent endpoint directly.
#
# Tab 1  → complaint_intelligence  (Complaint Details panel)
# Tab 2  → context_enrichment      (Linked Entities / Prior Cases panel)
# Tab 3  → similar_cases           (Similar Cases section)
# Tab 4  → risk_assessment         (Risk Panel)
# Tab 5  → investigation_playbook  (Suggested Strategy panel)
# Tab 6  → final_report            (Generated Investigation Report)

TOOL_TO_SECTION: dict[str, str] = {
    "verify_case_intake":       "complaint_intelligence",
    "fetch_subject_history":    "context_enrichment",
    "search_similar_cases":     "similar_cases",
    "calculate_risk_metrics":   "risk_assessment",
    "get_investigation_playbook": "investigation_playbook",
    "generate_final_report":    "final_report",
}


# ── Message parsing helpers ───────────────────────────────────────────────

def _extract_tool_results(messages: list) -> dict:
    """
    Parses the agent conversation history returned by BSIAgentRunner
    to extract each tool's result data into named sections.

    How it works:
      1. Scans assistant messages to build a map:
             tool_call_id → tool_name
      2. Scans role:"tool" messages to find each result by tool_call_id
      3. Maps each tool_name to its UI section name via TOOL_TO_SECTION
      4. Returns { section_name: result_data } for every tool called

    The LLM decided which tools to call and in what order.
    This function just harvests what was actually called.
    """
    # Step 1: Build tool_call_id → tool_name map from assistant messages
    tool_call_id_to_name: dict[str, str] = {}
    for msg in messages:
        # OpenAI SDK returns message objects (not dicts) for assistant turns
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                tool_call_id_to_name[tc.id] = tc.function.name

    # Step 2: Extract tool result messages (appended as dicts by agent_runner)
    sections: dict = {}
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            tool_name    = tool_call_id_to_name.get(tool_call_id)
            if not tool_name:
                continue
            section_name = TOOL_TO_SECTION.get(tool_name, tool_name)
            try:
                sections[section_name] = json.loads(msg["content"])
            except (json.JSONDecodeError, TypeError):
                sections[section_name] = msg.get("content")

    return sections


def _extract_agent_summary(messages: list) -> str:
    """
    Returns the final natural-language summary the LLM produced
    after completing all tool calls (the last assistant message
    with text content and no pending tool calls — finish_reason: stop).
    """
    for msg in reversed(messages):
        role    = getattr(msg, "role",    None) or (msg.get("role")    if isinstance(msg, dict) else None)
        content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
        # Skip tool result messages and empty assistant turns
        if role == "assistant" and content and content.strip():
            return content.strip()
    return ""


def _count_tool_calls(messages: list) -> int:
    """Counts how many tool calls the LLM made across all turns."""
    count = 0
    for msg in messages:
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            count += len(tool_calls)
    return count


def _resolve_manifest_path() -> str:
    """
    Finds manifest.yaml regardless of whether the project uses
    the flat layout (manifest.yaml at root) or the spec layout
    (config/manifest.yaml). Returns the first path that exists.
    """
    candidates = [
        os.path.join(ROOT, "manifest.yaml"),
        os.path.join(ROOT, "config", "manifest.yaml"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        f"manifest.yaml not found. Looked in: {candidates}"
    )


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get(
    "/health",
    summary     = "Liveness check",
    description = "Returns 200 OK if the service is running. Testable directly in a browser.",
    tags        = ["Monitoring"]
)
def health():
    return {
        "status":    "ok",
        "service":   "BSI Fraud Investigation Webhook",
        "version":   "1.0-POC",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post(
    "/investigate",
    summary     = "Trigger AI investigation for a complaint case",
    description = (
        "Receives a case_id (from AppWorks complaint form submission) and runs "
        "the full BSI AI Agentic Investigation workflow.\n\n"
        "The LLM autonomously decides which tools to call and in what order. "
        "No tool, agent, or tab is explicitly triggered from this endpoint.\n\n"
        "The response contains named sections that map directly to UI tabs:\n"
        "- `complaint_intelligence` → Complaint Details tab\n"
        "- `context_enrichment`     → Linked Entities / Prior Cases tab\n"
        "- `similar_cases`          → Similar Cases tab\n"
        "- `risk_assessment`        → Risk Panel tab\n"
        "- `investigation_playbook` → Suggested Strategy tab\n"
        "- `final_report`           → Investigation Report tab\n\n"
        "**Test with Postman:** `POST /investigate` body: `{ \"case_id\": \"BSI-2024-00421\" }`"
    ),
    tags        = ["Investigation"]
)
def investigate(request: InvestigateRequest):
    """
    ┌─────────────────────────────────────────────────────────────────┐
    │  AppWorks  ──POST {case_id}──►  webhook.py                      │
    │                                    │                            │
    │                               BSIAgentRunner                    │
    │                                    │                            │
    │                          LLM decides tool order                 │
    │                                    │                            │
    │                          SemanticDispatcher (gate)              │
    │                                    │                            │
    │                          appworks_services.py                   │
    │                                    │                            │
    │                          Structured JSON response               │
    │                          with named sections per UI tab         │
    └─────────────────────────────────────────────────────────────────┘
    """

    # ── Validate environment ─────────────────────────────────────────
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code = 500,
            detail      = "OPENAI_API_KEY is not set. Add it to your .env file."
        )

    try:
        manifest_path = _resolve_manifest_path()
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # ── Run the agentic investigation loop ───────────────────────────
    # BSIAgentRunner.investigate() returns the full message history.
    # The LLM decides tool order — we never specify it here.
    started_at = datetime.now(timezone.utc)

    try:
        runner   = BSIAgentRunner(manifest_path=manifest_path, api_key=api_key)
        messages = runner.investigate(request.case_id)
    except Exception as e:
        raise HTTPException(
            status_code = 500,
            detail      = {
                "error":   "Agent investigation failed",
                "message": str(e),
                "trace":   traceback.format_exc()
            }
        )

    completed_at = datetime.now(timezone.utc)

    # ── Parse message history into structured sections ───────────────
    # Each section maps to a UI tab. The LLM decided what to call —
    # we are only harvesting what was actually returned.
    sections      = _extract_tool_results(messages)
    agent_summary = _extract_agent_summary(messages)
    tool_call_count = _count_tool_calls(messages)

    # ── Return structured response ───────────────────────────────────
    return {
        "case_id": request.case_id,
        "status":  "completed",

        # LLM's final plain-English summary of all findings
        "agent_summary": agent_summary,

        # Named sections — one per UI tab.
        # The frontend reads the section it needs.
        # No tab calls a specific agent endpoint.
        "investigation": sections,

        # Metadata for observability and debugging
        "meta": {
            "manifest_path":       manifest_path,
            "total_messages":      len(messages),
            "tool_calls_made":     tool_call_count,
            "sections_populated":  list(sections.keys()),
            "started_at":          started_at.isoformat(),
            "completed_at":        completed_at.isoformat(),
            "duration_seconds":    round(
                (completed_at - started_at).total_seconds(), 2
            ),
        }
    }