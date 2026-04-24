# semantic_layer/appworks_services.py
# ----------------------------------------------------------------
# MOCK – Simulates AppWorks Web Service HTTP calls.
#
# ANTI-CORRUPTION LAYER:
#   Every function validates its return data against the canonical
#   entity model (semantic_model.py) before returning it.
#   This is the boundary between AppWorks (external system) and
#   your domain. Raw AppWorks data never passes this boundary
#   unvalidated.
#
# HOW VALIDATION WORKS HERE:
#   1. Raw dict is assembled (mock data, or real AppWorks response)
#   2. Pydantic model is instantiated — ValidationError raised here
#      if the data does not match the canonical schema
#   3. .model_dump() converts back to dict for the dispatcher
#      so the rest of the system works with clean, validated dicts
#
# IF APPWORKS CHANGES A FIELD:
#   The ValidationError surfaces here — at the boundary — not
#   silently downstream in the LLM context or CS-4 store.
#
# TO PRODUCTIONISE:
#   Replace the _mock_http body of each function with a real
#   requests.get/post to the AppWorks REST endpoint shown.
#   The validation layer beneath it stays identical.
# ----------------------------------------------------------------

import time
from pydantic import ValidationError

from semantic_model import (
    CaseHeader,
    SubjectProfile,
    SimilarCasesResult,
    RiskAssessment,
    InvestigationPlaybook,
    FinalReport,
    AddressEntry,
    PriorCase,
    KnownAssociate,
    TriggeredRule,
    InvestigationStep,
    EvidenceItem,
    SimilarCaseMatch,
    ReportSections,
)


def _mock_http(method: str, endpoint: str, payload: dict = None):
    """Simulates network latency and prints the would-be HTTP call."""
    print(f"      [AppWorks HTTP] {method} {endpoint}")
    if payload:
        print(f"      [AppWorks HTTP] Payload: {payload}")
    time.sleep(0.3)


def _validate(model_class, raw_data: dict, fn_name: str) -> dict:
    """
    Validates raw_data against the canonical Pydantic model.
    Returns a clean validated dict on success.
    Raises ValidationError on failure with a clear message
    identifying which function and which fields failed.

    This function is the Anti-Corruption Layer enforcement point.
    It is called at the end of every service function.
    """
    try:
        validated = model_class(**raw_data)
        return validated.model_dump()
    except ValidationError as e:
        # Re-raise with context so the dispatcher can surface
        # a clear error to the agent runner
        raise ValidationError(
            f"[{fn_name}] AppWorks response failed canonical validation.\n"
            f"This means AppWorks returned unexpected data.\n"
            f"Check the field errors below and update semantic_model.py "
            f"if the AppWorks API has changed.\n\n{e}"
        )


# ----------------------------------------------------------------
# AGENT: Complaint Intelligence Agent
# Tool:  verify_case_intake
# Model: CaseHeader
# ----------------------------------------------------------------

def get_case_header(case_id: str) -> dict:
    """
    PRODUCTION CALL:
      GET /appworks/rest/v1/cases/{case_id}/header
      Headers: Authorization: Bearer {session_token}

    RETURNS: Validated CaseHeader dict
    """
    print(f"\n      → Calling AppWorks: fetch case header for [{case_id}]")
    _mock_http("GET", f"/appworks/rest/v1/cases/{case_id}/header")

    # Raw AppWorks response (mock)
    # In production: raw = requests.get(...).json()
    raw_responses = {
        "BSI-2024-00421": {
            "case_id":                "BSI-2024-00421",
            "complainant_name":       "MassHealth Audit Division",
            "subject_primary":        "Dr. Amir Hosseini",
            "subject_primary_id":     "SUBJ-7821",
            "subject_secondary":      "Sunrise Home Health LLC",
            "complaint_description":  (
                "Subject billed for home health aide services on 47 dates "
                "where the patient was confirmed hospitalized. Claims totaling "
                "$84,200 submitted over 6 months with no supporting documentation."
            ),
            "fraud_type_classified":  "BILLING",
            "intake_date":            "2024-03-15",
            "status":                 "OPEN"
        }
    }

    raw = raw_responses.get(case_id, {"error": f"Case {case_id} not found"})

    if "error" in raw:
        raise ValueError(raw["error"])

    # ── Anti-Corruption Layer: validate before returning ──────────
    validated = _validate(CaseHeader, raw, "get_case_header")

    print(f"      ← AppWorks returned: case [{case_id}], "
          f"fraud type [{validated['fraud_type_classified']}] ✓ validated")
    return validated


