# semantic_layer/appworks_services.py
# ----------------------------------------------------------------
# MOCK – Simulates AppWorks Web Service HTTP calls.
# Each function prints what the real call would do,
# then returns hardcoded realistic data.
#
# TO PRODUCTIONIZE: Replace the body of each function with
# an actual requests.get/post to the AppWorks REST endpoint shown.
# ----------------------------------------------------------------

import time

def _mock_http(method: str, endpoint: str, payload: dict = None):
    """Simulates network latency and prints the would-be HTTP call."""
    print(f"      [AppWorks HTTP] {method} {endpoint}")
    if payload:
        print(f"      [AppWorks HTTP] Payload: {payload}")
    time.sleep(0.3)  # simulate real network call


# ----------------------------------------------------------------
# AGENT: Complaint Intelligence Agent
# ----------------------------------------------------------------

def get_case_header(case_id: str) -> dict:
    """
    PRODUCTION CALL:
      GET /appworks/rest/v1/cases/{case_id}/header
      Headers: Authorization: Bearer {session_token}

    RETURNS: Case header with complainant, subject, narrative, fraud classification
    """
    print(f"\n      → Calling AppWorks: fetch case header for [{case_id}]")
    _mock_http("GET", f"/appworks/rest/v1/cases/{case_id}/header")

    mock_data = {
        "BSI-2024-00421": {
            "case_id":                 "BSI-2024-00421",
            "complainant_name":        "MassHealth Audit Division",
            "subject_primary":         "Dr. Amir Hosseini",
            "subject_primary_id":      "SUBJ-7821",
            "subject_secondary":       "Sunrise Home Health LLC",
            "complaint_description":   (
                "Subject billed for home health aide services on 47 dates "
                "where the patient was confirmed hospitalized. Claims totaling "
                "$84,200 submitted over 6 months with no supporting documentation."
            ),
            "fraud_type_classified":   "BILLING",
            "intake_date":             "2024-03-15",
            "status":                  "OPEN"
        }
    }

    result = mock_data.get(case_id, {"error": f"Case {case_id} not found in AppWorks"})
    print(f"      ← AppWorks returned: case [{case_id}], fraud type [{result.get('fraud_type_classified')}]")
    return result


# ----------------------------------------------------------------
# AGENT: Context Enrichment Agent
# ----------------------------------------------------------------

def get_enriched_subject_profile(subject_id: str) -> dict:
    """
    PRODUCTION CALLS (joined internally):
      GET /appworks/rest/v1/subjects/{subject_id}
      GET /appworks/rest/v1/subjects/{subject_id}/cases?years=5
      GET /appworks/rest/v1/subjects/{subject_id}/addresses
      GET /appworks/rest/v1/subjects/{subject_id}/associates

    RETURNS: Enriched profile with history, addresses, known associates
    """
    print(f"\n      → Calling AppWorks: fetch enriched profile for subject [{subject_id}]")
    _mock_http("GET", f"/appworks/rest/v1/subjects/{subject_id}")
    _mock_http("GET", f"/appworks/rest/v1/subjects/{subject_id}/cases?years=5")
    _mock_http("GET", f"/appworks/rest/v1/subjects/{subject_id}/addresses")
    _mock_http("GET", f"/appworks/rest/v1/subjects/{subject_id}/associates")

    mock_data = {
        "SUBJ-7821": {
            "subject_id":       "SUBJ-7821",
            "full_name":        "Dr. Amir Hosseini",
            "dob":              "1968-04-22",
            "address_history": [
                {"address": "14 Beacon St, Boston MA", "from": "2021-01", "to": "present"},
                {"address": "88 Elm Ave, Quincy MA",   "from": "2018-06", "to": "2020-12"}
            ],
            "prior_cases": [
                {"case_id": "BSI-2021-00188", "year": 2021, "fraud_type": "BILLING", "outcome": "SUBSTANTIATED"},
                {"case_id": "BSI-2019-00044", "year": 2019, "fraud_type": "BILLING", "outcome": "UNSUBSTANTIATED"}
            ],
            "known_associates": [
                {"name": "Sunrise Home Health LLC", "relationship": "Employer",    "subject_id": "SUBJ-9034"},
                {"name": "Maria Hosseini",          "relationship": "Co-signatory","subject_id": "SUBJ-9201"}
            ],
            "prior_case_count": 2
        }
    }

    result = mock_data.get(subject_id, {"error": f"Subject {subject_id} not found"})
    print(f"      ← AppWorks returned: [{result.get('prior_case_count')} prior cases], "
          f"[{len(result.get('known_associates', []))} associates]")
    return result


