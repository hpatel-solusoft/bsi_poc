# BSI Fraud Investigation Platform
## Architecture Reference Document
### Version 3.0 — POC

---

> **Purpose of this document**
> This document provides a complete technical reference for the BSI Fraud
> Investigation Platform — an AI agentic workflow built on OpenText AppWorks.
> It covers the three-layer architecture, all context storage points, complete
> execution sequences for every workflow phase, endpoint responsibilities, and
> the design principles that govern how components interact.
>
> It is intended to serve as the single reference point during implementation
> so that design intent, execution flow, and component boundaries are
> understood consistently across the team.
>
---

## Section 1 — The Three Layers

Every request in this system passes through three layers in order.
Each layer has a single responsibility. No layer interacts with
the layer below it directly — communication flows downward through
defined interfaces only.

```
┌─────────────────────────────────────────────────────────┐
│  LAYER 1 — API Layer          api/webhook.py             │
│  Receives HTTP requests. Triggers agent. Returns JSON.   │
│  Has no knowledge of tools, agents, or AppWorks.         │
├───────────────────────────────────────────────────────── ┤
│  LAYER 2 — Agent + Semantic   agent_runner.py            │
│            Layer              dispatcher.py              │
│                               tool_builder.py            │
│                               manifest.yaml              │
│  The LLM decides what to call. The dispatcher validates. │
│  No direct function calls. No hardcoded sequences.       │
├───────────────────────────────────────────────────────── ┤
│  LAYER 3 — Service Layer      appworks_services.py       │
│  Python functions that call AppWorks REST APIs.          │
│  Reached only via the dispatcher in Layer 2.             │
│  Not directly accessible from Layer 1.                   │
└─────────────────────────────────────────────────────────┘
```



## Section 2 — Context Storage: What It Is and Where It Lives

The word "context" refers to different things at different points
in the execution. There are seven distinct context types in this
system. Understanding all seven is required to implement the
ON-DEMAND flows and the Copilot correctly.

```
┌──────────────────────────────────────────────────────────────────┐
│  CS-1  LLM TURN CONTEXT                                          │
│  Location: messages[] list in agent_runner.py                    │
│  What it is: The growing conversation between the system, user,  │
│  LLM, and tool results. Every tool result is appended here as    │
│  role:"tool". The LLM reads this on every turn to decide the     │
│  next step. Exists only for the duration of the agent loop.      │
├──────────────────────────────────────────────────────────────────┤
│  CS-2  TOOL RESULT CONTEXT                                       │
│  Location: role:"tool" messages inside CS-1                      │
│  What it is: The raw JSON returned by each tool call, appended   │
│  to the message history by agent_runner.py after every           │
│  dispatcher.dispatch() call. This is what the LLM reasons over   │
│  to decide the next tool.                                        │
├──────────────────────────────────────────────────────────────────┤
│  CS-3  RESPONSE CONTEXT                                          │
│  Location: webhook.py _extract_tool_results()                    │
│  What it is: After the agent loop ends, the webhook parses CS-1  │
│  to extract each tool's result into named sections. This becomes │
│  the structured JSON response returned to the frontend. The      │
│  frontend renders all sections into the case summary panel.      │
├──────────────────────────────────────────────────────────────────┤
│  CS-4  CASE SESSION CONTEXT                                      │
│  Location: CASE_STORE dict in webhook.py (in-memory for POC,     │
│  Redis or DB in production)                                      │
│  What it is: A cache-aside layer. After /investigate completes,  │
│  the extracted sections are stored here keyed by case_id. On     │
│  subsequent ON-DEMAND requests, webhook.py checks CS-4 first.    │
│  If populated (same server session), it uses CS-4 directly. If   │
│  empty (new session or server restart), it falls back to the     │
│  ai_investigation_data field sent in the request body by the frontend. │
│  CS-4 is never the authoritative store — AppWorks case fields    │
│  are. CS-4 is a within-session performance cache only.           │
├──────────────────────────────────────────────────────────────────┤
│  CS-5  COPILOT INJECTED CONTEXT                                  │
│  Location: System prompt string built in webhook.py /copilot     │
│  What it is: The full stored case data from CS-4 is serialised   │
│  and injected into the LLM system prompt before each Copilot     │
│  request. The LLM answers from this context. A tool call is      │
│  made only when the question requires data not present here.     │
├──────────────────────────────────────────────────────────────────┤
│  CS-6  COPILOT CONVERSATION HISTORY                              │
│  Location: conversation_history[] field in request body          │
│  What it is: The frontend maintains and sends the full prior     │
│  conversation on every Copilot request. The server is stateless  │
│  per request. Conversation continuity is the frontend's          │
│  responsibility.                                                 │
├──────────────────────────────────────────────────────────────────┤
│  CS-7  PROVENANCE TRAIL                                          │
│  Location: provenance_trail[] accumulated in agent_runner.py,    │
│  extracted and stored by webhook.py                              │
│  What it is: After every dispatcher.dispatch() call, the         │
│  provenance block returned by appworks_services.py is appended   │
│  to a running list — one entry per tool call. Each entry         │
│  records which AppWorks data sources were read, when they were   │
│  retrieved, and how the result was produced (direct REST         │
│  retrieval or BSI rules evaluation). The full trail is included  │
│  in the HTTP response as provenance_trail[] and persisted in     │
│  CASE_STORE alongside the investigation sections. This is what   │
│  makes every finding in the investigation traceable back to a    │
│  named AppWorks record and a timestamp.                          │
└──────────────────────────────────────────────────────────────────┘
```

---

## Section 3 — Complete Execution Sequences

### 3.1 AUTO Flow — POST /investigate
**Trigger:** AppWorks complaint form submitted or on Click of "AI Insights". Runs tools 1–4 automatically.

