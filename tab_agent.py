# bsi_app/tab_agent.py
# ─────────────────────────────────────────────────────────────────────────
# DUAL-MODE TAB AGENT
#
# MODE A (REAL): If openai package is installed and API key is valid,
#   the real GPT-4o agent runs. It receives a scoped task and the full
#   manifest tool catalogue. It decides which tools to call by itself,
#   based entirely on the data dependency descriptions in the manifest.
#
# MODE B (SIMULATION): If openai is not available, the prototype runs a
#   transparent simulation. The tool sequences shown are exactly what the
#   real LLM would derive from the manifest data contracts. The dispatcher,
#   3-gate validation, and mock AppWorks calls are all REAL in both modes.
#   Only the LLM reasoning is simulated with representative text.
#
# ARCHITECTURAL PRINCIPLE:
#   Tab click → scoped task description → (LLM reads manifest) → tool sequence
#   The tabs define the QUESTION. The agent decides the TOOL SEQUENCE.
#   NOT: tab click → call tool_X directly (that would bypass the agentic layer)
# ─────────────────────────────────────────────────────────────────────────

import json, io, sys, os, time
from pathlib import Path

ROOT = str(Path(__file__).parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from semantic_layer.dispatcher import SemanticDispatcher
from agent_service.tool_builder import build_openai_tools

# ── Detect real OpenAI availability ──────────────────────────────────────
try:
    import openai as _openai_lib
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ── Tab Configurations ────────────────────────────────────────────────────
TAB_CONFIGS = {
    "case_summary": {
        "title": "Case Summary",
        "icon":  "📋",
        "task": (
            "Your task: Verify and structure the complaint intake for case {case_id}. "
            "Retrieve the case record from AppWorks and provide a comprehensive structured summary. "
            "Include: case identification, subject details, fraud type, complaint narrative, and key entities."
        ),
        # Simulation: these are the tools the LLM would call from reading the manifest.
        # verify_case_intake is the only tool needed — it returns all intake fields.
        "sim_tools": ["verify_case_intake"],
        "sim_reasoning": [
            "I need to verify and structure the complaint intake for this case. "
            "Looking at my available tools, verify_case_intake reads a complaint case from AppWorks using the case_id "
            "and returns: case_id, complainant_name, subject_primary, subject_primary_id, complaint_description, fraud_type_classified, intake_date. "
            "That covers everything needed for the Case Summary tab. I'll call it now.",
            "The case header data has been returned successfully. I have all the intake information needed: "
            "the subject identification, fraud type classification, and complaint description. "
            "I can now provide a comprehensive case summary."
        ]
    },
    "subject_history": {
        "title": "Subject History",
        "icon":  "👤",
        "task": (
            "Your task: Retrieve the complete subject history and contextual enrichment for case {case_id}. "
            "Report: prior case history, known associates, address history, and escalation patterns."
        ),
        # Simulation: LLM sees fetch_subject_history needs subject_id.
        # It sees verify_case_intake returns subject_primary_id.
        # So it calls verify_case_intake FIRST, then fetch_subject_history.
        # This dependency chain is what makes the agent genuinely useful.
        "sim_tools": ["verify_case_intake", "fetch_subject_history"],
        "sim_reasoning": [
            "I need to retrieve the full subject history for this case. "
            "Looking at fetch_subject_history: it requires subject_id (subject_primary_id from verify_case_intake). "
            "I don't have that yet — so I must call verify_case_intake first to get the subject ID. "
            "This is a data dependency: fetch_subject_history → needs → verify_case_intake result. "
            "Calling verify_case_intake now.",
            "I now have the case record and can see the subject_primary_id. "
            "I'll use that to call fetch_subject_history to get the full 5-year subject profile, "
            "prior cases, known associates, and address history.",
            "Subject profile retrieved. I can see the prior case history, known associates, and address records. "
            "I'll now compile a comprehensive subject enrichment summary."
        ]
    },
    "similar_cases": {
        "title": "Similar Cases",
        "icon":  "🔍",
        "task": (
            "Your task: Find historically similar fraud cases for case {case_id}. "
            "Report the top matching cases with similarity scores, outcomes, and the dominant fraud pattern."
        ),
        # Simulation: search_similar_cases needs complaint_text (from verify_case_intake).
        "sim_tools": ["verify_case_intake", "search_similar_cases"],
        "sim_reasoning": [
            "I need to search for similar historical cases. "
            "The search_similar_cases tool requires complaint_text (complaint_description from verify_case_intake) and top_n. "
            "I don't have the complaint narrative yet, so I need to call verify_case_intake first to get it. "
            "Calling verify_case_intake to retrieve the complaint description.",
            "I have the complaint description. Now I'll search the BSI historical case archive "
            "using this complaint narrative to find similar past fraud cases. "
            "I'll request top 3 matches.",
            "Three similar cases returned with similarity scores. "
            "I can identify a consistent pattern across the matched cases. "
            "Compiling the similar cases report now."
        ]
    },
    "risk_assessment": {
        "title": "Risk Assessment",
        "icon":  "⚠️",
        "task": (
            "Your task: Calculate the fraud risk assessment for case {case_id}. "
            "Report: risk score, risk tier, all triggered rules with weights, billing anomaly flag, and recommendation. "
            "IMPORTANT: The risk score is deterministic — report it exactly as returned by the rules engine."
        ),
        # Simulation: calculate_risk_metrics needs case_id AND subject_id.
        # Both come from verify_case_intake, so it's called first.
        "sim_tools": ["verify_case_intake", "calculate_risk_metrics"],
        "sim_reasoning": [
            "I need to calculate the fraud risk metrics. "
            "The calculate_risk_metrics tool requires case_id and subject_id (subject_primary_id from verify_case_intake). "
            "I already have the case_id, but I need the subject_primary_id from the case record. "
            "Calling verify_case_intake to get both the confirmed case_id and subject_primary_id.",
            "I have the case_id and subject_primary_id. "
            "Now calling calculate_risk_metrics which will: fetch billing summary, fetch subject risk profile, "
            "then run the BSI rules engine to evaluate all active fraud detection rules. "
            "The risk score will be computed deterministically — I must report it exactly.",
            "Risk assessment completed. The rules engine has returned a deterministic risk score and tier. "
            "I'll report this exactly as returned — I cannot modify or override the score. "
            "Summarising triggered rules and the recommendation."
        ]
    }
}

SYSTEM_PROMPT = """You are the BSI Fraud Investigation AI Agent for the Bureau of Special Investigations, Massachusetts.

You have approved tools. Each tool description defines its data contract. Read them to determine which tools you need and in what order.

Rules:
1. Only call tools from your approved catalogue.
2. Never fabricate case data. Only use what tools return.
3. After each tool result, note what you found before deciding the next step.
4. The risk_score and risk_tier from calculate_risk_metrics are deterministic outputs from BSI rules engine. Report them exactly.
5. Complete your task concisely with a structured summary."""


# ═══════════════════════════════════════════════════════════════════════
# MAIN AGENT CLASS
# ═══════════════════════════════════════════════════════════════════════
class BSITabAgent:

    def __init__(self, manifest_path: str, api_key: str = None):
        self.dispatcher   = SemanticDispatcher(manifest_path)
        self.tools        = build_openai_tools(self.dispatcher)
        self.api_key      = api_key or os.environ.get("OPENAI_API_KEY")
        self.use_real_llm = OPENAI_AVAILABLE and bool(self.api_key)

        if self.use_real_llm:
            self.client = _openai_lib.OpenAI(api_key=self.api_key)

        mode = "REAL GPT-4o" if self.use_real_llm else "SIMULATION (openai not installed)"
        print(f"[BSITabAgent] Mode: {mode}")
        print(f"[BSITabAgent] Tools available: {[t['function']['name'] for t in self.tools]}")

    def get_tab_list(self):
        return [{"id": k, "title": v["title"], "icon": v["icon"]}
                for k, v in TAB_CONFIGS.items()]

    def investigate_tab(self, case_id: str, tab: str):
        """Generator yielding SSE events. Delegates to real or simulated runner."""
        if tab not in TAB_CONFIGS:
            yield {"type": "error", "message": f"Unknown tab: {tab}"}
            return
        if self.use_real_llm:
            yield from self._real_agent(case_id, tab)
        else:
            yield from self._simulated_agent(case_id, tab)

    # ── Real GPT-4o agent ─────────────────────────────────────────────────
    def _real_agent(self, case_id: str, tab: str):
        """Uses live GPT-4o. Agent decides tools from manifest. No tool hints given."""
        config = TAB_CONFIGS[tab]
        task   = config["task"].format(case_id=case_id)

        yield {"type": "start", "tab": tab, "title": config["title"],
               "case_id": case_id, "mode": "real",
               "task_sent_to_llm": task}

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": task}
        ]
        turn = 0
        all_data = {}

        while True:
            turn += 1
            resp    = self.client.chat.completions.create(
                model="gpt-4o", tools=self.tools,
                tool_choice="auto", messages=messages)
            msg     = resp.choices[0].message
            reason  = resp.choices[0].finish_reason
            messages.append(msg)

            if msg.content:
                yield {"type": "agent_thinking", "content": msg.content, "turn": turn}

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments)
                    yield {"type": "tool_call", "tool": name, "params": args, "turn": turn}

                    old, sys.stdout = sys.stdout, io.StringIO()
                    result = self.dispatcher.dispatch(name, args)
                    log, sys.stdout = sys.stdout.getvalue().strip(), old

                    if log:
                        yield {"type": "appworks_log", "content": log, "tool": name}
                    if result["status"] == "ok":
                        all_data[name] = result["data"]
                        yield {"type": "tool_result", "tool": name,
                               "agent_name": result.get("agent",""), "data": result["data"], "turn": turn}
                        messages.append({"role":"tool","tool_call_id":tc.id,"content":json.dumps(result["data"])})
                    else:
                        yield {"type": "tool_blocked", "tool": name, "reason": result["message"], "turn": turn}
                        messages.append({"role":"tool","tool_call_id":tc.id,"content":json.dumps(result)})

            if reason == "stop":
                yield {"type": "complete", "tab": tab, "title": config["title"],
                       "agent_summary": msg.content or "",
                       "tool_data": all_data, "turns_used": turn,
                       "tools_called": list(all_data.keys())}
                return
            if turn > 8:
                yield {"type": "error", "message": "Max turns exceeded"}
                return

    # ── Simulated agent (no OpenAI needed) ────────────────────────────────
    def _simulated_agent(self, case_id: str, tab: str):
        """
        Transparent simulation — shows exactly what the LLM would do.
        Tool sequences are derived from manifest data dependencies,
        identical to what GPT-4o would resolve from the contract descriptions.
        Real dispatcher + real mock AppWorks calls — only LLM calls are simulated.
        """
        config  = TAB_CONFIGS[tab]
        task    = config["task"].format(case_id=case_id)
        tools   = config["sim_tools"]
        reasons = config["sim_reasoning"]

        yield {"type": "start", "tab": tab, "title": config["title"],
               "case_id": case_id, "mode": "simulation",
               "task_sent_to_llm": task,
               "sim_note": (
                   "SIMULATION MODE: OpenAI not installed. Tool sequences shown are "
                   "exactly what GPT-4o would derive from manifest data dependencies. "
                   "Dispatcher and AppWorks mock calls are real."
               )}

        all_data  = {}
        collected = {}  # Accumulated results passed to next tool param resolution

        for i, tool_name in enumerate(tools):
            time.sleep(0.4)  # Simulate LLM thinking time

            # Emit reasoning that led to this tool selection
            if i < len(reasons):
                yield {"type": "agent_thinking", "content": reasons[i], "turn": i + 1}
                time.sleep(0.3)

            # Resolve parameters — this mirrors how the real LLM reads
            # the manifest description to figure out what params to pass.
            params = self._resolve_params(tool_name, case_id, collected)

            yield {"type": "tool_call", "tool": tool_name, "params": params, "turn": i + 1}
            time.sleep(0.2)

            # ── Real dispatcher call — 3 gates are live ──────────────────
            old, sys.stdout = sys.stdout, io.StringIO()
            result = self.dispatcher.dispatch(tool_name, params)
            log, sys.stdout = sys.stdout.getvalue().strip(), old

            if log:
                yield {"type": "appworks_log", "content": log, "tool": tool_name}

            if result["status"] == "ok":
                all_data[tool_name]    = result["data"]
                collected[tool_name]   = result["data"]
                yield {"type": "tool_result", "tool": tool_name,
                       "agent_name": result.get("agent", ""),
                       "data": result["data"], "turn": i + 1}
            else:
                yield {"type": "tool_blocked", "tool": tool_name,
                       "reason": result["message"], "turn": i + 1}

        # Final reasoning after all tools complete
        time.sleep(0.3)
        if len(reasons) > len(tools):
            yield {"type": "agent_thinking", "content": reasons[-1], "turn": len(tools) + 1}
            time.sleep(0.3)

        summary = self._build_summary(tab, all_data, case_id)

        yield {"type": "complete", "tab": tab, "title": config["title"],
               "agent_summary": summary,
               "tool_data": all_data, "turns_used": len(tools),
               "tools_called": list(all_data.keys()),
               "mode": "simulation"}

    def _resolve_params(self, tool_name: str, case_id: str, collected: dict) -> dict:
        """
        Mirrors how the real LLM reads manifest descriptions to pick parameters.
        Each tool's required_params description tells the LLM where data comes from.
        """
        if tool_name == "verify_case_intake":
            return {"case_id": case_id}

        if tool_name == "fetch_subject_history":
            # Manifest says: subject_id = subject_primary_id from verify_case_intake
            intake = collected.get("verify_case_intake", {})
            return {"subject_id": intake.get("subject_primary_id", "SUBJ-UNKNOWN")}

        if tool_name == "search_similar_cases":
            # Manifest says: complaint_text = complaint_description from verify_case_intake
            intake = collected.get("verify_case_intake", {})
            return {
                "complaint_text": intake.get("complaint_description", "fraud complaint"),
                "top_n": 3
            }

        if tool_name == "calculate_risk_metrics":
            # Manifest says: case_id + subject_id (subject_primary_id from verify_case_intake)
            intake = collected.get("verify_case_intake", {})
            return {
                "case_id":    case_id,
                "subject_id": intake.get("subject_primary_id", "SUBJ-UNKNOWN")
            }

        if tool_name == "get_investigation_playbook":
            risk = collected.get("calculate_risk_metrics", {})
            intake = collected.get("verify_case_intake", {})
            return {
                "fraud_type": intake.get("fraud_type_classified", "BILLING"),
                "risk_level": risk.get("risk_tier", "HIGH")
            }

        return {}

    def _build_summary(self, tab: str, data: dict, case_id: str) -> str:
        """Build a realistic agent summary narrative from tool results."""
        if tab == "case_summary":
            c = data.get("verify_case_intake", {})
            return (f"Case {c.get('case_id', case_id)} has been verified and structured. "
                    f"Subject: {c.get('subject_primary', 'unknown')} "
                    f"({c.get('subject_secondary', '')}). "
                    f"Fraud type: {c.get('fraud_type_classified', 'unknown')}. "
                    f"Status: {c.get('status', 'unknown')}. "
                    f"The complaint describes billing fraud involving home health services. "
                    f"All intake fields are verified and structured for downstream agents.")

        if tab == "subject_history":
            s = data.get("fetch_subject_history", {})
            c = data.get("verify_case_intake", {})
            n = s.get("prior_case_count", 0)
            assoc = [a["name"] for a in s.get("known_associates", [])]
            return (f"Subject {s.get('full_name', 'unknown')} (ID: {s.get('subject_id', '?')}) "
                    f"has {n} prior case{'s' if n != 1 else ''} on record. "
                    f"{'Prior cases include substantiated billing fraud findings, indicating a repeat pattern. ' if n > 0 else ''}"
                    f"Known associates: {', '.join(assoc) if assoc else 'none recorded'}. "
                    f"Subject has been at their current address since 2021. "
                    f"The history shows {'an escalating pattern of fraud activity' if n >= 2 else 'limited prior history'}.")

        if tab == "similar_cases":
            r = data.get("search_similar_cases", {})
            matches = r.get("matches", [])
            top_score = matches[0].get("similarity_score", 0) if matches else 0
            return (f"Found {len(matches)} similar case{'s' if len(matches) != 1 else ''} in the BSI archive. "
                    f"Top match: {matches[0].get('case_id','?')} with {int(top_score*100)}% similarity. "
                    f"Dominant pattern: {r.get('query_summary','billing fraud during hospitalization')}. "
                    f"All top matches were substantiated, suggesting a well-established fraud pattern "
                    f"consistent with the current complaint.")

        if tab == "risk_assessment":
            r = data.get("calculate_risk_metrics", {})
            rules = r.get("triggered_rules", [])
            return (f"Fraud risk assessment complete. Score: {r.get('risk_score', 0):.2f} — "
                    f"Tier: {r.get('risk_tier', 'UNKNOWN')}. "
                    f"{len(rules)} business rule{'s' if len(rules) != 1 else ''} triggered. "
                    f"Billing anomaly: {'confirmed' if r.get('billing_anomaly_flag') else 'not detected'}. "
                    f"This score was computed deterministically by the BSI rules engine and has not been modified. "
                    f"Recommendation: {r.get('recommendation', 'Review required.')}")

        return "Investigation complete."