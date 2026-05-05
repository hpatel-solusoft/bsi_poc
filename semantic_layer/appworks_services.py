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


def get_case_header(case_id: str) -> dict:
    """Dispatched from 'verify_case_intake'"""
    return f1.build_case_header_data(case_id)


def get_enriched_subject_profile(subject_id: str) -> dict:
    """Dispatched from 'fetch_subject_history'"""
    return f2.get_enriched_subject_profile(subject_id)


def search_similar_cases(
    fraud_types: list,
    case_id: str = None,
    complaint_description: str = None,
) -> dict:
    """Dispatched from 'search_similar_cases'"""
    return f3.search_similar_cases(
        fraud_types=fraud_types,
        case_id=case_id,
        complaint_description=complaint_description,
    )


def get_risk_rules() -> dict:
    """Dispatched from 'get_risk_rules'"""
    return f4.get_risk_rules()


def calculate_risk_metrics(case_id: str, subject_id: str, fraud_types: list) -> dict:
    """Dispatched from 'calculate_risk_metrics'"""
    return f4.calculate_risk_metrics(case_id, subject_id, fraud_types)


def get_investigation_playbook(
    fraud_types: list,
    risk_tier: str,
) -> dict:
    """Dispatched from 'get_investigation_playbook'"""
    return f5.get_investigation_playbook(
        fraud_types=fraud_types,
        risk_tier=risk_tier,
    )


def compile_and_render_report(
    case_id: str,
    subject_id: str,
    fraud_types: list,
    risk_score: float,
    risk_tier: str,
    triggered_rules: list,
) -> dict:
    """Dispatched from 'generate_final_report'"""
    return f6.compile_and_render_report(
        case_id=case_id,
        subject_id=subject_id,
        fraud_types=fraud_types,
        risk_score=risk_score,
        risk_tier=risk_tier,
        triggered_rules=triggered_rules,
    )