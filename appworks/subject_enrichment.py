"""
Subject Enrichment Services
---------------------------
Data functions for the Context Enrichment Agent (Agent 2).
Manifest tool: fetch_subject_history
"""

import logging
from typing import Dict, Optional

from appworks.appworks_auth import AppworksSessionExpiredError
from appworks.appworks_paths import AppWorksPaths
from appworks.appworks_utils import (
    embedded,
    embedded_id,
    get_relationship_items,
    safe_fetch,
)
from appworks.entity_mappers import map_commentary, map_workfolder_core
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

    prior_cases includes only rows where this subject was the PRIMARY
    subject on that other case — a "prior case" means this subject was
    themselves under investigation there, not merely linked to it as a
    co-subject (e.g. a PCA, employer contact, or anyone else incidentally
    connected to a case actually about someone else). A co-subject
    appearance is excluded before the per-case Workfolder/commentary fetch
    below, so it costs nothing beyond the one row the Subjects query
    already returned.
    """
    logger.info("🚀 [LIVE] Context Enrichment for Subject ID: %s", subject_id)

    tracker = ProvenanceTracker("Subject", subject_id)

    href = AppWorksPaths.Subjects.by_subject(subject_id)
    rows = get_relationship_items(href, "All_Subjects")

    logger.info("📋 Found %d case row(s) for Subject %s", len(rows), subject_id)

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
                logger.info("  Skipping current case %s from prior case history", wf_id)
                continue

            # Prior Cases means "this subject was themselves under
            # investigation," not "this subject was mentioned on a case" —
            # a co-subject appearance (e.g. a PCA, employer contact, or
            # anyone else incidentally linked to another person's case) is
            # not that subject's own case history. Without this filter, any
            # case that adds this subject as a secondary/co-subject — even
            # one entirely unrelated to them, correctly entered or not —
            # would silently inflate their Prior Cases count. Skipped here,
            # before the Workfolder/commentary fetch below, so a filtered
            # row also costs nothing beyond the one row already returned by
            # the Subjects query.
            if not is_primary:
                logger.info(
                    "  Skipping case %s from prior case history — subject %s is a "
                    "co-subject there, not the primary subject",
                    wf_id,
                    subject_id,
                )
                continue

            tracker.add_source("Workfolder", wf_id)

            # Fetch linked Workfolder summary
            logger.info("📂 Fetching linked Workfolder: %s", wf_id)
            wf_props, _ = safe_fetch(AppWorksPaths.Workfolder.item(wf_id), "Workfolder")

            core_props = map_workfolder_core(wf_props)
            commentary = map_commentary(wf_id, tracker)

            prior_cases.append(
                {
                    "workfolder_id": wf_id,
                    "is_primary_subject": is_primary,
                    **core_props,
                    "commentary": commentary["items"],
                    "commentary_count": commentary["count"],
                    # Case-level field AppWorks repeats on every commentary row —
                    # map_commentary already dedupes it to a single value here.
                    "allegation_description": commentary["allegation_description"],
                }
            )
        except AppworksSessionExpiredError:
            # Same reasoning as appworks_utils.safe_fetch: a caller-token
            # 401 must not look like "this subject's prior-case row simply
            # had a processing error" — every other AppWorks call in this
            # enrichment carries the same rejected token.
            raise
        except Exception as exc:
            logger.warning("⚠️ Failed processing Subjects row: %s", exc)

    logger.info("✅ %d prior case(s) found for Subject %s", len(prior_cases), subject_id)

    return {
        "result": {
            "subject_id": subject_id,
            "first_name": first_name,
            "last_name": last_name,
            "dob": dob or None,
            "prior_cases": prior_cases,
            "prior_case_count": len(prior_cases),
        },
        "provenance": tracker.get_provenance_block(),
    }