# ----------------------------------------------------------------
# AGENT: Similar Case Retrieval Agent
# ----------------------------------------------------------------

def vector_search_cases(complaint_text: str, top_n: int = 3) -> dict:
    """
    PRODUCTION CALL:
      POST /vector-service/search
      Body: { "text": complaint_text, "top_n": top_n, "collection": "bsi_cases" }
      (Vector DB service wrapping Pinecone / pgvector)

    RETURNS: Top-N similar cases with similarity scores
    """
    print(f"\n      → Calling Vector DB: semantic search on complaint narrative")
    print(f"      [Vector DB] POST /vector-service/search  top_n={top_n}")
    print(f"      [Vector DB] Embedding text: \"{complaint_text[:60]}...\"")
    time.sleep(0.4)

    result = {
        "query_summary": "Home health billing fraud – hospitalized patient overlap pattern",
        "matches": [
            {
                "case_id":          "BSI-2022-00317",
                "similarity_score": 0.94,
                "fraud_type":       "BILLING",
                "outcome":          "SUBSTANTIATED",
                "summary":          "Provider billed 38 days of home services while patient was in ICU. $61,400 recovered."
            },
            {
                "case_id":          "BSI-2021-00188",
                "similarity_score": 0.89,
                "fraud_type":       "BILLING",
                "outcome":          "SUBSTANTIATED",
                "summary":          "Same subject. Duplicate billing across two provider entities. $29,000 overpayment."
            },
            {
                "case_id":          "BSI-2020-00502",
                "similarity_score": 0.81,
                "fraud_type":       "BILLING",
                "outcome":          "REFERRED_TO_AG",
                "summary":          "Home health agency billed for deceased patient for 3 months post-death."
            }
        ][:top_n],
        "top_n_returned": min(top_n, 3)
    }

    print(f"      ← Vector DB returned: [{result['top_n_returned']} matches], "
          f"top score [{result['matches'][0]['similarity_score']}]")
    return result


# ----------------------------------------------------------------
# AGENT: Fraud Risk Assessment Agent
# ----------------------------------------------------------------

def get_risk_measures(case_id: str, subject_id: str) -> dict:
    """
    PRODUCTION CALLS:
      GET /appworks/rest/v1/cases/{case_id}/billing-summary
      GET /appworks/rest/v1/subjects/{subject_id}/risk-profile
      POST /rules-engine/evaluate  Body: { case_id, subject_id }

    RETURNS: Risk score, tier, triggered rules, recommendation
    """
    print(f"\n      → Calling AppWorks: fetch billing summary + risk profile")
    _mock_http("GET",  f"/appworks/rest/v1/cases/{case_id}/billing-summary")
    _mock_http("GET",  f"/appworks/rest/v1/subjects/{subject_id}/risk-profile")
    print(f"      [Rules Engine] POST /rules-engine/evaluate")
    time.sleep(0.3)

    result = {
        "case_id":    case_id,
        "subject_id": subject_id,
        "risk_score": 0.87,
        "risk_tier":  "HIGH",
        "triggered_rules": [
            {"rule_id": "R-101", "rule_name": "Billing During Active Hospitalization",   "weight": 0.40},
            {"rule_id": "R-205", "rule_name": "Prior Substantiated Case Within 3 Years", "weight": 0.25},
            {"rule_id": "R-312", "rule_name": "Claim Volume Spike (>40 claims / 6 mo)",  "weight": 0.22}
        ],
        "billing_anomaly_flag": True,
        "prior_case_count":     2,
        "recommendation":       "Escalate to Senior Investigator. Request full billing records subpoena."
    }

    print(f"      ← Rules Engine returned: score [{result['risk_score']}], "
          f"tier [{result['risk_tier']}], [{len(result['triggered_rules'])} rules triggered]")
    return result


# ----------------------------------------------------------------
# AGENT: Case Strategy Agent
# ----------------------------------------------------------------