# ----------------------------------------------------------------
# AGENT: Context Enrichment Agent
# Tool:  fetch_subject_history
# Model: SubjectProfile
# ----------------------------------------------------------------

def get_enriched_subject_profile(subject_id: str) -> dict:
    """
    PRODUCTION CALLS:
      GET /appworks/rest/v1/subjects/{subject_id}
      GET /appworks/rest/v1/subjects/{subject_id}/cases?years=5
      GET /appworks/rest/v1/subjects/{subject_id}/addresses
      GET /appworks/rest/v1/subjects/{subject_id}/associates

    RETURNS: Validated SubjectProfile dict
    """
    print(f"\n      → Calling AppWorks: fetch enriched profile for [{subject_id}]")
    _mock_http("GET", f"/appworks/rest/v1/subjects/{subject_id}")
    _mock_http("GET", f"/appworks/rest/v1/subjects/{subject_id}/cases?years=5")
    _mock_http("GET", f"/appworks/rest/v1/subjects/{subject_id}/addresses")
    _mock_http("GET", f"/appworks/rest/v1/subjects/{subject_id}/associates")

    raw_responses = {
        "SUBJ-7821": {
            "subject_id":       "SUBJ-7821",
            "full_name":        "Dr. Amir Hosseini",
            "dob":              "1968-04-22",
            "address_history": [
                {"address": "14 Beacon St, Boston MA", "from": "2021-01", "to": "present"},
                {"address": "88 Elm Ave, Quincy MA",   "from": "2018-06", "to": "2020-12"}
            ],
            "prior_cases": [
                {"case_id": "BSI-2021-00188", "year": 2021,
                 "fraud_type": "BILLING", "outcome": "SUBSTANTIATED"},
                {"case_id": "BSI-2019-00044", "year": 2019,
                 "fraud_type": "BILLING", "outcome": "UNSUBSTANTIATED"}
            ],
            "known_associates": [
                {"name": "Sunrise Home Health LLC",
                 "relationship": "Employer",     "subject_id": "SUBJ-9034"},
                {"name": "Maria Hosseini",
                 "relationship": "Co-signatory", "subject_id": "SUBJ-9201"}
            ],
            "prior_case_count": 2
        }
    }

    raw = raw_responses.get(subject_id)
    if not raw:
        raise ValueError(f"Subject {subject_id} not found in AppWorks")

    # ── Anti-Corruption Layer ─────────────────────────────────────
    # Note: AddressEntry uses field aliases (from/to are Python keywords).
    # Pydantic handles this via populate_by_name=True in AddressEntry.
    validated = _validate(SubjectProfile, raw, "get_enriched_subject_profile")

    print(f"      ← AppWorks returned: [{validated['prior_case_count']} prior cases], "
          f"[{len(validated['known_associates'])} associates] ✓ validated")
    return validated


# ----------------------------------------------------------------
# AGENT: Similar Case Retrieval Agent
# Tool:  search_similar_cases
# Model: SimilarCasesResult
# ----------------------------------------------------------------

