# bsi_app/server.py
# ─────────────────────────────────────────────────────────────────────────
# Flask server — serves the UI and streams agent events via SSE
# ─────────────────────────────────────────────────────────────────────────

import os
import sys
import json
import queue
import threading
from pathlib import Path
from flask import Flask, Response, request, stream_with_context, send_file

# Load env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

ROOT = str(Path(__file__).parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tab_agent import BSITabAgent

# ── Config ────────────────────────────────────────────────────────────────
MANIFEST_PATH = Path(__file__).parent / "manifest.yaml"
API_KEY       = os.environ.get("OPENAI_API_KEY")
DEFAULT_CASE  = "BSI-2024-00421"

if not API_KEY:
    print("\n[ERROR] OPENAI_API_KEY not found in .env\n")
    sys.exit(1)

agent = BSITabAgent(str(MANIFEST_PATH), API_KEY)

# ── Flask App ─────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)
app.config["ENV"] = "development"


@app.route("/")
def serve_ui():
    """Serve the single-page prototype UI."""
    html_path = Path(__file__).parent / "index.html"
    return send_file(str(html_path), mimetype="text/html")


@app.route("/api/tabs")
def get_tabs():
    """Return available tab definitions."""
    return {"tabs": agent.get_tab_list()}


@app.route("/api/investigate/<tab>", methods=["POST"])
def investigate_tab(tab: str):
    """
    SSE endpoint — streams agent events for a given tab.
    The agent decides which tools to call based on the scoped task + manifest.
    """
    data    = request.get_json() or {}
    case_id = data.get("case_id", DEFAULT_CASE)

    event_queue = queue.Queue()

    def run_agent_thread():
        """Run the blocking agent in a background thread, push events to queue."""
        try:
            for event in agent.investigate_tab(case_id, tab):
                event_queue.put(event)
        except Exception as e:
            event_queue.put({"type": "error", "message": str(e)})
        finally:
            event_queue.put(None)  # sentinel — stream ended

    thread = threading.Thread(target=run_agent_thread, daemon=True)
    thread.start()

    def generate():
        """Pull events from queue and format as SSE."""
        while True:
            event = event_queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":      "no-cache",
            "X-Accel-Buffering":  "no",
            "Connection":         "keep-alive"
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*60}")
    print(f"  BSI Fraud Investigation Platform — Prototype")
    print(f"  Open: http://localhost:{port}")
    print(f"  Case: {DEFAULT_CASE}")
    print(f"  Manifest: {MANIFEST_PATH.name}")
    print(f"{'='*60}\n")
    app.run(debug=False, port=port, threaded=True)