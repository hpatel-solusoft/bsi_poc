# demo.py
# ----------------------------------------------------------------
# BSI Fraud Investigation – POC Demo Entry Point
#
# Usage:
#   export OPENAI_API_KEY=sk-...
#   python demo.py
#
# What this proves:
#   - LLM receives ONLY the tool catalogue from manifest.yaml
#   - LLM autonomously decides which tools to call and in what order
#   - Every tool call is intercepted by SemanticDispatcher (the gate)
#   - Dispatcher validates params and routes to appworks_services.py
#   - appworks_services.py prints the mock HTTP calls to AppWorks
#   - LLM reasons over each result and decides the next step
#   - No direct DB access. No raw API exposure. LLM cannot hallucinate tools.
# ----------------------------------------------------------------

from dotenv import load_dotenv
load_dotenv()

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    print("\n[ERROR] OPENAI_API_KEY not found in .env file")
    sys.exit(1)

from agent_service.agent_runner import BSIAgentRunner

# ── Config ───────────────────────────────────────────────────────
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "manifest.yaml")
DEMO_CASE_ID  = "BSI-2024-00421"

# ── Run ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    runner = BSIAgentRunner(manifest_path=MANIFEST_PATH, api_key=api_key)
    runner.investigate(DEMO_CASE_ID)