```
AppWorks submits: POST /investigate  { "case_id": "BSI-2024-00421" }
│
├── webhook.py
│     checks OPENAI_API_KEY present
│     checks manifest.yaml exists
│     creates BSIAgentRunner(manifest_path)
│     calls runner.investigate("BSI-2024-00421")
│
├── agent_runner.py  ←  BSIAgentRunner.investigate()
│     builds initial messages list:
│       messages = [
│         { role:"system",  content: SYSTEM_PROMPT },        ← CS-1 STARTS
│         { role:"user",    content: "investigate BSI-2024-00421" }
│       ]
│     calls build_openai_tools(dispatcher) to get tool catalogue
│
├── tool_builder.py  ←  build_openai_tools(dispatcher)
│     reads dispatcher.get_tool_catalogue()
│     wraps each tool in OpenAI function schema format
│     returns tools list to agent_runner
│     [ LLM will see: verify_case_intake, fetch_subject_history,
│       search_similar_cases, get_risk_rules, calculate_risk_metrics,
│       get_investigation_playbook, generate_final_report ]
│       NOTE: get_risk_rules is intentionally visible — the LLM calling
│       it as an explicit tool call demonstrates that BSI fraud detection
│       rules are fetched from AppWorks at runtime, not embedded in code.
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 1                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o  (receives messages + tool catalogue)
│     reads tool descriptions from manifest
│     DECIDES: "I need case data first → verify_case_intake"
│     returns: finish_reason="tool_calls"
│               tool_call: { name:"verify_case_intake",
│                            params:{ case_id:"BSI-2024-00421" } }
│
├── agent_runner.py
│     appends LLM assistant message to messages          ← CS-1 GROWS
│     reads tool_call.function.name = "verify_case_intake"
│     reads tool_call.function.arguments = { case_id: "BSI-2024-00421" }
│
├── dispatcher.py  ←  SemanticDispatcher.dispatch("verify_case_intake",
│                                                  { case_id: "BSI-2024-00421" })
│     GATE 1: "verify_case_intake" in manifest.yaml?     ✓ PASS
│     GATE 2: "case_id" param present?                   ✓ PASS
│     GATE 3: resolve appworks_services.get_case_header  ✓ PASS
│     passes full envelope from appworks_services through unchanged:
│       { status:"ok",
│         result: { case_id, subject_id,
│                   fraud_types: ["BILLING","NETWORK"],
│                   procedure_codes: ["99213"],
│                   complaint_description,
│                   key_persons: [...], linked_organisations: [...],
│                   prior_case_ids: [...], estimated_loss },
│         provenance: { sources: ["AppWorks case record BSI-2024-00421"],
│                       retrieved_at: "2025-04-27T14:32:00Z",
│                       computed_by:  "AppWorks REST retrieval" } }
│
├── appworks_services.py  ←  get_case_header("BSI-2024-00421")
│     calls AppWorks REST: GET /appworks/rest/v1/cases/BSI-2024-00421/header
│     returns standard envelope:
│       { result: { ...case header fields... },
│         provenance: { sources: ["AppWorks case record BSI-2024-00421"],
│                       retrieved_at: datetime.utcnow().isoformat(),
│                       computed_by:  "AppWorks REST retrieval" } }
│
├── agent_runner.py
│     appends tool result to messages:                   ← CS-2 STORED
│       { role:"tool",
│         tool_call_id: "tc_abc123",
│         content: JSON.stringify(envelope.result) }     ← LLM sees result only
│     appends to provenance_trail[]:                     ← CS-7 GROWS
│       { tool: "verify_case_intake", ...envelope.provenance }
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 2                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o  (receives updated messages including tool result)
│     reads case data — sees subject_primary_id = "SUBJ-7821"
│     DECIDES: "I have case data. Now I need subject history →
│               fetch_subject_history"
│     returns: tool_call: { name:"fetch_subject_history",
│                           params:{ subject_id:"SUBJ-7821" } }
│
├── dispatcher.py  ←  all three gates pass
├── appworks_services.py  ←  get_enriched_subject_profile("SUBJ-7821")
│
├── agent_runner.py
│     appends tool result to messages                    ← CS-2 STORED
│     appends to provenance_trail[]                      ← CS-7 GROWS
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 3                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     reads subject history — has fraud_types: ["BILLING","NETWORK"]
│     DECIDES: "I need similar cases →
│               search_similar_cases"
│     returns: tool_call: { name:"search_similar_cases",
│                           params:{ fraud_types:["BILLING","NETWORK"] } }
│     NOTE: fraud_types is a list — results grouped by fraud type.
│     Config block in manifest governs lookback years, max results
│     per type, and required case status. LLM does not decide these.
│
├── dispatcher.py → appworks_services.py → agent_runner.py
│     appends tool result to messages                    ← CS-2 STORED
│     appends to provenance_trail[]                      ← CS-7 GROWS
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 4a — fetch BSI fraud detection rules           ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     DECIDES: "I need the active fraud detection rules before
│               calculating risk → get_risk_rules"
│     returns: tool_call: { name:"get_risk_rules", params:{} }
│     NOTE: This tool call is intentionally explicit and visible.
│     It demonstrates that BSI fraud detection rules are fetched
│     from AppWorks at runtime — not hardcoded in the application.
│     BSI can add, modify or deactivate rules in AppWorks without
│     any code change.
│
├── dispatcher.py → appworks_services.py → agent_runner.py
│     returns active rules from AppWorks configurable rules table:
│       { result: { rules: [
│           { rule_id:"R-101", condition:"prior_cases >= 2",
│             weight: 0.3, active: true },
│           { rule_id:"R-205", condition:"linked_provider exists",
│             weight: 0.25, active: true },
│           { rule_id:"R-312", condition:"billing_freq > 3x baseline",
│             weight: 0.25, active: true },
│           { rule_id:"R-408", condition:"risk_escalation_across_cases",
│             weight: 0.2, active: true }
│         ]},
│         provenance: { sources: ["AppWorks BSI fraud detection rules table"],
│                       retrieved_at: "2025-04-27T14:32:04Z",
│                       computed_by:  "AppWorks REST retrieval" } }
│     appends result to messages                         ← CS-2 STORED
│     appends to provenance_trail[]                      ← CS-7 GROWS
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 4b — evaluate rules, produce risk score        ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     has active rules from Turn 4a
│     DECIDES: "I have the rules. Now calculate risk →
│               calculate_risk_metrics"
│     returns: tool_call: { name:"calculate_risk_metrics",
│                           params:{ case_id:"BSI-2024-00421",
│                                    subject_id:"SUBJ-7821",
│                                    fraud_types:["BILLING","NETWORK"] } }
│     NOTE: fraud_types is passed so the rules engine evaluates
│     only the rule categories relevant to the submitted fraud types.
│
├── dispatcher.py → appworks_services.py → agent_runner.py
│     rules engine evaluates active rules against AppWorks case and
│     billing data. Score is deterministic — LLM is not involved.
│     returns envelope:
│       { result: { case_id, risk_score: 0.87, risk_tier: "HIGH",
│                   triggered_rules: ["R-101","R-205","R-312"] },
│         provenance: { sources: ["AppWorks case record BSI-2024-00421",
│                                 "AppWorks subject record SUBJ-7821",
│                                 "AppWorks BSI fraud detection rules table"],
│                       retrieved_at: "2025-04-27T14:32:06Z",
│                       computed_by:  "BSI configured rules evaluation" } }
│     appends result to messages                         ← CS-2 STORED
│     appends to provenance_trail[]                      ← CS-7 GROWS
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 5 — LLM produces final summary                 ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     has: case data, subject history, similar cases,
│          active rules, risk score and triggered rules
│     DECIDES: "AUTO phase complete. No further tools required."
│     returns: finish_reason="stop"
│              content: "Investigation complete. Risk score 0.87 HIGH.
│                        Three rules triggered. Subject has 2 prior cases..."
│
├── agent_runner.py
│     sees finish_reason="stop"
│     exits loop
│     returns full messages list AND provenance_trail[] to webhook.py
│
├── webhook.py  ←  back from runner.investigate()
│     calls _extract_tool_results(messages)             ← CS-3 BUILT
│       reads TOOL_TO_SECTION built dynamically from manifest.yaml
│         TOOL_TO_SECTION = {
│           tool["name"]: tool["section"]
│           for tool in manifest["tools"]
│           if "section" in tool
│         }
│       scans messages for role:"tool" entries
│       maps each tool name to its section label via TOOL_TO_SECTION
│       returns { <section>: <result> } for every tool that ran
│         — no hardcoded section names, no hardcoded tool names
│         — adding a new tool with a section field in manifest.yaml
│           automatically adds a new key to this response
│
│     calls _extract_agent_summary(messages)
│       returns LLM's final text summary
│
│     calls _extract_provenance_trail(messages)         ← CS-7 EXTRACTED
│       reads provenance_trail[] accumulated by agent_runner
│       returns ordered list — one entry per tool call that ran:
│         each entry contains the tool name and the provenance block
│         returned by appworks_services.py for that call
│         { tool: <tool_name>,
│           sources: [...],
│           retrieved_at: <timestamp>,
│           computed_by: <method> }
│       the list length and tool names are determined at runtime
│       by which tools the LLM called — not hardcoded
│
│     CASE_STORE["BSI-2024-00421"] = {                  ← CS-4 STORED
│       ...sections,                                     ← investigation data
│       provenance_trail: [...]                          ← CS-7 PERSISTED
│     }
│       Persisted in memory (Redis/DB in production).
│       Read by /playbook, /report, /copilot so those
│       endpoints do not re-run the full investigation.
│
│     returns HTTP 200:
│       {
│         case_id:       "BSI-2024-00421",
│         status:        "completed",
│         agent_summary: "Investigation complete. Risk score 0.87 HIGH.
│                         Three rules triggered. Subject has 2 prior
│                         cases...",              ← NATURAL LANGUAGE
│                                                   displayed on screen
│                                                   saved to ai_case_summary
│         investigation: {
│           <section>: { ... }   ← STRUCTURED JSON, one key per tool,
│           ...                    section name from manifest.yaml
│         },                       saved to ai_investigation_data
│                                  used by /playbook /report /copilot
│         provenance_trail: [ ...one entry per tool call... ],
│         meta: { tool_calls_made:5, duration_seconds:9.1, ... }
│       }
│
└── FRONTEND
      Case summary panel renders agent_summary as natural language
      narrative alongside the Copilot chat interface.
      The investigator reads agent_summary — not raw JSON.

      Analyst reviews the summary on screen. When satisfied, the
      analyst uses the native BSI AppWorks save action to commit
      both fields to the case record in one action:

        ai_case_summary      = agent_summary
                               (natural language — what the analyst read
                                and approved. Displayed on screen when
                                case is reopened.)

        ai_investigation_data = investigation{}
                               (structured JSON sections — used by
                                /playbook, /report, /copilot as
                                machine-readable context. Never
                                displayed directly to the investigator.)

      This is a single BSI UI save action — not an agent write.
      The agent service has no write path to AppWorks.
      READONLY is preserved end to end.

      If the case is subsequently modified in AppWorks (new allegation
      added, subject details updated), AppWorks workflow rules set
      the reload_ai_summary field to true. When the case is next
      opened, the AI Insights panel shows a banner prompting the
      analyst to re-run the investigation. The analyst re-runs,
      reviews, and saves again. The agent service is not involved
      in detecting or acting on this flag — it is a BSI UI concern.
```

