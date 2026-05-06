"""
BSI Agent Runner — LLM agentic loop.
Responsibilities: message history, turn management, provenance_trail accumulation.
Outside its scope: HTTP concerns, section names, UI structure.
"""
import json
import os
from typing import List, Dict, Tuple

from openai import OpenAI

# -----------------------------------------------------------------------
# SYSTEM PROMPT — /investigate (AUTO flow)
# The LLM decides which available tools to call based on their input/output contracts.
# -----------------------------------------------------------------------
INVESTIGATE_SYSTEM_PROMPT = """You are the BSI Fraud Investigation AI Agent for the Bureau of Special Investigations, Massachusetts.

You have access to a set of approved tools connected to the AppWorks case management system. Read each tool description carefully — it tells you exactly what data it needs and what it returns.

YOUR TASK: Investigate the submitted case_id by calling the tools that are necessary to gather case intake, subject history, similar case context, active risk rules, and the deterministic risk assessment. Use the available tool descriptions and the manifest to decide which tools to call and in what or=== HOW TO WORK ===
- Choose tools based on the inputs they require and the outputs they provide.
- CRITICAL: Use verified values from previous tool results as input to later tool calls. For example, pass prior_case_count from fetch_subject_history, similar_case_volume from search_similar_cases, and rules from get_risk_rules to calculate_risk_metrics. This ensures the deterministic scoring engine uses verified data and avoids inconsistent "zero-scores" caused by redundant fetches.
- Stop calling tools once you have enough verified information to produce a complete investigator-facing summary.
- Do not fabricate any data. If a tool returns an error, report it honestly.
- Treat risk_score as a deterministic output from the risk assessment engine and report it exactly as returned.

=== RISK ASSESSMENT — REQUIRED TOOL SEQUENCE ===
1. Call get_risk_rules first (no parameters). It returns { rules: [...] } — the active AppWorks rule dimensions.
2. Call calculate_risk_metrics with case_id, subject_id, fraud_types, active_rules (from step 1), AND all applicable context params (prior_case_count, similar_case_volume, etc.) gathered from prior tool calls.

=== FINAL SUMMARY ===
After gathering all data, write the agent_summary using the following structured format exactly as shown. Use bold headers and bullet points as specified.

### Investigation Summary for Case [case_id]

**Case Summary:**
- **Complaint No:** [value]
- **Description:** [value]
- **Case Description:** [value]
- **Status:** [value]
- **Destination:** [value]
- **Team:** [value]
- **Created:** [value]

**Subject History:**
- **Primary Subject:** [name]
- **Aliases:** [aliases]
- **Prior Case Count:** [count]
- **Co-Subject:** [name]

**Similar Cases:**
- Found [n] similar archived cases across [m] fraud types.

**Risk Assessment:**
- **Risk Score:** [score]
- **Risk Tier:** [tier]
- **Rules Triggered:**
  - [Rule Name 1]: [Brief explanation of trigger]
  - [Rule Name 2]: [Brief explanation of trigger]
- **Total points earned:** [points]/100
- **Recommendation:** [verbatim from tool output]

**Recommended Actions:**
- [Recommendation verbatim from tool output]

WRITING RULES:
- REQUIRED: Start the summary with the '### Investigation Summary for Case [case_id]' header.
- REQUIRED: Use the exact bold headers and bullet point structure shown above.
- NEGATIVE CONSTRAINT: DO NOT write narrative paragraphs.
- NEGATIVE CONSTRAINT: DO NOT use "Paragraph 1", "Paragraph 2" etc. headers.
- Cite actual values from tool outputs — do not invent anything.
- If a field is null or empty, state "Not recorded in AppWorks".
- Ensure the Risk Assessment section explicitly states it was computed by the BSI rules engine.
engine, not by AI inference."""


# -----------------------------------------------------------------------
# SCOPED SYSTEM PROMPTS — ON-DEMAND flows
# -----------------------------------------------------------------------

