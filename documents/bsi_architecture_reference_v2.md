# BSI Fraud Investigation Platform
## Architecture Reference Document
### Version 1.0 — POC

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
├─────────────────────────────────────────────────────────┤
│  LAYER 2 — Agent + Semantic   agent_runner.py            │
│            Layer              dispatcher.py              │
│                               tool_builder.py            │
│                               manifest.yaml              │
│  The LLM decides what to call. The dispatcher validates. │
│  No direct function calls. No hardcoded sequences.       │
├─────────────────────────────────────────────────────────┤
│  LAYER 3 — Service Layer      appworks_services.py       │
│  Python functions that call AppWorks REST APIs.          │
│  Reached only via the dispatcher in Layer 2.             │
│  Not directly accessible from Layer 1.                   │
└─────────────────────────────────────────────────────────┘
```

A common alternative pattern — and one worth understanding so it
can be distinguished from this architecture — is to map each tool
directly to an HTTP endpoint:

```
POST /api/verify_case_intake     →  get_case_header()
POST /api/calculate_risk_metrics →  get_risk_measures()
```

While this pattern is familiar and straightforward to implement,
it produces a REST wrapper around service functions rather than
an AI agentic system. The LLM, the dispatcher, and the manifest
are absent from the execution path. The differences this creates
in extensibility, validation, and agent behaviour are covered in
Section 8.

---

## Section 2 — Context Storage: What It Is and Where It Lives

The word "context" refers to different things at different points
in the execution. There are six distinct context types in this
system. Understanding all six is required to implement the
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
│  the structured JSON response. Each section maps to a UI tab.    │
├──────────────────────────────────────────────────────────────────┤
│  CS-4  CASE SESSION CONTEXT                                      │
│  Location: CASE_STORE dict in webhook.py (in-memory for POC,     │
│  Redis or DB in production)                                      │
│  What it is: After /investigate completes, the extracted         │
│  sections are stored here keyed by case_id. This is what         │
│  /playbook, /report, and /copilot read — so the full             │
│  investigation does not need to be re-run for each subsequent    │
│  ON-DEMAND request. CS-4 is required for ON-DEMAND flows         │
│  and the Copilot to function correctly.                          │
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
│  What it is: The frontend maintains and sends the full prior      │
│  conversation on every Copilot request. The server is stateless  │
│  per request. Conversation continuity is the frontend's          │
│  responsibility.                                                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## Section 3 — Complete Execution Sequences

### 3.1 AUTO Flow — POST /investigate
**Trigger:** AppWorks complaint form submitted. Runs tools 1–4 automatically.

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
│       search_similar_cases, calculate_risk_metrics,
│       get_investigation_playbook, generate_final_report ]
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
│     returns { status:"ok", data: { case_id, subject_primary_id,
│               fraud_type_classified, complaint_description, ... } }
│
├── appworks_services.py  ←  get_case_header("BSI-2024-00421")
│     calls AppWorks REST: GET /appworks/rest/v1/cases/BSI-2024-00421/header
│     returns case header dict
│
├── agent_runner.py
│     appends tool result to messages:                   ← CS-2 STORED
│       { role:"tool",
│         tool_call_id: "tc_abc123",
│         content: JSON.stringify(dispatch_result.data) }
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
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 3                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     reads subject history
│     DECIDES: "I need similar cases →
│               search_similar_cases"
│     returns: tool_call: { name:"search_similar_cases",
│                           params:{ complaint_text:"...", top_n:3 } }
│
├── dispatcher.py → appworks_services.py → agent_runner.py
│     appends tool result to messages                    ← CS-2 STORED
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 4                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     DECIDES: "I need risk metrics →
│               calculate_risk_metrics"
│     returns: tool_call: { name:"calculate_risk_metrics",
│                           params:{ case_id:"BSI-2024-00421",
│                                    subject_id:"SUBJ-7821" } }
│
├── dispatcher.py → appworks_services.py → agent_runner.py
│     appends tool result to messages                    ← CS-2 STORED
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 5 — LLM produces final summary                ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     has: case data, subject history, similar cases, risk score
│     DECIDES: "AUTO phase complete. No further tools required."
│     returns: finish_reason="stop"
│              content: "Investigation complete. Risk score 0.87 HIGH.
│                        Three rules triggered. Subject has 2 prior cases..."
│
├── agent_runner.py
│     sees finish_reason="stop"
│     exits loop
│     returns full messages list to webhook.py
│
├── webhook.py  ←  back from runner.investigate()
│     calls _extract_tool_results(messages)             ← CS-3 BUILT
│       scans messages for role:"tool" entries
│       maps each to section name via TOOL_TO_SECTION
│       returns {
│         complaint_intelligence: { ...case header data... },
│         context_enrichment:     { ...subject history... },
│         similar_cases:          { ...vector matches... },
│         risk_assessment:        { ...risk score, rules... }
│       }
│
│     calls _extract_agent_summary(messages)
│       returns LLM's final text summary
│
│     CASE_STORE["BSI-2024-00421"] = sections            ← CS-4 STORED
│       Persisted in memory (Redis/DB in production).
│       Read by /playbook, /report, /copilot so those
│       endpoints do not re-run the full investigation.
│
│     returns HTTP 200:
│       {
│         case_id:       "BSI-2024-00421",
│         status:        "completed",
│         agent_summary: "Investigation complete. Risk score 0.87...",
│         investigation: {
│           complaint_intelligence: { ... },   ← Tab 1 reads this
│           context_enrichment:     { ... },   ← Tab 2 reads this
│           similar_cases:          { ... },   ← Tab 3 reads this
│           risk_assessment:        { ... }    ← Tab 4 reads this
│         },
│         meta: { tool_calls_made:4, duration_seconds:8.3, ... }
│       }
│
└── FRONTEND
      Tab 1 (Complaint Details)  reads response.investigation.complaint_intelligence
      Tab 2 (Subject History)    reads response.investigation.context_enrichment
      Tab 3 (Similar Cases)      reads response.investigation.similar_cases
      Tab 4 (Risk Assessment)    reads response.investigation.risk_assessment

      Tab rendering is driven by reading named sections from the response.
      No tab triggers a separate agent call or tool endpoint.
```