---

### 3.2 ON-DEMAND Flow — POST /playbook
**Trigger:** Reviewer clicks "Load Investigation Playbook" button in BSI UI.

The frontend always sends the ai_investigation_data it has in the
request body. This is the structured JSON sections from the /investigate
response — not the natural language agent_summary. The system needs
structured data to build the scoped prompt reliably. This is the same
stateless pattern as /copilot sending conversation_history. If CS-4
is populated (same session), webhook.py uses it and ignores the body
field. If CS-4 is empty (new session), webhook.py reads from the
request body. No AppWorks read is required either way.

```
Reviewer clicks button → POST /playbook
  {
    "case_id":              "BSI-2024-00421",
    "ai_investigation_data": {    ← structured JSON from /investigate
      "complaint_intelligence": { fraud_types: ["BILLING","NETWORK"], ... },
      "risk_assessment":        { risk_score: 0.87, risk_tier: "HIGH",
                                  triggered_rules: [...], ... },
      "context_enrichment":     { prior_cases: [...], ... },
      "similar_cases":          { matches: [...], ... }
    }
  }
│
├── webhook.py
│     checks CS-4 — if populated use it, skip ai_investigation_data field
│     if CS-4 empty: reads request.ai_investigation_data     ← CS-4 POPULATED
│       CASE_STORE["BSI-2024-00421"] = request.ai_investigation_data
│
│     passes full ai_investigation_data as context into scoped prompt
│       no static field extraction — the full JSON is serialised
│       and injected directly. The LLM reads what it needs from it.
│       This means adding a new investigation section automatically
│       enriches the playbook prompt with no code change.
│
│     creates BSIAgentRunner(manifest_path)
│     builds scoped prompt with full case context:
│       "Retrieve the investigation playbook appropriate for this case.
│        Here is the full investigation data:
│        { ...full ai_investigation_data serialised as JSON... }
│        Tailor the recommended investigation steps specifically to
│        this case — prioritise steps relevant to the established
│        fraud pattern, reference prior case findings where relevant,
│        and flag evidence already available versus what needs to
│        be obtained."
│
├── agent_runner.py
│     builds scoped messages:
│       messages = [
│         { role:"system", content: scoped_prompt_with_context },
│         { role:"user",   content:
│             "Load investigation playbook for case BSI-2024-00421" }
│       ]
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 1                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     reads full case context in scoped prompt
│     DECIDES: "get_investigation_playbook(["BILLING","NETWORK"], HIGH)"
│     returns: tool_call: { name:"get_investigation_playbook",
│                           params:{ fraud_types:["BILLING","NETWORK"],
│                                    risk_tier:"HIGH" } }
│     NOTE: fraud_types is a list — playbook covers investigation
│     steps for all submitted fraud types. risk_tier replaces
│     risk_level to align with the canonical RiskAssessment entity.
│
├── dispatcher.py  ←  all three gates pass
├── appworks_services.py  ←  get_playbook_by_type("BILLING", "HIGH")
│
├── agent_runner.py
│     appends tool result to messages                    ← CS-2 STORED
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 2 — LLM tailors playbook to case context       ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     reads raw playbook from tool result
│     reads full case context from scoped prompt
│     synthesises: tailored playbook with case-specific step ordering,
│     evidence notes referencing prior case findings, priority flags
│     based on triggered rules
│     returns: finish_reason="stop"
│
├── agent_runner.py → returns messages to webhook.py
│
├── webhook.py
│     extracts investigation_playbook section            ← CS-3 BUILT
│     updates CASE_STORE["BSI-2024-00421"]
│       with investigation_playbook section              ← CS-4 UPDATED
│
│     returns HTTP 200:
│       {
│         case_id: "BSI-2024-00421",
│         status:  "completed",
│         investigation: {
│           investigation_playbook: { ... }
│         }
│       }
│
└── FRONTEND
      Case summary panel updates with tailored playbook.
      Reviewer approves, modifies, or rejects.
      Analyst decision is saved to AppWorks case field
      via native BSI UI action — not via agent endpoint.
```

