"""
AppWorks calls for case intake (verify_case_intake).
Owns: fetching and assembling the CS-4 case snapshot handed to the agent
for one Workfolder — the root case record plus its subjects, allegations,
and financials.

Does NOT own the AppWorks->canonical parsing itself. That lives in
appworks/entity_mappers.py, shared with appworks/subject_enrichment.py.
This file's job is orchestration: fetch the root Workfolder, call the
shared mappers, synthesize the derived convenience fields
(fraud_types, subject_ids, subject_primary_id) the agent tools need, and
return the {result, provenance} envelope.
"""

import logging
from typing import Dict, List, Any

from appworks.appworks_utils import safe_fetch
from appworks.provenance import ProvenanceTracker
from appworks.entity_mappers import map_allegations, map_subjects, map_financials
from appworks.appworks_paths import AppWorksPaths

logger = logging.getLogger(__name__)


def _derive_fraud_types(allegations_list: List[Dict], case_props: Dict) -> List[str]:
    """
    Extracts a deduplicated list of fraud types from allegations,
    falling back to legacy case properties if no allegations exist.
    """
    types = []
    for alleg in allegations_list:
        desc = (
            alleg.get("allegation_type", {}).get("description") or
            alleg.get("allegation_type", {}).get("short_desc")
        )
        if desc and desc not in types:
            types.append(desc)

    if not types:
        # Fallback for dirty data / legacy schema
        fallback = (
            case_props.get("WorkfolderAllegation") or
            case_props.get("WorkFolderAllegation") or
            case_props.get("Workfolder_Allegation") or
            case_props.get("WorkFolder_Allegation")
        )
        if isinstance(fallback, str) and fallback.strip():
            types.append(fallback.strip())

    return types


def build_case_header_data(case_id: str) -> Dict[str, Any]:
    """
    Main orchestrator for the verify_case_intake tool.
    Fetches the root Workfolder and delegates subject/allegation/financial
    retrieval to the shared entity_mappers (single-call /lists/ endpoints).
    Returns the strict architecture-compliant {result, provenance} envelope.
    """
    logger.info(f"🚀 [LIVE] Initiating deep fetch for Case ID: {case_id}")

    # 1. Initialize System-Wide Provenance Tracker (Principle 8)
    tracker = ProvenanceTracker("Workfolder", case_id)

    # 2. Fetch Root Workfolder
    endpoint = AppWorksPaths.Workfolder.item(case_id)
    logger.info(f"📡 Requesting Workfolder from: {endpoint}")
    props, links = safe_fetch(endpoint, "Workfolder")

    # Guard clause: If the core case doesn't exist or API is down, fail safely
    if not props:
        logger.error(f"❌ Critical Error: Could not fetch root Workfolder for {case_id}")
        return {
            "result": {"case_id": case_id, "error": "Case record not found or API unavailable"},
            "provenance": tracker.get_provenance_block(computed_by="Failed REST retrieval")
        }

    logger.info(f"✅ Successfully retrieved Workfolder for {case_id}")

    # 3. Delegate to shared mappers — each is a single /lists/ call now
    # (see entity_mappers.py docstring), keyed off the workfolder id itself
    # rather than a relationship href.
    allegations_list = map_allegations(case_id, tracker)
    subjects_list = map_subjects(case_id, tracker)
    financials = map_financials(case_id, tracker)

    # 4. Synthesize Derived Fields
    fraud_types = _derive_fraud_types(allegations_list, props)
    subject_ids = [s["subject_id"] for s in subjects_list if s.get("subject_id")]
    primary_subject_id = next((s["subject_id"] for s in subjects_list if s.get("is_primary_subject")), None)

    # 5. Build Final Semantic Payload
    clean_result = {
        "case_id": props.get("CASEID", case_id),
        "summary": {
            "complaint_no": props.get("WorkfolderComplaintNumber"),
            "description": props.get("WorkfolderDescription"),
            "case_description": props.get("Workfolder_CaseDescription"),
            "status": props.get("WorkfolderStatus"),
            "created": props.get("CREATION_DATE"),
        },
        "details": {
            "source": props.get("WorkfolderSource"),
            "identifier_name": props.get("IDENTIFIER_NAME"),
            "date_reported": props.get("WorkfolderDateReported"),
            "date_reported_age": props.get("WorkfolderDateReportedAge"),
            "date_received": props.get("WorkfolderDateReceived"),
            "date_received_age": props.get("WorkfolderDateReceivedAge"),
            "date_entered_age": props.get("WorkfolderDateEnteredAge"),
            "workfolder_allegation": props.get("WorkFolderAllegation"),
            "co_subject_name": props.get("WorkfolderCoSubjectName"),
            "subject_city": props.get("WorkfolderSubjectCity"),
        },
        "allegations": allegations_list,
        "subjects": subjects_list,
        "subject_ids": subject_ids,
        "subject_primary_id": primary_subject_id,
        "financials": financials,
        "fraud_types": fraud_types,
    }

    logger.info(
        f"✅ clean_result built — {len(allegations_list)} allegation(s), "
        f"{len(subjects_list)} subject(s)"
    )

    # 6. Return Architecture Envelope
    return {
        "result": clean_result,
        "provenance": tracker.get_provenance_block()
    }
