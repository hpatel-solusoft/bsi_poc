# run_server.py
# ----------------------------------------------------------------
# BSI Fraud Investigation Platform — Webhook Server Entry Point
#
# This is the server equivalent of demo.py.
# demo.py       → runs the agent once from the command line
# run_server.py → starts the FastAPI webhook server so AppWorks
#                 (or Postman / browser) can trigger investigations
#
# Usage:
#   python run_server.py
#
# Then test:
#   Browser  → http://localhost:8000/health
#   Swagger  → http://localhost:8000/docs      (full interactive UI)
#   Postman  → POST http://localhost:8000/investigate
#              Body: { "case_id": "BSI-2024-00421" }
#
# Environment:
#   Requires OPENAI_API_KEY in .env file (same as demo.py)
# ----------------------------------------------------------------

from dotenv import load_dotenv
load_dotenv()

import uvicorn

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  BSI Fraud Investigation Webhook — Starting")
    print("=" * 60)
    print("  Health check : http://localhost:8000/health")
    print("  Swagger UI   : http://localhost:8000/docs")
    print("  Investigate  : POST http://localhost:8000/investigate")
    print('  Body         : { "case_id": "BSI-2024-00421" }')
    print("=" * 60 + "\n")

    uvicorn.run(
        "api.webhook:app",
        host        = "0.0.0.0",
        port        = 8000,
        reload      = True,     # Auto-reload on code changes during dev
        log_level   = "info"
    )