---

### 3.3 ON-DEMAND Flow — POST /report
**Trigger:** Director clicks "Generate Investigation Summary" button.

```
Director clicks button → POST /report
  {
    "case_id":               "BSI-2024-00421",
    "ai_case_summary":       "Investigation complete. Risk score 0.87...",
                              ← natural language narrative — woven into
                                 report tone and framing
    "ai_investigation_data": { ...full structured investigation sections... },
                              ← structured JSON — factual grounding for
                                 report synthesis
    "ai_playbook":           { ...tailored playbook from /playbook... },
    "analyst_decision": {
      "decision":   "APPROVED",
      "notes":      "Risk assessment confirmed. Proceed with full audit.",
      "decided_by": "Analyst Jane Smith",
      "decided_at": "2025-04-27T15:10:00Z"
    }
  }
│
├── webhook.py
│     checks CS-4 — if populated use it, skip body fields
│     if CS-4 empty: reads request.ai_investigation_data  ← CS-4 POPULATED
│     validates all prior phases present:
│       ai_investigation_data.risk_assessment present? if not → 400
│       ai_playbook present?                           if not → 400
│       analyst_decision.decision == "APPROVED"?       if not → 400
│         clear message: report requires analyst approval first
│
│     builds scoped prompt with complete case context:
│       all investigation sections + playbook + analyst decision
│       LLM task: synthesise into a director-ready narrative report
│
├── agent_runner.py → OpenAI GPT-4o
│     DECIDES: "generate_final_report(BSI-2024-00421)"
│     tool_call: { name:"generate_final_report",
│                  params:{ case_id:"BSI-2024-00421" } }
│
├── dispatcher.py → appworks_services.py
│     compile_and_render_report("BSI-2024-00421")
│     returns report template + case metadata envelope
│
├── OpenAI GPT-4o  (Turn 2)
│     reads report template + full case context from scoped prompt
│     synthesises: narrative report weaving all findings together —
│     intake summary, enrichment patterns, risk rationale, playbook
│     steps taken, analyst decision and notes
│     finish_reason="stop"
│
├── webhook.py
│     extracts final_report section                     ← CS-3 BUILT
│     updates CASE_STORE["BSI-2024-00421"]
│       with final_report section                       ← CS-4 UPDATED
│
│     returns HTTP 200:
│       {
│         case_id: "BSI-2024-00421",
│         status:  "completed",
│         investigation: {
│           final_report: { ... }
│         }
│       }
│
└── FRONTEND
      Case summary panel updates with final report.
      Director reviews. Report is saved to AppWorks case field
      via native BSI UI action — not via agent endpoint.
```

---

### 3.4 ON-DEMAND Flow — POST /copilot
**Trigger:** Investigator sends a message in the chat panel. Every message.

