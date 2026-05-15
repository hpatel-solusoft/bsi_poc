"""
AppWorks Service Router.
Dispatches calls to the underlying feature-specific service modules.
Every function MUST return {"result": {...}, "provenance": {...}}.
"""
import semantic_layer.services.f1_intake_services as f1
import semantic_layer.services.f2_enrichment_services as f2
import semantic_layer.services.f3_case_retrieval_services as f3
import semantic_layer.services.f4_risk_services as f4
import semantic_layer.services.f5_strategy_services as f5
import semantic_layer.services.f6_report_services as f6
import semantic_layer.semantic_model as model
import logging

logger = logging.getLogger(__name__)


def _validate(model_class, envelope: dict, tool_name: str) -> dict:
    """
    Normalization Layer Gatekeeper.
    Validates the 'result' portion of a tool response against a Pydantic model.
    Ensures absolute schema alignment before the LLM consumes the data.
    Returns the envelope with the 'result' key replaced by validated_data.

    Raises ValueError on failure — the dispatcher's Gate 3 try/except catches
    this and returns a structured error envelope to the LLM, keeping HTTP
    concerns out of the service layer (NEW-B).
    """
    try:
        raw_result = envelope.get("result", {})
        # Create a validated instance. .model_dump() returns a clean dict.
        validated_data = model_class(**raw_result).model_dump(by_alias=True)
        # Return a new envelope with result replaced by the validated output.
        # The original envelope (including provenance) is preserved unchanged.
        return {**envelope, "result": validated_data}
    except Exception as e:
        error_msg = f"Tool '{tool_name}' response failed model validation: {e}"
        logger.error(error_msg)
        raise ValueError(error_msg)



def get_case_header(case_id: str) -> dict:
    """Dispatched from 'verify_case_intake'"""
    res = f1.build_case_header_data(case_id)
    return _validate(model.CaseHeader, res, "verify_case_intake")


def get_enriched_subject_profile(subject_ids: list, case_id: str = None) -> dict:
    """Dispatched from 'fetch_subject_history'

    Accepts a list of subject IDs (as declared in the manifest).
    Returns a SubjectHistory object containing individual profiles
    for every subject provided.
    """
    if not subject_ids:
        raise ValueError("subject_ids list is empty — at least one subject ID is required")

    profiles = []
    total_cases = 0
    provenance_sources = []
    last_retrieved_at = ""
    last_computed_by = ""

    for sid in subject_ids:
        res = f2.get_enriched_subject_profile(sid, case_id=case_id)
        result = res.get("result", {})
        prov = res.get("provenance", {})

        # Build individual profile for this subject
        profile = {
            "subject_id": sid,
            "first_name": result.get("first_name"),
            "last_name": result.get("last_name"),
            "dob": result.get("dob"),
            "prior_cases": result.get("prior_cases", []),
            "prior_case_count": result.get("prior_case_count", 0),
        }
        profiles.append(profile)
        total_cases += profile["prior_case_count"]

        provenance_sources.extend(prov.get("sources", []))
        last_retrieved_at = prov.get("retrieved_at", "")
        last_computed_by = prov.get("computed_by", "")

    # Build combined SubjectHistory envelope
    combined = {
        "result": {
            "profiles": profiles,
            "total_prior_case_count": total_cases,
        },
        "provenance": {
            "sources": list(set(provenance_sources)), # Deduplicate
            "retrieved_at": last_retrieved_at,
            "computed_by": last_computed_by,
        },
    }
    return _validate(model.SubjectHistory, combined, "fetch_subject_history")


def search_similar_cases(
    case_id: str = None,
    fraud_types: list = None,
    complaint_description: str = None,
) -> dict:
    """Dispatched from 'search_similar_cases'"""
    res = f3.search_similar_cases(
        fraud_types=fraud_types,
        case_id=case_id,
        complaint_description=complaint_description,
    )
    return _validate(model.SimilarCasesResult, res, "search_similar_cases")


def get_risk_rules() -> dict:
    """Dispatched from 'get_risk_rules'"""
    res = f4.get_risk_rules()
    return _validate(model.RiskRulesResult, res, "get_risk_rules")


def calculate_risk_metrics(
    case_id: str,
    subject_id: str,
    fraud_types: list,
    active_rules: list = None,
    prior_case_count: int = None,
    primary_in_prior_cases: int = None,
    total_calculated: float = None,
    total_ordered: float = None,
    similar_case_volume: int = None,
    distinct_types: int = None,
    has_open_allegation: bool = None,
    fast_track: bool = None,
    subject_count: int = None,
    received_age: int = None
) -> dict:
    """Dispatched from 'calculate_risk_metrics'"""
    res = f4.calculate_risk_metrics(
        case_id=case_id,
        subject_id=subject_id,
        fraud_types=fraud_types,
        active_rules=active_rules,
        prior_case_count=prior_case_count,
        primary_in_prior_cases=primary_in_prior_cases,
        total_calculated=total_calculated,
        total_ordered=total_ordered,
        similar_case_volume=similar_case_volume,
        distinct_types=distinct_types,
        has_open_allegation=has_open_allegation,
        fast_track=fast_track,
        subject_count=subject_count,
        received_age=received_age
    )
    return _validate(model.RiskAssessment, res, "calculate_risk_metrics")


def get_investigation_playbook(
    fraud_types: list,
    risk_tier: str,
    case_data: dict = None,
) -> dict:
    """Dispatched from 'get_investigation_playbook'"""
    res = f5.get_investigation_playbook(
        fraud_types=fraud_types,
        risk_tier=risk_tier,
        case_data=case_data,
    )
    return _validate(model.InvestigationPlaybook, res, "get_investigation_playbook")


def compile_and_render_report(
    case_id: str,
    subject_id: str,
    fraud_types: list,
    risk_score: float,
    risk_tier: str,
    risk_indicators: list,
) -> dict:
    """Dispatched from 'generate_final_report'"""
    res = f6.compile_and_render_report(
        case_id=case_id,
        subject_id=subject_id,
        fraud_types=fraud_types,
        risk_score=risk_score,
        risk_tier=risk_tier,
        risk_indicators=risk_indicators,
    )
    return _validate(model.FinalReport, res, "generate_final_report")
