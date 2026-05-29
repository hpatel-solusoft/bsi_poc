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
    complaint_description: str = None,
) -> dict:
    """Dispatched from 'search_similar_cases'"""
    res = similar_cases.search_similar_cases(
        fraud_types=fraud_types,
        case_id=case_id,
        complaint_description=complaint_description,
    )
    return _validate(contracts.SimilarCasesResult, res, "search_similar_cases")


def get_risk_rules() -> dict:
    """Dispatched from 'get_risk_rules'"""
    res = risk_scoring.get_risk_rules()
    return _validate(contracts.RiskRulesResult, res, "get_risk_rules")


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
    received_age: int = None,
) -> dict:
    """Dispatched from 'calculate_risk_metrics'"""
    res = risk_scoring.calculate_risk_metrics(
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
        received_age=received_age,
    )
    return _validate(contracts.RiskAssessment, res, "calculate_risk_metrics")


def get_investigation_plan(
    fraud_types: list,
    risk_tier: str
) -> dict:
    """Dispatched from 'get_investigation_plan'"""
    res = investigation_strategy.get_investigation_plan(
        fraud_types=fraud_types,
        risk_tier=risk_tier
    )
    return _validate(contracts.InvestigationPlan, res, "get_investigation_plan")

_ALLEGATIONS_ALL_LIST_ENDPOINT = "/entities/Allegations/lists/Allegations_All"

_ALLEGATIONS_TYPE_LIST_ENDPOINT = "/entities/AllegationType/lists/AllegationType_ManageAllegationType"
def _id_from_appworks_href(href: str) -> str:
    if not href:
        return ""
    match = re.search(r"/items/(\d+)", href)
    return match.group(1) if match else ""


def _allegation_type_from_row(row: dict) -> Optional[Any]:
    """Extract allegation type from an Allegations_All list row."""
    if not isinstance(row, dict):
        return None

    type_props = row.get("Allegations_AllegationsType$Properties")
    type_identity = row.get("Allegations_AllegationsType$Identity")
    if isinstance(type_props, dict) and type_props:
        payload = dict(type_props)
        if isinstance(type_identity, dict):
            type_id = type_identity.get("Id") or type_identity.get("id")
            if type_id is not None:
                payload["Id"] = str(type_id)
        elif type_identity is not None and str(type_identity).strip():
            payload["Id"] = str(type_identity).strip()
        return payload

    props = row.get("Properties", {})
    raw = props.get("Allegations_AllegationsType")
    if isinstance(raw, dict) and raw:
        return raw
    if raw is not None and str(raw).strip():
        return raw

    type_href = (
        row.get("_links", {})
        .get("relationship:Allegations_AllegationsType", {})
        .get("href", "")
    )
    type_id = _id_from_appworks_href(type_href)
    if type_id:
        return {"Id": type_id}
    return None

def _type_id_from_row(item: dict) -> Optional[str]:
    """Read allegation type id from an Allegations_All list row."""
    identity = item.get("Allegations_AllegationsType$Identity")
    if isinstance(identity, dict):
        raw = identity.get("Id") or identity.get("id")
    else:
        raw = identity
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip()

def get_allegation_types_original() -> dict:
    raw = fetch(_ALLEGATIONS_ALL_LIST_ENDPOINT)
    items = raw if isinstance(raw, list) else raw.get("_embedded", {}).get("Allegations_All", [])
    seen_type_ids = set()
    allegation_types = []
    
    for item in items:
        type_props = item.get("Allegations_AllegationsType$Properties", {})
        type_id    = item.get("Allegations_AllegationsType$Identity", {}).get("Id")
        if not type_id or type_id in seen_type_ids:
            continue
        seen_type_ids.add(type_id)
        allegation_types.append({
            "type_id":      type_id,
            "short_code":   type_props.get("AllegationType_AllegationTypeShortDesc", ""),
            "description":  type_props.get("AllegationType_AllegationTypeDescription", ""),
            "default_text": type_props.get("AllegationType_AllegationTypeDefaults", ""),
        })
    
    envelope = {
        "result": {
            "allegation_types": allegation_types,
            "total_types":      len(allegation_types),
        },

        "provenance": {
            "sources":      [_ALLEGATIONS_ALL_LIST_ENDPOINT],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by":  "get_allegation_types",
        }
    }
    print("************************************")
    print(allegation_types)
    return _validate(contracts.AllegationTypesResult, envelope, "get_allegation_types")
 

def get_allegation_types() -> dict:
    raw = fetch(_ALLEGATIONS_TYPE_LIST_ENDPOINT)
    items = raw if isinstance(raw, list) else raw.get("_embedded", {}).get("AllegationType_ManageAllegationType", [])
    seen_type_ids = set()
    allegation_types = []
    
    for item in items:
        type_props = item.get("Properties", {})
        href = item.get("_links", {}).get("item", {}).get("href", "")
        type_id = href.rstrip("/").split("/")[-1] if href else None
        if not type_id or type_id in seen_type_ids:
            continue
        seen_type_ids.add(type_id)
        
        allegation_types.append({
            "type_id":      type_id,
            "short_code":   type_props.get("AllegationType_AllegationTypeShortDesc", ""),
            "description":  type_props.get("AllegationType_AllegationTypeDescription", ""),
            "default_text": type_props.get("AllegationType_AllegationTypeDefaults", ""),
        })
    envelope = {
        "result": {
            "allegation_types": allegation_types,
            "total_types":      len(allegation_types),
        },

        "provenance": {
            "sources":      [_ALLEGATIONS_TYPE_LIST_ENDPOINT],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by":  "get_allegation_types",
        }
    }
    print("************************************")
    print(allegation_types)
    return _validate(contracts.AllegationTypesResult, envelope, "get_allegation_types")