```
Investigator types: "Why is the risk score so high?"

POST /copilot  {
  "case_id":               "BSI-2024-00421",
  "question":              "Why is the risk score so high?",
  "ai_investigation_data": { ...structured JSON investigation sections... },
                             ← system context — not displayed to user
                               used to build injected prompt reliably
  "conversation_history":  []   ← empty on first message,
                                   grows with every exchange

  Conversation history size and duration:
  The frontend appends every user + assistant exchange to this list.
  There is no automatic truncation in the POC — the full history
  is sent on every request. In practice this means:
  - Each exchange adds two messages (user + assistant)
  - A 10-question Copilot session sends 20 messages of history
  - The LLM context window is the practical ceiling
  The practical limit is determined by the model context window budget
  minus the size of the injected ai_investigation_data. A sliding
  window keeping the last N exchanges is the production approach.
  The server never enforces a limit — it processes whatever it receives.
}
│
├── webhook.py
│     checks CS-4 — if populated use it, skip ai_investigation_data field
│     if CS-4 empty: reads request.ai_investigation_data  ← CS-4 POPULATED
│       CASE_STORE["BSI-2024-00421"] = request.ai_investigation_data
│
│     builds CONTEXT-INJECTED system prompt:            ← CS-5 BUILT
│       "You are the BSI Investigation Copilot for case BSI-2024-00421.
│
│        The following investigation data has already been retrieved
│        and verified. Use it to answer questions.
│
│        --- VERIFIED CASE CONTEXT ---
│        { ...full CASE_STORE[case_id] serialised as JSON... }
│        --- END CONTEXT ---
│
│        Rules:
│        - Answer from the context above whenever possible.
│        - Only call a tool if the question requires data not in context.
│        - State which section of the context the answer came from.
│        - When citing a finding, reference the provenance entry for that
│          section — name the AppWorks source and when it was retrieved.
│        - Do not fabricate case data."
│
│     builds conversation history messages from request  ← CS-6 READ
│
├── agent_runner.py
│     builds messages:
│       messages = [
│         { role:"system", content: injected_context_prompt }, ← CS-5
│         ...conversation_history (all prior turns),           ← CS-6
│         { role:"user", content: "Why is the risk score so high?" }
│       ]
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 1                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     reads injected context — risk_score=0.87, risk_tier=HIGH
│     reads triggered_rules: [R-101, R-205, R-312]
│     reads provenance for risk_assessment section:
│       computed_by: "BSI configured rules evaluation"
│       sources: AppWorks case + subject records + rules table
│     DECIDES: "Answer available in context. No tool call needed."
│     returns: finish_reason="stop"
│              content: "The risk score of 0.87 is HIGH because three
│                        rules were triggered:
│                        R-101: Subject has 2 prior cases within 3 years.
│                        R-205: Prior substantiated billing fraud case.
│                        R-312: Claim volume spike — 47 claims in 6 months.
│                        Source: risk_assessment — produced by BSI
│                        configured rules against AppWorks records,
│                        retrieved 2025-04-27T14:32:06Z."
│
├── agent_runner.py → returns messages to webhook.py
│
├── webhook.py
│     returns HTTP 200:
│       {
│         answer:          "The risk score of 0.87 is HIGH because...",
│         sources_cited:   ["risk_assessment — BSI configured rules evaluation,
│                            AppWorks records retrieved 2025-04-27T14:32:06Z"],
│         tool_calls_made: 0
│       }
│
└── FRONTEND
      Chat panel displays answer with source citation.
      conversation_history updated with this exchange.  ← CS-6 GROWS
      Next message sends updated conversation_history.

─────────────────────────────────────────────────────────────
  SECOND QUESTION — targeted tool call required
─────────────────────────────────────────────────────────────

Investigator types: "Are there any newer similar cases since last week?"

POST /copilot  {
  "case_id":   "BSI-2024-00421",
  "question":  "Are there any newer similar cases since last week?",
  "conversation_history": [
    { role:"user",      content:"Why is the risk score so high?" },
    { role:"assistant", content:"The risk score of 0.87 is HIGH because..." }
  ]
}
│
├── webhook.py → agent_runner.py
│     messages = [
│       { role:"system",    content: injected_context },     ← CS-5
│       { role:"user",      content: "Why is the risk..." }, ← CS-6
│       { role:"assistant", content: "The risk score..." },  ← CS-6
│       { role:"user",      content: "Are there any newer..." }
│     ]
│
├── OpenAI GPT-4o
│     reads context — similar_cases already present but from intake time
│     DECIDES: "Question asks for newer cases. Fresh tool call required."
│     returns: tool_call: { name:"search_similar_cases",
│                           params:{ fraud_types:["BILLING","NETWORK"] } }
│
├── dispatcher.py  ←  Gate 1, 2, 3 pass
├── appworks_services.py  ←  fresh search
│
├── agent_runner.py
│     appends fresh result to messages               ← CS-2 STORED
│
├── OpenAI GPT-4o
│     returns: finish_reason="stop", answer with fresh results
│
└── webhook.py  → returns answer
      Chat panel updates.
      Frontend appends to conversation_history.       ← CS-6 GROWS

─────────────────────────────────────────────────────────────
  THIRD QUESTION — tool not in manifest
─────────────────────────────────────────────────────────────

Investigator types: "Can you pull the provider's licence status?"

  (get_provider_license is not registered in manifest.yaml)
│
├── OpenAI GPT-4o
│     reads context — provider_id present but no licence data
│     DECIDES: "Need licence data. Call get_provider_license."
│     returns: tool_call: { name:"get_provider_license",
│                           params:{ provider_id:"PROV-441" } }
│
├── dispatcher.py
│     GATE 1: "get_provider_license" in manifest.yaml?  ✗ FAIL
│     returns structured error to agent_runner:
│       { status:"error",
│         message:"Tool 'get_provider_license' is not registered.
│                  Available tools: verify_case_intake,
│                  fetch_subject_history, search_similar_cases,
│                  get_risk_rules, calculate_risk_metrics,
│                  get_investigation_playbook, generate_final_report" }
│
├── agent_runner.py
│     appends error as tool result to messages         ← CS-2 STORED
│
├── OpenAI GPT-4o
│     reads gate error — tool not available
│     DECIDES: "Cannot retrieve this data. Inform the investigator."
│     returns: finish_reason="stop"
│              content: "Provider licence status is not available
│                        through the current investigation tools.
│                        This data source has not been configured.
│                        Please check AppWorks directly for licence
│                        records, or contact your system administrator
│                        to have this tool added."
│
└── webhook.py  → returns answer
      Investigator sees a clear, honest response.
      No fabricated data. No silent failure.
      No crash. Gate 1 made the system safe.
```

---

## Section 4 — Context Storage Reference Table

| ID | Name | Location | Created by | Read by | Lifespan |
|---|---|---|---|---|---|
| CS-1 | LLM Turn Context | `messages[]` in agent_runner.py | agent_runner.py init | GPT-4o on every turn | Duration of agent loop |
| CS-2 | Tool Result Context | `role:"tool"` entries in CS-1 | agent_runner.py after each dispatch | GPT-4o on next turn | Duration of agent loop |
| CS-3 | Response Context | `sections{}` dict in webhook.py | `_extract_tool_results()` | HTTP response builder, frontend case summary panel | Duration of request |
| CS-4 | Case Session Context | `CASE_STORE[case_id]` in webhook.py | `/investigate` endpoint or request body ai_investigation_data | `/playbook`, `/report`, `/copilot` | Server session (POC) — falls back to request body if empty |
| CS-5 | Copilot Injected Context | System prompt string | webhook.py `/copilot` handler | GPT-4o system turn | Duration of request |
| CS-6 | Copilot Conversation History | `conversation_history[]` in request body | Frontend (appends each exchange) | agent_runner.py messages build | Frontend session |
| CS-7 | Provenance Trail | `provenance_trail[]` in agent_runner.py; extracted by webhook.py | agent_runner.py after each dispatch | webhook.py response builder; CASE_STORE; Copilot injected context | Duration of request; persisted in CASE_STORE |

---

## Section 5 — Endpoint Reference