def get_playbook_by_type(fraud_type: str, risk_level: str) -> dict:
    """
    PRODUCTION CALL:
      GET /appworks/rest/v1/playbooks?fraud_type={fraud_type}&risk_level={risk_level}
      (AppWorks document library – BSI investigation manual)

    RETURNS: Ordered investigation steps, evidence checklist, escalation flag
    """
    print(f"\n      → Calling AppWorks: fetch investigation playbook "
          f"[{fraud_type}] / [{risk_level}]")
    _mock_http("GET", f"/appworks/rest/v1/playbooks?fraud_type={fraud_type}&risk_level={risk_level}")

    playbooks = {
        ("BILLING", "HIGH"): {
            "playbook_id": "PB-BILLING-HIGH-v3",
            "fraud_type":  "BILLING",
            "risk_level":  "HIGH",
            "investigation_steps": [
                {"step": 1, "action": "Pull complete billing history from MassHealth claims system",     "owner": "Analyst",      "deadline_days": 3},
                {"step": 2, "action": "Cross-reference claim dates with hospital admission records",     "owner": "Analyst",      "deadline_days": 5},
                {"step": 3, "action": "Issue subpoena for provider service logs and patient records",    "owner": "Investigator", "deadline_days": 10},
                {"step": 4, "action": "Interview patient and / or family members",                       "owner": "Investigator", "deadline_days": 14},
                {"step": 5, "action": "Prepare referral package for Attorney General if substantiated",  "owner": "Director",     "deadline_days": 21}
            ],
            "evidence_checklist": [
                {"item": "MassHealth claims printout",           "mandatory": True},
                {"item": "Hospital admission/discharge records",  "mandatory": True},
                {"item": "Provider service documentation",        "mandatory": True},
                {"item": "Patient statement",                     "mandatory": False},
                {"item": "Prior case file BSI-2021-00188",        "mandatory": False}
            ],
            "escalation_required": True
        }
    }

    key    = (fraud_type.upper(), risk_level.upper())
    result = playbooks.get(key, {
        "playbook_id":          "PB-DEFAULT",
        "fraud_type":           fraud_type,
        "risk_level":           risk_level,
        "investigation_steps":  [{"step": 1, "action": "Manual review required", "owner": "Analyst", "deadline_days": 5}],
        "evidence_checklist":   [],
        "escalation_required":  False
    })

    print(f"      ← AppWorks returned: playbook [{result['playbook_id']}], "
          f"[{len(result['investigation_steps'])} steps], "
          f"escalation=[{result['escalation_required']}]")
    return result


# ----------------------------------------------------------------
# AGENT: Report Generation Agent
# ----------------------------------------------------------------

def compile_and_render_report(case_id: str) -> dict:
    """
    PRODUCTION CALLS:
      GET /appworks/rest/v1/cases/{case_id}/full-summary
      POST /appworks/rest/v1/reports/render
      Body: { case_id, template: "BSI_INVESTIGATION_REPORT_v2" }

    RETURNS: Structured report dict (rendered to PDF via AppWorks template in production)
    """
    print(f"\n      → Calling AppWorks: compile and render final report for [{case_id}]")
    _mock_http("GET",  f"/appworks/rest/v1/cases/{case_id}/full-summary")
    _mock_http("POST", "/appworks/rest/v1/reports/render",
               {"case_id": case_id, "template": "BSI_INVESTIGATION_REPORT_v2"})

    result = {
        "report_id":    f"RPT-{case_id}",
        "case_id":      case_id,
        "generated_at": "2024-03-15T14:32:00Z",
        "sections": {
            "case_summary":       "Billing fraud complaint against Dr. Amir Hosseini / Sunrise Home Health LLC. "
                                  "Claims of $84,200 submitted for dates patient was confirmed hospitalized.",
            "subject_history":    "Subject has 2 prior BSI cases. BSI-2021-00188 was substantiated for billing fraud.",
            "similar_cases":      "3 similar historical cases found. All substantiated. Pattern consistent with "
                                  "hospitalization-overlap billing scheme.",
            "risk_assessment":    "Risk Score: 0.87 (HIGH). Three rules triggered: R-101, R-205, R-312.",
            "recommended_actions":"Escalate to Senior Investigator. Subpoena billing records. "
                                  "Cross-reference MassHealth claims with hospital admission records.",
            "analyst_notes":      "[Pending human-in-the-loop review and approval]"
        },
        "status": "DRAFT – PENDING ANALYST APPROVAL"
    }

    print(f"      ← AppWorks returned: report [{result['report_id']}], status [{result['status']}]")
    return result