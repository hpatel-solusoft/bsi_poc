"""
Subject Enrichment Services
---------------------------
Data functions for the Context Enrichment Agent (Agent 2).
Manifest tool: fetch_subject_history
"""

import logging
from typing import Dict, List, Optional, Any
from appworks.entity_mappers import map_allegations, map_commentary, map_workfolder_core
from appworks.appworks_paths import AppWorksPaths
from appworks.appworks_utils import safe_fetch, extract_id_from_href, get_relationship_items
from appworks.provenance import ProvenanceTracker

logger = logging.getLogger(__name__)

def get_enriched_subject_profile(subject_id: str, case_id: Optional[str] = None) -> Dict:
    """
    Fetches deep subject history and prior cases for a given subject_id.
    """
    logger.info(f"🚀 [LIVE] Context Enrichment for Subject ID: {subject_id}")
    
    tracker = ProvenanceTracker("Subject", subject_id)

    # ── Step 1: Fetch Base Subject Info ──────────────────────────────────────
    subject_href = AppWorksPaths.Subject.item(subject_id)
    subj_props, subj_links = safe_fetch(subject_href, "Subject")

    first_name = subj_props.get("Subject_FirstName", "")
    last_name  = subj_props.get("Subject_LastName", "")
    dob        = subj_props.get("Subject_DOB")

    # ── Step 2: Get Subject_SubjectWorkfolderMapping list ───────────────
    mapping_href = AppWorksPaths.Subject.workfolder_mappings(subject_id)
    mapping_items = get_relationship_items(mapping_href, "Subject_SubjectWorkfolderMapping")

    logger.info(f"📋 Found {len(mapping_items)} mapping entry/entries for Subject {subject_id}")

    prior_cases = []

    for mapping_item in mapping_items:
        try:
            mapping_self_href = mapping_item.get("_links", {}).get("self", {}).get("href", "")
            is_primary = mapping_item.get("Properties", {}).get("SubjectWorkfolderMapping_IsPrimary", False)
            title_text = mapping_item.get("Title", {}).get("Title", "")

            mapping_props, mapping_links = safe_fetch(mapping_self_href, "SubjectWorkfolderMapping")

            wf_href = mapping_links.get("relationship:SubjectWorkfolderMapping_WorkfolderRelation", {}).get("href", "")
            wf_id = extract_id_from_href(wf_href)

            if not wf_id:
                continue

            # Exclude current case
            if case_id and str(wf_id) == str(case_id):
                logger.info(f"  Skipping current case {wf_id} from prior case history")
                continue

            # Fetch linked Workfolder summary and track it
            logger.info(f"📂 Fetching linked Workfolder: {wf_id}")
            wf_props, _ = safe_fetch(wf_href, "Workfolder")
            if wf_props:
                tracker.add_source("Workfolder", wf_id)

            core_props = map_workfolder_core(wf_props)
            commentary = map_commentary(wf_id, tracker)
            allegations = map_allegations(wf_id, tracker)

            prior_cases.append({
                "workfolder_id":      wf_id,
                "is_primary_subject": is_primary,
                "mapping_title":      title_text,
                **core_props,
                #"commentary":         commentary, ## [HP] Depending on needs, we can include the full commentary or just the count in the final payload, No need to feed this to LLM except the amount and type of commentary
                #"allegations":        allegations,## [HP] Depending on needs, we can include the full commentary or just the count in the final payload, No need to feed this to LLM except the amount and type of commentary
            })
        except Exception as exc:
            logger.warning(f"⚠️ Failed processing mapping item: {exc}")

    logger.info(f"✅ {len(prior_cases)} prior case(s) found for Subject {subject_id}")

    return {
        "result": {
            "subject_id": subject_id,
            "first_name": first_name,
            "last_name": last_name,
            "dob": dob or None,
            "prior_cases": prior_cases,
            "prior_case_count": len(prior_cases),
        },
        "provenance": tracker.get_provenance_block()
    }