def build_playbook_prompt(case_data: dict) -> str:
    return f"""You are the BSI Investigation Agent. Your ONLY task is to call the 'get_investigation_playbook' tool for this case.

=== VERIFIED CASE CONTEXT (from prior AppWorks data retrieval) ===
{json.dumps(case_data, indent=2)}
=== END CONTEXT ===

INSTRUCTIONS:
1. Extract fraud_types from the complaint_intelligence section of the context above.
   fraud_types must be a JSON array of strings — e.g. ["Dependent Not in Home", "EAEDC"].

2. Extract risk_tier from the risk_assessment section.
   risk_tier must be one of: LOW, MEDIUM, HIGH, CRITICAL.

3. Call get_investigation_playbook with these two parameters.

4. After the tool returns, produce a concise plain-English summary of:
   - The playbook ID and which fraud types it covers
   - Total number of investigation steps
   - Key escalation requirements (if any)
   - Evidence checklist mandatory items

Do NOT call any other tool. Do NOT fabricate steps."""


def build_report_prompt(
    case_data: dict,
    ai_case_summary: str,
    playbook_data: dict,
    analyst_decision: dict,
) -> str:
    return f"""You are the BSI Investigation Report Agent. Your task is to call the 'generate_final_report' tool and then synthesise all verified findings into a director-ready narrative investigation summary report.

=== VERIFIED INVESTIGATION DATA ===
{json.dumps(case_data, indent=2)}

=== AI CASE SUMMARY (from /investigate) ===
{ai_case_summary or "Not provided."}

=== INVESTIGATION PLAYBOOK (from /playbook) ===
{json.dumps(playbook_data, indent=2)}

=== ANALYST DECISION ===
{json.dumps(analyst_decision, indent=2)}
=== END OF INPUT DATA ===

STEP 1 — TOOL CALL:
Call generate_final_report with these exact parameters extracted from the verified context above:
- case_id from complaint_intelligence.case_id
- subject_id from complaint_intelligence.subject_primary_id
- fraud_types from complaint_intelligence.fraud_types
- risk_score from risk_assessment.risk_score
- risk_tier from risk_assessment.risk_tier
- triggered_rules from risk_assessment.triggered_rules, enriched with matching rule definitions from risk_rules when available

The tool will return the raw AppWorks case data (Workfolder, subjects, allegations, financials, commentary).

STEP 2 — NARRATIVE SYNTHESIS:
After the tool returns, write a formal investigation summary report covering these sections IN ORDER:

1. CASE SUMMARY
   Complaint number, intake date, source, referral number, assigned team, description, and current status — all from tool output.

2. SUBJECT PROFILE
   Full name, identifier (SSN/EIN), type, role, address history, prior case count and case references — from tool output.

3. ALLEGATIONS & FINANCIAL RECORD
   All allegation types with status, dates, and agency. Financial amounts (ordered and calculated) with fraud type and period — from tool output.

4. RISK ASSESSMENT
   Risk score (exact decimal), risk tier, triggered BSI rule IDs and their condition descriptions. State explicitly that the score was computed by the BSI configured rules evaluation engine.

5. INVESTIGATION PLAYBOOK SUMMARY
   Number of steps, key actions, escalation requirements, mandatory evidence items.

6. ANALYST DECISION & NOTES
   Decision outcome, analyst name if available, decision notes. Analyst commentary from AppWorks WorkfolderCommentary entity.

7. RECOMMENDED NEXT ACTIONS
   Based on risk tier and playbook — factual, directive, no speculation.

RULES:
- Every factual claim must reference the AppWorks source (e.g. "per AppWorks Workfolder entity", "per WorkfolderCommentary").
- The risk score is deterministic — never restate it as an estimate or modify the value.
- Do NOT fabricate data. If a field is missing from AppWorks, state "Not recorded in AppWorks".
- Write in formal plain English suitable for a Director of Special Investigations."""