| Endpoint | Trigger | Tools called | Reads from | Writes to | Phase |
|---|---|---|---|---|---|
| `GET /health` | Browser / monitoring | None | Nothing | Nothing | — |
| `POST /investigate` | AppWorks form submission | 1–5 (LLM decides — includes get_risk_rules) | manifest.yaml | CS-4 CASE_STORE | AUTO |
| `POST /playbook` | Reviewer clicks button | Tool 6 only | CS-4 or request body ai_investigation_data | CS-4 updated | ON-DEMAND |
| `POST /report` | Director clicks button | Tool 7 only | CS-4 or request body ai_investigation_data + ai_case_summary + ai_playbook + analyst_decision | CS-4 updated | ON-DEMAND |
| `POST /copilot` | Investigator sends message | 0 or 1 (LLM decides) | CS-4 or request body ai_investigation_data | CS-4 if tool called | ON-DEMAND |

---

## Section 6 — File Responsibilities

Each file has a single, bounded responsibility. When a change is needed,
this table identifies which file owns that concern.

| File | Responsible for | Outside its scope |
|---|---|---|
| `api/webhook.py` | HTTP endpoints, CASE_STORE, response shaping, provenance trail extraction and persistence | Calling appworks_services directly. Knowing tool names beyond TOOL_TO_SECTION. |
| `agent_service/agent_runner.py` | LLM loop, message history, turn management, provenance_trail[] accumulation across turns | HTTP concerns, tab names, UI section structure. |
| `agent_service/tool_builder.py` | Converting manifest catalogue to OpenAI tool schema | Knowledge of specific tools. It is intentionally generic. |
| `semantic_layer/dispatcher.py` | Three validation gates, tool routing, passing the full {result, provenance} envelope through unchanged | Being bypassed or called around for any reason. Modifying or stripping provenance blocks. |
| `semantic_layer/appworks_services.py` | AppWorks REST API calls, constructing the provenance block on every response | Being called from any layer other than dispatcher.py. |
| `manifest.yaml` | Tool contracts — names, descriptions, params, function mappings, section labels for response building | Ordering instructions or hardcoded sequences. |

---

## Section 7 — Adding a New Tool

When a new tool is required — for example `check_provider_license` —
two files are involved.

**File 1: manifest.yaml — add one entry**
```yaml
- name: "check_provider_license"
  section: "provider_analysis"       ← webhook.py reads this to name
                                       the response section dynamically
  description: >
    Checks whether a provider licence is active or suspended in AppWorks.
    Requires: provider_id (from verify_case_intake output).
    Returns: licence_status, suspension_dates, issuing_authority.
  python_function: "appworks_services.get_provider_license"
  required_params:
    - name: "provider_id"
      type: "string"
      description: "Provider identifier from verify_case_intake output"
  config:
    include_suspension_history: true
```

**For reference — corrected entries for existing tools:**
```yaml
- name: "search_similar_cases"
  section: "similar_cases"
  description: >
    Searches AppWorks closed case archive for cases matching the
    submitted fraud types. Returns top matching cases with outcomes,
    estimated loss, and case summaries grouped by fraud type.
    Requires: fraud_types list from verify_case_intake output.
  python_function: "appworks_services.search_similar_cases"
  required_params:
    - name: "fraud_types"
      type: "list[string]"
      description: "List of fraud types from verify_case_intake — drives similarity search"
  config:
    similarity_lookback_years: 3
    max_results_per_type: 3
    required_status: "Closed"

- name: "calculate_risk_metrics"
  section: "risk_assessment"
  description: >
    Evaluates active BSI fraud detection rules against the case and
    subject data. Returns risk score (0-1), risk tier, and triggered rules.
    Requires: case_id, subject_id, fraud_types.
    Call get_risk_rules first to fetch the active rules.
  python_function: "appworks_services.calculate_risk_metrics"
  required_params:
    - name: "case_id"
      type: "string"
    - name: "subject_id"
      type: "string"
    - name: "fraud_types"
      type: "list[string]"
      description: "List of fraud types — determines which rule categories are evaluated"

- name: "get_investigation_playbook"
  section: "investigation_playbook"
  description: >
    Retrieves the investigation playbook from AppWorks for the given fraud
    types and risk tier. Returns ordered investigation steps and evidence
    checklist. Requires: fraud_types list and risk_tier from
    calculate_risk_metrics output.
  python_function: "appworks_services.get_investigation_playbook"
  required_params:
    - name: "fraud_types"
      type: "list[string]"
      description: "List of fraud types from verify_case_intake — playbook covers all types"
    - name: "risk_tier"
      type: "string"
      description: "Risk tier from calculate_risk_metrics output: LOW/MEDIUM/HIGH/CRITICAL"
```

**File 2: appworks_services.py — add one function**
```python
def get_provider_license(provider_id: str) -> dict:
    _mock_http("GET", f"/appworks/rest/v1/providers/{provider_id}/license")
    return {
        "result": { "licence_status": "SUSPENDED", "suspension_date": "2024-01-15" },
        "provenance": {
            "sources":      [f"AppWorks provider record {provider_id}"],
            "retrieved_at": datetime.utcnow().isoformat(),
            "computed_by":  "AppWorks REST retrieval"
        }
    }
```

**All other files — no changes required:**

| File | Reason no change is needed |
|---|---|
| `dispatcher.py` | Reads manifest dynamically at startup — new tool is automatically registered |
| `tool_builder.py` | Converts whatever the dispatcher holds — no tool-specific knowledge |
| `agent_runner.py` | LLM receives the updated tool catalogue automatically |
| `webhook.py` | TOOL_TO_SECTION is built dynamically from manifest at startup — new section name appears in the response automatically |
| Frontend | Updates case summary panel if the new section is relevant to the investigator view |

---


## Section 8 — Design Principles

