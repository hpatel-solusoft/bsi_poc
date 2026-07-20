"""
Subject Enrichment Services
---------------------------
Data functions for the Context Enrichment Agent (Agent 2).
Manifest tool: fetch_subject_history
"""

import logging
from typing import Dict, List, Optional, Any
from appworks.entity_mappers import map_commentary, map_workfolder_core
from appworks.appworks_paths import AppWorksPaths
from appworks.appworks_utils import safe_fetch, get_relationship_items, embedded, embedded_id
from utils.provenance import ProvenanceTracker

logger = logging.getLogger(__name__)

def get_enriched_subject_profile(subject_id: str, case_id: Optional[str] = None) -> Dict:
    """
    Fetches deep subject history and prior cases for a given subject_id.

    Single call: AppWorksPaths.Subjects.by_subject(subject_id) is the same
    Subjects/lists/All_Subjects endpoint case_intake uses (by_workfolder),
    just filtered by Subjects_Subject$Identity.Id instead of
    Subjects_Workfolder$Identity.Id — so it returns one row per case this
    subject appears on, each row already embedding the Subject detail
    Properties, the case's own Workfolder id, and IsPrimarySubject. Replaces
    the old Subject.item() + workfolder_mappings() + workfolder_mapping_item()
    3-call chase (the last of which existed only because childEntities list
    rows carry a bare 'self' href, not the parent Workfolder relationship).
    """
    logger.info(f"🚀 [LIVE] Context Enrichment for Subject ID: {subject_id}")

    tracker = ProvenanceTracker("Subject", subject_id)

    href = AppWorksPaths.Subjects.by_subject(subject_id)
    rows = get_relationship_items(href, "All_Subjects")

    logger.info(f"📋 Found {len(rows)} case row(s) for Subject {subject_id}")

    first_name = ""
    last_name = ""
    dob = None
    prior_cases = []

    for row in rows:
        try:
            detail_props = embedded(row, "Subjects_Subject")
            is_primary = row.get("Properties", {}).get("Subjects_IsPrimarySubject", False)
            wf_id = embedded_id(row, "Subjects_Workfolder")

            # Subject_FirstName/LastName/DOB are identical across every row
            # for the same subject_id — take them once, from the first row
            # that has them.
            if not first_name:
                first_name = detail_props.get("Subject_FirstName", "") or ""
            if not last_name:
                last_name = detail_props.get("Subject_LastName", "") or ""
            if dob is None:
                dob = detail_props.get("Subject_DOB")

            if not wf_id:
                logger.warning("⚠️ Subjects row with no resolvable Subjects_Workfolder id skipped")
                continue

            # Exclude current case
            if case_id and str(wf_id) == str(case_id):
                logger.info(f"  Skipping current case {wf_id} from prior case history")
                continue

            tracker.add_source("Workfolder", wf_id)

            # Fetch linked Workfolder summary
            logger.info(f"📂 Fetching linked Workfolder: {wf_id}")
            wf_props, _ = safe_fetch(AppWorksPaths.Workfolder.item(wf_id), "Workfolder")

            core_props = map_workfolder_core(wf_props)
            commentary = map_commentary(wf_id, tracker)

            prior_cases.append({
                "workfolder_id":      wf_id,
                "is_primary_subject": is_primary,
                **core_props,
                "commentary":            commentary["items"],
                "commentary_count":      commentary["count"],
                # Case-level field AppWorks repeats on every commentary row —
                # map_commentary already dedupes it to a single value here.
                "allegation_description": commentary["allegation_description"],
            })
        except Exception as exc:
            logger.warning(f"⚠️ Failed processing Subjects row: {exc}")

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