def vector_search_cases(complaint_text: str, top_n: int = 3) -> dict:
    """
    PRODUCTION CALL:
      POST /vector-service/search
      Body: { "text": complaint_text, "top_n": top_n,
              "collection": "bsi_cases" }

    RETURNS: Validated SimilarCasesResult dict
    """
    print(f"\n      → Calling Vector DB: semantic search on complaint narrative")
    print(f"      [Vector DB] POST /vector-service/search  top_n={top_n}")
    print(f"      [Vector DB] Embedding: \"{complaint_text[:60]}...\"")
    time.sleep(0.4)

    raw = {
        "query_summary": "Home health billing fraud – hospitalized patient overlap",
        "matches": [
            {
                "case_id":          "BSI-2022-00317",
                "similarity_score": 0.94,
                "fraud_type":       "BILLING",
                "outcome":          "SUBSTANTIATED",
                "summary":          "Provider billed 38 days of home services "
                                    "while patient was in ICU. $61,400 recovered."
            },
            {
                "case_id":          "BSI-2021-00188",
                "similarity_score": 0.89,
                "fraud_type":       "BILLING",
                "outcome":          "SUBSTANTIATED",
                "summary":          "Same subject. Duplicate billing across two "
                                    "provider entities. $29,000 overpayment."
            },
            {
                "case_id":          "BSI-2020-00502",
                "similarity_score": 0.81,
                "fraud_type":       "BILLING",
                "outcome":          "REFERRED_TO_AG",
                "summary":          "Home health agency billed for deceased patient "
                                    "for 3 months post-death."
            }
        ][:top_n],
        "top_n_returned": min(top_n, 3)
    }

    # ── Anti-Corruption Layer ─────────────────────────────────────
    validated = _validate(SimilarCasesResult, raw, "vector_search_cases")

    print(f"      ← Vector DB returned: [{validated['top_n_returned']} matches], "
          f"top score [{validated['matches'][0]['similarity_score']}] ✓ validated")
    return validated


# ----------------------------------------------------------------
# AGENT: Fraud Risk Assessment Agent
# Tool:  calculate_risk_metrics
# Model: RiskAssessment
# ----------------------------------------------------------------

def get_risk_measures(case_id: str, subject_id: str) -> dict:
    """
    PRODUCTION CALLS:
      GET /appworks/rest/v1/cases/{case_id}/billing-summary
      GET /appworks/rest/v1/subjects/{subject_id}/risk-profile
      POST /rules-engine/evaluate  Body: { case_id, subject_id }

    RETURNS: Validated RiskAssessment dict
    """
    print(f"\n      → Calling AppWorks: fetch billing summary + risk profile")
    _mock_http("GET",  f"/appworks/rest/v1/cases/{case_id}/billing-summary")
    _mock_http("GET",  f"/appworks/rest/v1/subjects/{subject_id}/risk-profile")
    print(f"      [Rules Engine] POST /rules-engine/evaluate")
    time.sleep(0.3)

    raw = {
        "case_id":    case_id,
        "subject_id": subject_id,
        "risk_score": 0.87,
        "risk_tier":  "HIGH",
        "triggered_rules": [
            {"rule_id": "R-101",
             "rule_name": "Billing During Active Hospitalization",   "weight": 0.40},
            {"rule_id": "R-205",
             "rule_name": "Prior Substantiated Case Within 3 Years", "weight": 0.25},
            {"rule_id": "R-312",
             "rule_name": "Claim Volume Spike (>40 claims / 6 mo)",  "weight": 0.22}
        ],
        "billing_anomaly_flag": True,
        "prior_case_count":     2,
        "recommendation":       "Escalate to Senior Investigator. "
                                "Request full billing records subpoena."
    }

    # ── Anti-Corruption Layer ─────────────────────────────────────
    validated = _validate(RiskAssessment, raw, "get_risk_measures")

    print(f"      ← Rules Engine returned: score [{validated['risk_score']}], "
          f"tier [{validated['risk_tier']}], "
          f"[{len(validated['triggered_rules'])} rules triggered] ✓ validated")
    return validated


# ----------------------------------------------------------------
# AGENT: Case Strategy Agent
# Tool:  get_investigation_playbook
# Model: InvestigationPlaybook
# ----------------------------------------------------------------