```
PRINCIPLE 1 — The LLM is the router, not the URL.
  The frontend does not choose which tool runs by choosing which
  endpoint to call. The frontend submits a case. The LLM reads
  the manifest and determines what to call based on data dependencies.

PRINCIPLE 2 — All tool calls are routed through the dispatcher.
  appworks_services.py is reached only via dispatcher.py.
  This ensures all three validation gates are applied to every call.

PRINCIPLE 3 — The case summary panel reads sections. It does not trigger agents.
  The frontend renders all investigation sections in a single unified
  case summary panel alongside the Copilot chat interface. Rendering
  a section does not initiate an agent run or tool call. The two-panel
  UI is the surface. The architecture is backstage.

PRINCIPLE 4 — ON-DEMAND endpoints receive context in the request body.
  POST /playbook and POST /copilot receive ai_investigation_data —
  the structured JSON sections from the /investigate response.
  POST /report receives both ai_investigation_data and ai_case_summary.
  POST /copilot also receives conversation_history.
  The server does not need to hold state between sessions. If CS-4
  is populated from the current session, it is used. If not, the
  request body field is the fallback. No AppWorks read is required
  by the agent service to recover context between sessions.

PRINCIPLE 5 — The Copilot answers from context first.
  POST /copilot injects stored case data into the system prompt.
  The LLM answers from that context. A tool call is made only
  when the question requires data genuinely not in the context.

PRINCIPLE 6 — manifest.yaml is an executable contract, not documentation.
  The dispatcher reads and enforces it at runtime. A tool not
  present in the manifest is blocked at Gate 1. A tool present
  in the manifest is automatically available to the LLM.

PRINCIPLE 7 — Conversation history is owned by the frontend.
  The frontend appends each Copilot exchange to conversation_history
  and sends the complete list on every request. The server holds
  no per-session conversation state.

PRINCIPLE 8 — Every tool response carries provenance.
  appworks_services.py wraps every return value in a standard envelope:
  { result: {...}, provenance: { sources, retrieved_at, computed_by } }.
  The dispatcher passes this envelope through unchanged. The agent runner
  gives the LLM only the result portion — provenance travels alongside
  separately and is never part of LLM reasoning. The webhook extracts
  the full provenance trail and includes it in every investigation
  response. For the risk assessment result specifically, computed_by
  must reflect that the score was produced by BSI's configured fraud
  detection rules evaluated against AppWorks data — the LLM receives
  this result and explains it in plain English. It does not compute,
  adjust, or override the score, tier, or triggered rules.

PRINCIPLE 9 — The agent service has no write path to AppWorks.
  appworks_services.py makes read-only AppWorks REST calls only.
  Investigation outputs are returned to the frontend as JSON. The
  analyst saves them to AppWorks case fields via native BSI UI
  actions — not via any agent endpoint. This preserves the READONLY
  constraint and means every write to the case record is a deliberate
  analyst action recorded in the AppWorks audit trail.

PRINCIPLE 10 — Unknown tool calls fail gracefully at Gate 1.
  If the LLM attempts to call a tool not registered in manifest.yaml,
  the dispatcher returns a structured error listing the available tools.
  The LLM receives this as a tool result message and informs the
  investigator honestly that the data is not available. No fabricated
  answer. No crash. No silent failure. Gate 1 is the safety boundary.

PRINCIPLE 11 — ai_summary staleness is a BSI workflow concern, not an agent concern.
  When a case is modified in AppWorks, the reload_ai_summary field
  is set by AppWorks workflow rules. The BSI UI reads this flag and
  shows a reload banner under AI Insights. The analyst re-runs and
  re-saves. The agent service never reads, sets, or acts on this flag.
  Staleness detection and enforcement is the BSI application's
  responsibility. The agent service always works from the context
  it is given.
```

---

## Section 9 — System Characteristics Summary

| This system | Description |
|---|---|
| Is an AI agentic system | The LLM reads tool descriptions and decides autonomously what to call |
| Is manifest-driven | The tool registry is the executable contract — not code, not comments |
| Is governed at the gateway | Every tool call is validated through three gates before reaching AppWorks |
| Is context-injected for the Copilot | Stored case data is injected into the LLM prompt — no re-investigation per question |
| Is extensible at low cost | Adding a tool requires two files regardless of current tool count |
| Is not a REST wrapper | Endpoints represent workflow phases, not individual service functions |
| Is not frontend-orchestrated | The frontend does not decide tool order or call sequence |
| Is provenance-aware | Every tool response is stamped with its AppWorks data sources, retrieval timestamp, and how the result was produced. The risk score carries provenance showing it came from BSI's configured rules — not from LLM reasoning. The Copilot cites provenance when answering investigator questions. |
| Is READONLY end to end | appworks_services.py makes read-only AppWorks REST calls only. No agent endpoint writes to AppWorks. Investigation outputs are saved to case fields by analyst action via the BSI UI. |
| Fails gracefully on unknown tools | If the LLM attempts a tool not in the manifest, Gate 1 returns a structured error. The LLM informs the investigator honestly. No fabrication, no crash. |
| Is stateless between sessions | CS-4 is a within-session cache. ON-DEMAND endpoints fall back to ai_investigation_data sent in the request body. The same pattern as the Copilot's conversation_history. No AppWorks read required by the agent service to recover between sessions. |

---

## Section 10 — Standard Tool Response Envelope

Every function in `appworks_services.py` returns the same two-key
structure. No function returns a plain dict. This is the contract
the dispatcher and agent runner depend on.

```python
{
    "result":     { ...canonical entity data... },
    "provenance": {
        "sources":      [ ...AppWorks record identifiers... ],
        "retrieved_at": "ISO 8601 UTC timestamp",
        "computed_by":  "human-readable description of how result was produced"
    }
}
```

**The `result` key** contains the canonical entity fields as defined
in the semantic model. This is what the LLM receives in the tool
result message and reasons over.

**The `provenance` key** travels alongside the result but is never
injected into the LLM's tool message content. The agent runner
accumulates provenance entries separately into `provenance_trail[]`.

### Fields

| Field | Type | What to put in it |
|---|---|---|
| `sources` | list of strings | The named AppWorks records that provided the data — e.g. `"AppWorks case record BSI-2024-00421"`, `"AppWorks subject record SUBJ-7821"` |
| `retrieved_at` | ISO 8601 string | `datetime.utcnow().isoformat()` at the moment the AppWorks call returns |
| `computed_by` | string | A plain description of how the result was produced. For direct data retrieval: `"AppWorks REST retrieval"`. For the risk assessment: something that accurately reflects BSI's configured fraud detection rules evaluating AppWorks data — the exact string is an implementation detail to confirm with the BSI team. |

### The risk assessment result — one firm rule

For `calculate_risk_metrics`, `computed_by` must never be empty,
never reference the LLM, and must clearly distinguish that the
risk score, risk tier, and triggered rules were produced by
BSI's configured fraud detection rules running against AppWorks
data — not generated or inferred by the AI.

The LLM receives the completed `result` block, reads the score and
triggered rules, and writes a plain-English explanation. At no point
does the LLM calculate, adjust, or override the score. If `computed_by`
on a risk assessment result does not reflect deterministic rule
evaluation, the dispatcher should return an error rather than passing
the result through.

### Example — data retrieval function