def build_copilot_prompt(case_id: str, case_data: dict) -> str:
    return f"""You are the BSI Investigation Copilot for Case {case_id}.

Your role is to answer investigator questions accurately and concisely, grounded entirely in the verified AppWorks data below.

=== VERIFIED CASE CONTEXT ===
{json.dumps(case_data, indent=2)}
=== END CONTEXT ===

RULES:
1. CONTEXT FIRST: Always attempt to answer from the verified context above before calling any tool.
   If the answer is present in the context, state which section it came from.

2. TOOL FALLBACK: Only call a tool if the question requires data that is genuinely absent from the context.
   Do not call tools to reconfirm data already present.

3. PROVENANCE: When citing a finding, reference the AppWorks source entity.
   Example: "Per the AppWorks Subject entity, the subject's address is..."

4. HONESTY: If data is not available in the context and no tool can retrieve it, say so explicitly.
   Do not speculate or fabricate.

5. CONCISENESS: Give direct, precise answers. Avoid lengthy preambles."""


# -----------------------------------------------------------------------
# RUNNER
# -----------------------------------------------------------------------

class BSIAgentRunner:

    def __init__(self, manifest_path: str, api_key: str = None):
        from semantic_layer.dispatcher import SemanticDispatcher
        from agent_service.tool_builder import build_openai_tools

        self.dispatcher = SemanticDispatcher(manifest_path)
        self.tools = build_openai_tools(self.dispatcher)
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    # ------------------------------------------------------------------
    # AUTO flow — /investigate
    # ------------------------------------------------------------------
    def investigate(self, case_id: str) -> Tuple[List[Dict], List[Dict]]:
        messages = [
            {"role": "system", "content": INVESTIGATE_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Investigate case {case_id}. Use the available tools to gather the relevant "
                "case intake, subject history, similarity context, active risk rules, and deterministic "
                "risk assessment. Then produce a concise investigator-facing summary."
            )},
        ]
        return self._run_loop(
            messages,
            allowed_tool_names=[
                "verify_case_intake",
                "fetch_subject_history",
                "search_similar_cases",
                "get_risk_rules",
                "calculate_risk_metrics",
            ],
        )

    # ------------------------------------------------------------------
    # ON-DEMAND flow — /playbook, /report, /copilot
    # ------------------------------------------------------------------
    def run_scoped(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list | None = None,
        allowed_tool_names: list[str] | None = None,
    ) -> Tuple[List[Dict], List[Dict]]:
        messages: List[Dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})
        return self._run_loop(messages, allowed_tool_names=allowed_tool_names)

    # ------------------------------------------------------------------
    # Core agentic loop
    # ------------------------------------------------------------------
    def _run_loop(
        self,
        messages: List[Dict],
        allowed_tool_names: list[str] | None = None,
    ) -> Tuple[List[Dict], List[Dict]]:
        provenance_trail: List[Dict] = []
        tools = self.tools
        if allowed_tool_names:
            allowed = set(allowed_tool_names)
            tools = [
                tool for tool in self.tools
                if tool.get("function", {}).get("name") in allowed
            ]

        while True:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )

            choice = response.choices[0]
            msg    = choice.message

            # Append assistant turn
            assistant_entry: Dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id":   tc.id,
                        "type": "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_entry)

            # Stop if LLM is done
            if choice.finish_reason == "stop" or not msg.tool_calls:
                break

            # Process every tool call in this turn
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    params = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    params = {}

                envelope = self.dispatcher.dispatch(tool_name, params)

                if envelope.get("status") == "ok":
                    # LLM receives only the result — never the provenance block
                    tool_content = json.dumps(envelope.get("result", {}))
                    # Provenance accumulates separately (CS-7) — strip to spec fields only
                    prov = envelope.get("provenance", {})
                    provenance_trail.append({
                        "tool": tool_name,
                        "sources": prov.get("sources", []),
                        "retrieved_at": prov.get("retrieved_at", ""),
                        "computed_by": prov.get("computed_by", ""),
                    })
                else:
                    # Gate/execution error — LLM sees it and can self-correct
                    tool_content = json.dumps(envelope)

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      tool_content,
                })

        return messages, provenance_trail