---

### 3.2 ON-DEMAND Flow — POST /playbook
**Trigger:** Reviewer clicks "Load Investigation Playbook" button in BSI UI.

```
Reviewer clicks button → POST /playbook  { "case_id": "BSI-2024-00421" }
│
├── webhook.py
│     reads CASE_STORE["BSI-2024-00421"]                ← CS-4 READ
│     if not found: return 404 — /investigate required first
│     extracts from stored context:
│       fraud_type = stored.complaint_intelligence.fraud_type_classified
│       risk_tier  = stored.risk_assessment.risk_tier
│
│     creates BSIAgentRunner(manifest_path)
│     calls runner with scoped prompt containing fraud_type + risk_tier
│
├── agent_runner.py
│     builds scoped messages:
│       messages = [
│         { role:"system", content:
│             "Your task is to retrieve the investigation playbook.
│              fraud_type = BILLING, risk_tier = HIGH.
│              Call get_investigation_playbook with these values." },
│         { role:"user", content:
│             "Load investigation playbook for case BSI-2024-00421" }
│       ]
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 1                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
│     DECIDES: "get_investigation_playbook(BILLING, HIGH)"
│     returns: tool_call: { name:"get_investigation_playbook",
│                           params:{ fraud_type:"BILLING",
│                                    risk_level:"HIGH" } }
│
├── dispatcher.py  ←  all three gates pass
├── appworks_services.py  ←  get_playbook_by_type("BILLING", "HIGH")
│
├── agent_runner.py
│     appends tool result to messages                    ← CS-2 STORED
│
│     ╔══════════════════════════════════════════════════════╗
│     ║  TURN 2                                              ║
│     ╚══════════════════════════════════════════════════════╝
│
├── OpenAI GPT-4o
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
│           investigation_playbook: { ... }   ← Tab 5 reads this
│         }
│       }
│
└── FRONTEND
      Tab 5 (Strategy) reads response.investigation.investigation_playbook
```

---

### 3.3 ON-DEMAND Flow — POST /report
**Trigger:** Director clicks "Generate Investigation Summary" button.

```
Director clicks button → POST /report  { "case_id": "BSI-2024-00421" }
│
├── webhook.py
│     reads CASE_STORE["BSI-2024-00421"]                ← CS-4 READ
│     confirms prior phases completed
│       complaint_intelligence and risk_assessment present
│       if missing: return 400 with clear message
│
│     creates BSIAgentRunner(manifest_path)
│     calls runner with scoped prompt for report phase
│
├── agent_runner.py → OpenAI GPT-4o
│     DECIDES: "generate_final_report(BSI-2024-00421)"
│
├── dispatcher.py → appworks_services.py
│     ← compile_and_render_report("BSI-2024-00421")
│
├── agent_runner.py
│     appends result to messages                        ← CS-2 STORED
│
├── OpenAI GPT-4o
│     finish_reason="stop"
│
├── webhook.py
│     extracts final_report section                     ← CS-3 BUILT
│     updates CASE_STORE["BSI-2024-00421"]
│       with final_report section                       ← CS-4 UPDATED
│
│     returns HTTP 200:
│       {
│         investigation: {
│           final_report: { ... }   ← Tab 6 reads this
│         }
│       }
│
└── FRONTEND
      Tab 6 (Report) reads response.investigation.final_report
```