def get_playbook_by_type(fraud_type: str, risk_level: str) -> dict:
    """
    PRODUCTION CALL:
      GET /appworks/rest/v1/playbooks
          ?fraud_type={fraud_type}&risk_level={risk_level}

    RETURNS: Validated InvestigationPlaybook dict
    """
    print(f"\n      → Calling AppWorks: fetch playbook "
          f"[{fraud_type}] / [{risk_level}]")
    _mock_http("GET",
               f"/appworks/rest/v1/playbooks"
               f"?fraud_type={fraud_type}&risk_level={risk_level}")

    playbooks = {
        ("BILLING", "HIGH"): {
            "playbook_id": "PB-BILLING-HIGH-v3",
            "fraud_type":  "BILLING",
            "risk_level":  "HIGH",
            "investigation_steps": [
                {"step": 1,
                 "action": "Pull complete billing history from MassHealth",
                 "owner": "Analyst",      "deadline_days": 3},
                {"step": 2,
                 "action": "Cross-reference claim dates with hospital records",
                 "owner": "Analyst",      "deadline_days": 5},
                {"step": 3,
                 "action": "Issue subpoena for provider service logs",
                 "owner": "Investigator", "deadline_days": 10},
                {"step": 4,
                 "action": "Interview patient and / or family members",
                 "owner": "Investigator", "deadline_days": 14},
                {"step": 5,
                 "action": "Prepare referral package for Attorney General",
                 "owner": "Director",     "deadline_days": 21}
            ],
            "evidence_checklist": [
                {"item": "MassHealth claims printout",          "mandatory": True},
                {"item": "Hospital admission/discharge records", "mandatory": True},
                {"item": "Provider service documentation",       "mandatory": True},
                {"item": "Patient statement",                    "mandatory": False},
                {"item": "Prior case file BSI-2021-00188",       "mandatory": False}
            ],
            "escalation_required": True
        }
    }

    key = (fraud_type.upper(), risk_level.upper())
    raw = playbooks.get(key, {
        "playbook_id":         "PB-DEFAULT",
        "fraud_type":          fraud_type,
        "risk_level":          risk_level,
        "investigation_steps": [
            {"step": 1, "action": "Manual review required",
             "owner": "Analyst", "deadline_days": 5}
        ],
        "evidence_checklist":  [],
        "escalation_required": False
    })

    # ── Anti-Corruption Layer ─────────────────────────────────────
    validated = _validate(InvestigationPlaybook, raw, "get_playbook_by_type")

    print(f"      ← AppWorks returned: playbook [{validated['playbook_id']}], "
          f"[{len(validated['investigation_steps'])} steps], "
          f"escalation=[{validated['escalation_required']}] ✓ validated")
    return validated


# ----------------------------------------------------------------
# AGENT: Report Generation Agent
# Tool:  generate_final_report
# Model: FinalReport
# ----------------------------------------------------------------

def compile_and_render_report(case_id: str) -> dict:
    """
    PRODUCTION CALLS:
      GET /appworks/rest/v1/cases/{case_id}/full-summary
      POST /appworks/rest/v1/reports/render
      Body: { case_id, template: "BSI_INVESTIGATION_REPORT_v2" }

    RETURNS: Validated FinalReport dict
    """
    print(f"\n      → Calling AppWorks: compile and render report for [{case_id}]")
    _mock_http("GET",  f"/appworks/rest/v1/cases/{case_id}/full-summary")
    _mock_http("POST", "/appworks/rest/v1/reports/render",
               {"case_id": case_id, "template": "BSI_INVESTIGATION_REPORT_v2"})

    raw = {
        "report_id":    f"RPT-{case_id}",
        "case_id":      case_id,
        "generated_at": "2024-03-15T14:32:00Z",
        "sections": {
            "case_summary":
                "Billing fraud complaint against Dr. Amir Hosseini / "
                "Sunrise Home Health LLC. Claims of $84,200 submitted for "
                "dates patient was confirmed hospitalized.",
            "subject_history":
                "Subject has 2 prior BSI cases. BSI-2021-00188 substantiated "
                "for billing fraud.",
            "similar_cases":
                "3 similar historical cases found. All substantiated. Pattern "
                "consistent with hospitalization-overlap billing scheme.",
            "risk_assessment":
                "Risk Score: 0.87 (HIGH). Three rules triggered: "
                "R-101, R-205, R-312.",
            "recommended_actions":
                "Escalate to Senior Investigator. Subpoena billing records. "
                "Cross-reference MassHealth claims with hospital records.",
            "analyst_notes":
                "[Pending human-in-the-loop review and approval]"
        },
        "status": "DRAFT – PENDING ANALYST APPROVAL"
    }

    # ── Anti-Corruption Layer ─────────────────────────────────────
    validated = _validate(FinalReport, raw, "compile_and_render_report")

    print(f"      ← AppWorks returned: report [{validated['report_id']}], "
          f"status [{validated['status']}] ✓ validated")
    return validated