```python
def get_case_header(case_id: str) -> dict:
    _mock_http("GET", f"/appworks/rest/v1/cases/{case_id}/header")
    return {
        "result": {
            "case_id":               case_id,
            "subject_id":            "SUBJ-7821",
            "fraud_types":           ["BILLING", "NETWORK"],
            "procedure_codes":       ["99213"],
            "complaint_description": "ABC Clinic repeatedly billed...",
            "key_persons":           [{"name": "John Miller", "role": "Billing Manager"}],
            "linked_organisations":  [{"name": "XYZ Healthcare"}],
            "prior_case_ids":        ["BSI-2021-113", "BSI-2022-078"],
            "estimated_loss":        84000
        },
        "provenance": {
            "sources":      [f"AppWorks case record {case_id}"],
            "retrieved_at": datetime.utcnow().isoformat(),
            "computed_by":  "AppWorks REST retrieval"
        }
    }
```

### Example — risk assessment function

```python
def calculate_risk_metrics(case_id: str, subject_id: str,
                           fraud_types: list) -> dict:
    # Active rules already fetched from AppWorks via get_risk_rules()
    # Rules engine evaluates rules relevant to the submitted fraud types
    # Score computed deterministically — LLM is not involved
    return {
        "result": {
            "case_id":          case_id,
            "risk_score":       0.87,
            "risk_tier":        "HIGH",
            "triggered_rules":  ["R-101", "R-205", "R-312"]
        },
        "provenance": {
            "sources": [
                f"AppWorks case record {case_id}",
                f"AppWorks subject record {subject_id}",
                "AppWorks BSI fraud detection rules table"
            ],
            "retrieved_at": datetime.utcnow().isoformat(),
            "computed_by":  "BSI configured rules evaluation"
        }
    }
```

### Illustrative provenance trail — standard four-tool AUTO investigation

The following shows what `_extract_provenance_trail()` returns after a
complete `/investigate` run. Actual entries, tool names, and sources
are determined at runtime by which tools the LLM called.

```python
[
    { "tool":         "verify_case_intake",
      "sources":      ["AppWorks case record BSI-2024-00421"],
      "retrieved_at": "2025-04-27T14:32:00Z",
      "computed_by":  "AppWorks REST retrieval" },

    { "tool":         "fetch_subject_history",
      "sources":      ["AppWorks subject record SUBJ-7821",
                       "AppWorks billing summary 2023-2025"],
      "retrieved_at": "2025-04-27T14:32:02Z",
      "computed_by":  "AppWorks REST retrieval" },

    { "tool":         "search_similar_cases",
      "sources":      ["AppWorks closed case archive"],
      "retrieved_at": "2025-04-27T14:32:04Z",
      "computed_by":  "AppWorks REST retrieval" },

    { "tool":         "get_risk_rules",
      "sources":      ["AppWorks BSI fraud detection rules table"],
      "retrieved_at": "2025-04-27T14:32:05Z",
      "computed_by":  "AppWorks REST retrieval" },

    { "tool":         "calculate_risk_metrics",
      "sources":      ["AppWorks case record BSI-2024-00421",
                       "AppWorks subject record SUBJ-7821",
                       "AppWorks BSI fraud detection rules table"],
      "retrieved_at": "2025-04-27T14:32:06Z",
      "computed_by":  "BSI configured rules evaluation" }
]
```

---

## Section 11 — ai_case_summary: Session Persistence Pattern

The agent service is stateless between server sessions. CS-4 is an
in-memory cache that does not survive a server restart. The pattern
that bridges sessions without giving the agent service a write path
to AppWorks is as follows.

### The AppWorks case fields

| Field | Written by | Contains | Used by |
|---|---|---|---|
| `ai_case_summary` | Analyst (BSI UI save action) | `agent_summary` — natural language narrative | Displayed on case screen when case reopened |
| `ai_investigation_data` | Analyst (BSI UI save action) | `investigation{}` — structured JSON sections | Sent in request body to /playbook, /report, /copilot |
| `ai_playbook` | Reviewer (BSI UI save action) | Tailored playbook JSON from /playbook response | Sent in request body to /report |
| `ai_report_summary` | Director (BSI UI save action) | Final report narrative from /report response | Displayed to director |
| `reload_ai_summary` | AppWorks workflow rules | Boolean flag — set to true when case is modified | Read by BSI UI only |

Both `ai_case_summary` and `ai_investigation_data` come from the same
single HTTP response returned by /investigate. The frontend saves both
in one analyst action. They serve different purposes:
`ai_case_summary` is for humans. `ai_investigation_data` is for the system.

### The write path — analyst action only

```
/investigate completes → HTTP 200 → frontend receives:
  { agent_summary: "...",  ← natural language
    investigation: {...},  ← structured JSON
    provenance_trail: [...] }
│
└── Frontend renders agent_summary on case summary panel
      Analyst reads and reviews

      If satisfied: clicks "Save AI Summary" in BSI UI
        → AppWorks native save action (one action, two fields)
        → ai_case_summary      = response.agent_summary
        → ai_investigation_data = response.investigation
        → AppWorks audit trail records: Analyst X saved at timestamp Y
        → Agent service is not involved
```

No agent endpoint writes to AppWorks. Every field write is a named
analyst action in the AppWorks audit trail.

### The read path — frontend owns context between sessions

```
Analyst opens case next day — new server session, CS-4 empty
│
└── BSI UI reads both AppWorks fields:
      ai_case_summary      → renders on case summary panel (human readable)
      ai_investigation_data → held in memory, sent in request bodies

      When analyst clicks "Load Playbook":
        POST /playbook {
          case_id: "BSI-2024-00421",
          ai_investigation_data: <field value>
        }
        webhook.py reads request.ai_investigation_data → populates CS-4
        Agent loop proceeds normally

      When investigator asks Copilot question:
        POST /copilot {
          case_id: "BSI-2024-00421",
          question: "...",
          ai_investigation_data: <field value>,
          conversation_history: [...]
        }
```

The frontend is responsible for reading both AppWorks fields and sending
the right one to each endpoint. The server receives what it needs in the
request body. It never reads AppWorks to recover context.

### The staleness flag — BSI workflow concern only

```
Case modified in AppWorks (new allegation added)
│
└── AppWorks workflow rule fires
      Sets reload_ai_summary = true on case record
      BSI UI detects flag when case is opened
      Shows banner: "Case has been updated since AI summary was saved.
                     Please reload AI Insights."
      Downstream buttons (Load Playbook, Generate Report) disabled
      until analyst re-runs and re-saves

      Agent service does not read this flag.
      Agent service does not set this flag.
      This is entirely a BSI application concern.
```

---

*This document covers all architectural decisions, execution sequences,
context storage points, and design principles for the BSI Fraud
Investigation Platform POC. It is intended as the shared reference
for the implementation team.*
