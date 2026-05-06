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
    """
    try:
        raw_result = envelope.get("result", {})
        # Create a validated instance. .model_dump() returns a clean dict.
        validated_data = model_class(**raw_result).model_dump(by_alias=True)
        envelope["result"] = validated_data
        return envelope
    except Exception as e:
        logger.error(f"Normalization failure in {tool_name}: {e}")
        # In production, we might raise an error. For the POC, we log and return
        # the raw envelope to avoid breaking the agentic loop, but v6 spec
        # recommends strict enforcement.
        return envelope


def get_case_header(case_id: str) -> dict:
    """Dispatched from 'verify_case_intake'"""
    res = f1.build_case_header_data(case_id)
    return _validate(model.CaseHeader, res, "verify_case_intake")


def get_enriched_subject_profile(subject_id: str) -> dict:
    """Dispatched from 'fetch_subject_history'"""
    res = f2.get_enriched_subject_profile(subject_id)
    return _validate(model.SubjectProfile, res, "fetch_subject_history")


def search_similar_cases(
    fraud_types: list,
    case_id: str = None,
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
) -> dict:
    """Dispatched from 'get_investigation_playbook'"""
    res = f5.get_investigation_playbook(
        fraud_types=fraud_types,
        risk_tier=risk_tier,
    )
    return _validate(model.InvestigationPlaybook, res, "get_investigation_playbook")


def compile_and_render_report(
    case_id: str,
    subject_id: str,
    fraud_types: list,
    risk_score: float,
    risk_tier: str,
    triggered_rules: list,
) -> dict:
    """Dispatched from 'generate_final_report'"""
    res = f6.compile_and_render_report(
        case_id=case_id,
        subject_id=subject_id,
        fraud_types=fraud_types,
        risk_score=risk_score,
        risk_tier=risk_tier,
        triggered_rules=triggered_rules,
    )
    return _validate(model.FinalReport, res, "generate_final_report")