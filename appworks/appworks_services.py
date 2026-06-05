"""
AppWorks Service Router.
Dispatches calls to the underlying feature-specific service modules.
Every function MUST return {"result": {...}, "provenance": {...}}.
"""
import appworks.case_intake as case_intake
import appworks.subject_enrichment as subject_enrichment
import appworks.similar_cases as similar_cases
import appworks.risk_scoring as risk_scoring
import appworks.investigation_strategy as investigation_strategy
import semantic_layer.entity_contracts as contracts
from appworks.appworks_auth import fetch,fetch_list
from datetime import datetime, timezone
import logging
from typing import List, Optional, Any
import json
import re

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
    res = case_intake.build_case_header_data(case_id)
    return _validate(contracts.CaseHeader, res, "verify_case_intake")


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
        res = subject_enrichment.get_enriched_subject_profile(sid, case_id=case_id)
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
    return _validate(contracts.SubjectHistory, combined, "fetch_subject_history")


def search_similar_cases(
    case_id: str = None,
    fraud_types: list = None,
    max_total_results: int = 3,
    **kwargs
) -> dict:
    """Dispatched from 'search_similar_cases'"""
    res = similar_cases.search_similar_cases(
        fraud_types=fraud_types,
        case_id=case_id,
        max_total_results=max_total_results,
        **kwargs
    )
    return _validate(contracts.SimilarCasesResult, res, "search_similar_cases")


def get_risk_rules(**kwargs) -> dict:
    """Dispatched from 'get_risk_rules'"""
    res = risk_scoring.get_risk_rules(**kwargs)
    return _validate(contracts.RiskRulesResult, res, "get_risk_rules")


def calculate_risk_metrics(
    case_id: str,
    subject_id: str,
    fraud_types: list,
    prior_case_count: int = None,
    primary_in_prior_cases: int = None,
    total_calculated: float = None,
    total_ordered: float = None,
    similar_case_volume: int = None,
    distinct_types: int = None,
    has_open_allegation: bool = None,
    fast_track: bool = None,
    subject_count: int = None,
    received_age: int = None,
    **kwargs
) -> dict:
    """Dispatched from 'calculate_risk_metrics'"""
    res = risk_scoring.calculate_risk_metrics(
        case_id=case_id,
        subject_id=subject_id,
        fraud_types=fraud_types,
        prior_case_count=prior_case_count,
        primary_in_prior_cases=primary_in_prior_cases,
        total_calculated=total_calculated,
        total_ordered=total_ordered,
        similar_case_volume=similar_case_volume,
        distinct_types=distinct_types,
        has_open_allegation=has_open_allegation,
        fast_track=fast_track,
        subject_count=subject_count,
        received_age=received_age,
        **kwargs
    )
    return _validate(contracts.RiskAssessment, res, "calculate_risk_metrics")


def get_investigation_plan(
    fraud_types: list,
    risk_tier: str, 
    ai_summary=None,
    **kwargs
) -> dict:
    """Dispatched from 'get_investigation_plan'"""
    res = investigation_strategy.get_investigation_plan(
        fraud_types=fraud_types,
        risk_tier=risk_tier,
        ai_summary=ai_summary,
        **kwargs
    )
    return _validate(contracts.InvestigationPlan, res, "get_investigation_plan")


def get_allegation_types(**kwargs) -> dict:
    """Dispatched from 'get_allegation_types' — feeds search_similar_cases"""
    res = similar_cases.get_allegation_types(**kwargs)
    return _validate(contracts.AllegationTypesResult, res, "get_allegation_types")