---

### 3.4 ON-DEMAND Flow — POST /copilot
**Trigger:** Investigator sends a message in the chat panel. Every message.

```
Investigator types: "Why is the risk score so high?"

POST /copilot  {
  "case_id":             "BSI-2024-00421",
  "question":            "Why is the risk score so high?",
  "conversation_history": []   ← empty on first message,
                                 grows with every exchange
}
│
├── webhook.py
│     reads CASE_STORE["BSI-2024-00421"]                ← CS-4 READ
│     if not found: return 404
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
│     DECIDES: "Answer available in context. No tool call needed."
│     returns: finish_reason="stop"
│              content: "The risk score of 0.87 is HIGH because three
│                        rules were triggered:
│                        R-101: Subject has 2 prior cases within 3 years.
│                        R-205: Prior substantiated billing fraud case.
│                        R-312: Claim volume spike — 47 claims in 6 months.
│                        Source: risk_assessment context."
│
├── agent_runner.py → returns messages to webhook.py
│
├── webhook.py
│     returns HTTP 200:
│       {
│         answer:          "The risk score of 0.87 is HIGH because...",
│         sources_cited:   ["risk_assessment context"],
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
│                           params:{ complaint_text:"...", top_n:3 } }
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
```

---

## Section 4 — Context Storage Reference Table

| ID | Name | Location | Created by | Read by | Lifespan |
|---|---|---|---|---|---|
| CS-1 | LLM Turn Context | `messages[]` in agent_runner.py | agent_runner.py init | GPT-4o on every turn | Duration of agent loop |
| CS-2 | Tool Result Context | `role:"tool"` entries in CS-1 | agent_runner.py after each dispatch | GPT-4o on next turn | Duration of agent loop |
| CS-3 | Response Context | `sections{}` dict in webhook.py | `_extract_tool_results()` | HTTP response builder | Duration of request |
| CS-4 | Case Session Context | `CASE_STORE[case_id]` in webhook.py | `/investigate` endpoint | `/playbook`, `/report`, `/copilot` | Server session (POC) |
| CS-5 | Copilot Injected Context | System prompt string | webhook.py `/copilot` handler | GPT-4o system turn | Duration of request |
| CS-6 | Copilot Conversation History | `conversation_history[]` in request body | Frontend (appends each exchange) | agent_runner.py messages build | Frontend session |

---

## Section 5 — Endpoint Reference

| Endpoint | Trigger | Tools called | Reads from | Writes to | Phase |
|---|---|---|---|---|---|
| `GET /health` | Browser / monitoring | None | Nothing | Nothing | — |
| `POST /investigate` | AppWorks form submission | 1–4 (LLM decides) | manifest.yaml | CS-4 CASE_STORE | AUTO |
| `POST /playbook` | Reviewer clicks button | Tool 5 only | CS-4 for fraud_type + risk_tier | CS-4 updated | ON-DEMAND |
| `POST /report` | Director clicks button | Tool 6 only | CS-4 for case context | CS-4 updated | ON-DEMAND |
| `POST /copilot` | Investigator sends message | 0 or 1 (LLM decides) | CS-4 for injected context | CS-4 if tool called | ON-DEMAND |

---

## Section 6 — File Responsibilities

Each file has a single, bounded responsibility. When a change is needed,
this table identifies which file owns that concern.

| File | Responsible for | Outside its scope |
|---|---|---|
| `api/webhook.py` | HTTP endpoints, CASE_STORE, response shaping | Calling appworks_services directly. Knowing tool names beyond TOOL_TO_SECTION. |
| `agent_service/agent_runner.py` | LLM loop, message history, turn management | HTTP concerns, tab names, UI section structure. |
| `agent_service/tool_builder.py` | Converting manifest catalogue to OpenAI tool schema | Knowledge of specific tools. It is intentionally generic. |
| `semantic_layer/dispatcher.py` | Three validation gates, tool routing | Being bypassed or called around for any reason. |
| `semantic_layer/appworks_services.py` | AppWorks REST API calls | Being called from any layer other than dispatcher.py. |
| `manifest.yaml` | Tool contracts — names, descriptions, params, function mappings | Ordering instructions or hardcoded sequences. |

---

## Section 7 — Adding a New Tool

When a new tool is required — for example `check_provider_license` —
two files are involved.

**File 1: manifest.yaml — add one entry**
```yaml
- name: "check_provider_license"
  description: >
    Checks whether a provider licence is active or suspended in AppWorks.
    Requires: provider_id (from verify_case_intake).
    Returns: licence_status, suspension_dates, issuing_authority.
  python_function: "appworks_services.get_provider_license"
  required_params:
    - name: "provider_id"
      type: "string"
      description: "Provider identifier from verify_case_intake output"
```

**File 2: appworks_services.py — add one function**
```python
def get_provider_license(provider_id: str) -> dict:
    _mock_http("GET", f"/appworks/rest/v1/providers/{provider_id}/license")
    return { "licence_status": "SUSPENDED", "suspension_date": "2024-01-15" }
```

**All other files — no changes required:**

| File | Reason no change is needed |
|---|---|
| `dispatcher.py` | Reads manifest dynamically at startup — new tool is automatically registered |
| `tool_builder.py` | Converts whatever the dispatcher holds — no tool-specific knowledge |
| `agent_runner.py` | LLM receives the updated tool catalogue automatically |
| `webhook.py` | New tool result appears under its tool name in the response |
| Frontend | Reads the new section by name if a new tab is needed |

---

## Section 8 — Architectural Comparison: Tool Registry vs Direct Endpoints

It is useful to compare the two approaches explicitly so the design
decisions in this architecture are clearly understood.

**Adding the same `check_provider_license` tool using direct endpoints
would require the following changes across the stack:**

1. New function in `appworks_services.py`
2. New HTTP endpoint: `POST /api/check_provider_license`
3. New frontend API call targeting that endpoint
4. A decision made in the frontend about where in the call sequence it belongs
5. Frontend updated to call it in the correct order relative to other calls
6. Frontend updated to wire the result to the appropriate panel
7. Error handling written for the new endpoint
8. `manifest.yaml` updated as documentation — it has no effect at runtime
   in a direct-endpoint architecture

As the tool count grows from 6 to 10 to 20, this change cost scales
with each addition. In the tool registry architecture the cost remains
constant — manifest entry plus one service function — regardless of
how many tools already exist.

The deeper distinction is where intelligence lives. In a direct-endpoint
approach the frontend decides which service to call, in what order, and
when. In the tool registry approach that responsibility is delegated to
the LLM, which reads the manifest and determines call sequence from
data dependencies. This is what makes the system agentic rather than
orchestrated.

The Investigation Copilot also cannot exist in a direct-endpoint
architecture. Answering natural language questions, maintaining
conversational context, and making selective tool calls only when
the question requires new data are capabilities that depend on the
LLM reasoning over injected context. A direct-endpoint approach
has no mechanism for this.

---

## Section 9 — Design Principles

```
PRINCIPLE 1 — The LLM is the router, not the URL.
  The frontend does not choose which tool runs by choosing which
  endpoint to call. The frontend submits a case. The LLM reads
  the manifest and determines what to call based on data dependencies.

PRINCIPLE 2 — All tool calls are routed through the dispatcher.
  appworks_services.py is reached only via dispatcher.py.
  This ensures all three validation gates are applied to every call.

PRINCIPLE 3 — Tabs read sections. Tabs do not trigger agents.
  A tab renders content from the investigation response or from
  stored session context. Tab interaction does not initiate agent
  runs or tool calls.

PRINCIPLE 4 — ON-DEMAND endpoints receive a case_id, not tool parameters.
  POST /playbook receives { "case_id": "BSI-2024-00421" }.
  fraud_type and risk_tier are read from CASE_STORE[case_id].
  Tool parameters are never passed directly from the frontend.

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
```

---

## Section 10 — System Characteristics Summary

| This system | Description |
|---|---|
| Is an AI agentic system | The LLM reads tool descriptions and decides autonomously what to call |
| Is manifest-driven | The tool registry is the executable contract — not code, not comments |
| Is governed at the gateway | Every tool call is validated through three gates before reaching AppWorks |
| Is context-injected for the Copilot | Stored case data is injected into the LLM prompt — no re-investigation per question |
| Is extensible at low cost | Adding a tool requires two files regardless of current tool count |
| Is not a REST wrapper | Endpoints represent workflow phases, not individual service functions |
| Is not frontend-orchestrated | The frontend does not decide tool order or call sequence |

---

*This document covers all architectural decisions, execution sequences,
context storage points, and design principles for the BSI Fraud
Investigation Platform POC. It is intended as the shared reference
for the implementation team.*
