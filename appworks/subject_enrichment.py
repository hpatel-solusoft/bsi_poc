"""
Subject Enrichment Services
---------------------------
Data functions for the Context Enrichment Agent (Agent 2).
Manifest tool: fetch_subject_history
"""

import logging
from typing import Dict, List, Optional, Any

from appworks.appworks_paths import AppWorksPaths
from appworks.appworks_utils import safe_fetch, extract_id_from_href, get_relationship_items
from appworks.provenance import ProvenanceTracker

logger = logging.getLogger(__name__)


def _extract_workfolder_core_props(wf_props: Dict) -> Dict:
    """
    Step 1: Extracts core properties and extended text fields from a Workfolder properties dict.
    """
    return {
        "complaint_no":                      wf_props.get("WorkfolderComplaintNumber"),
        "status":                            wf_props.get("WorkfolderStatus"),
        "description":                       wf_props.get("WorkfolderDescription"),
        "case_description":                  wf_props.get("Workfolder_CaseDescription"),
        "date_received":                     wf_props.get("WorkfolderDateReceived"),
        "date_reported":                     wf_props.get("WorkfolderDateReported"),
        "allegation":                        wf_props.get("WorkFolderAllegation"),
        "team":                              wf_props.get("TEAM_DISPLAY_NAME"),
        "destination":                       wf_props.get("DESTINATION"),
        "workfolder_allegations_description": wf_props.get("WorkfolderAllegationsDescription"),
        "workfolder_reviewer_comments":       wf_props.get("WorkfolderReviewerComments"),
        "workfolder_analyst_comments":        wf_props.get("WorkfolderAnalystComments"),
    }


def _fetch_commentary_type(type_href: str, tracker: ProvenanceTracker) -> Optional[str]:
    """
    Fetches a CommentaryType entity and returns its type name.
    """
    if not type_href:
        return None
    props, _ = safe_fetch(type_href, "CommentaryType")
    
    if props:
        tracker.add_source("CommentaryType", extract_id_from_href(type_href))
        return props.get("Type")
    return None


def _fetch_workfolder_commentary(wf_id: str, tracker: ProvenanceTracker) -> List[Dict]:
    """
    Step 2: Fetches commentary entries for a Workfolder and returns a list of
    {commentary_text, commentary_type} dicts.
    """
    commentary_href = AppWorksPaths.Workfolder.commentary(wf_id)
    logger.info(f"💬 Fetching commentary for Workfolder: {wf_id}")
    
    items = get_relationship_items(commentary_href, "Workfolder_WorkfolderCommentaryNewRelationship")
    commentary_list = []
    
    for item in items:
        try:
            embedded_props = item.get("Properties", {})
            comment_text = embedded_props.get("WorkfolderCommentary_Comment")

            self_href = item.get("_links", {}).get("self", {}).get("href", "")
            commentary_type = None
            
            if self_href:
                comment_id = extract_id_from_href(self_href)
                tracker.add_source("WorkfolderCommentary", comment_id)
                
                _, item_links = safe_fetch(self_href, "WorkfolderCommentary")
                type_href = item_links.get("relationship:WorkfolderCommentary_CommentaryTypeRelationship", {}).get("href", "")
                
                if type_href:
                    commentary_type = _fetch_commentary_type(type_href, tracker)

            commentary_list.append({
                "commentary_text": comment_text,
                "commentary_type": commentary_type,
            })
        except Exception as exc:
            logger.warning(f"⚠️ Failed processing commentary item for Workfolder {wf_id}: {exc}")

    logger.info(f"  → {len(commentary_list)} commentary item(s) found for Workfolder {wf_id}")
    return commentary_list


def _fetch_workfolder_allegations(wf_id: str, tracker: ProvenanceTracker) -> List[Dict]:
    """
    Step 3: Fetches full allegation details for a prior-case Workfolder.
    Mirrors the allegation-fetching logic in case_intake.
    """
    allegations_href = AppWorksPaths.Workfolder.allegations(wf_id)
    logger.info(f"⚖️ Fetching allegations for Workfolder: {wf_id}")
    
    items = get_relationship_items(allegations_href, "Workfolder_AllegationsRelationship")
    logger.info(f"  → {len(items)} allegation(s) found for Workfolder {wf_id}")
    
    allegations_list = []

    for alleg_item in items:
        try:
            alleg_self_href = alleg_item.get("_links", {}).get("self", {}).get("href", "")
            
            # 1. Fetch full Allegation record
            alleg_props, alleg_links = safe_fetch(alleg_self_href, "Allegation")
            if alleg_props:
                tracker.add_source("Allegation", extract_id_from_href(alleg_self_href))

            # 2. Fetch Allegation Type
            alleg_type_href = alleg_item.get("_links", {}).get("relationship:Allegations_AllegationsType", {}).get("href", "")
            if not alleg_type_href:
                alleg_type_href = alleg_links.get("relationship:Allegations_AllegationsType", {}).get("href", "")

            alleg_type_props, _ = safe_fetch(alleg_type_href, "AllegationType")
            if alleg_type_props:
                tracker.add_source("AllegationType", extract_id_from_href(alleg_type_href))

            # 3. Fetch Source Agency
            agency_href = alleg_links.get("relationship:Allegations_Source", {}).get("href", "")
            agency_props, _ = safe_fetch(agency_href, "Agency")
            if agency_props:
                tracker.add_source("Agency", extract_id_from_href(agency_href))

            # Resolve description
            allegation_description = (
                alleg_type_props.get("AllegationType_AllegationTypeDescription")
                or alleg_type_props.get("AllegationType_AllegationTypeShortDesc")
                or alleg_type_props.get("AllegationType_AllegationTypeDefaults")
                or alleg_props.get("Allegations_AllegationType")
                or alleg_props.get("Allegations_Comment")
                or f"Unknown allegation type {extract_id_from_href(alleg_type_href) or 'unknown'}"
            )

            allegations_list.append({
                "status":                  alleg_props.get("Allegations_Status"),
                "allegation_status":       alleg_props.get("Allegations_AllegationStatus"),
                "date_received":           alleg_props.get("Allegations_DateReceived"),
                "date_reported":           alleg_props.get("Allegations_DateReported"),
                "date_closed":             alleg_props.get("Allegations_DateClosed"),
                "closure_date_reported":   alleg_props.get("Allegations_ClosureDateReported"),
                "close_comment":           alleg_props.get("Allegations_AllegationCloseComment"),
                "comment":                 alleg_props.get("Allegations_Comment"),
                "agency_referral_no":      alleg_props.get("Allegations_AgencyReferralNumber"),
                "is_intake":               alleg_props.get("Allegations_IsIntakeAllegation"),
                "disposition_norris_code": alleg_props.get("Allegations_DispositionNorrisCode"),
                "dta_closure_report":      alleg_props.get("Allegations_DTAClosureReport"),
                "allegation_type": {
                    "id":          extract_id_from_href(alleg_type_href),
                    "description": allegation_description,
                    "short_desc":  alleg_type_props.get("AllegationType_AllegationTypeShortDesc"),
                    "defaults":    alleg_type_props.get("AllegationType_AllegationTypeDefaults"),
                },
                "source_agency": {
                    "name":              agency_props.get("Agency_AgencyName"),
                    "short_description": agency_props.get("Agency_AgencyShortDescription"),
                },
            })
        except Exception as exc:
            logger.warning(f"⚠️ Failed processing allegation for Workfolder {wf_id}: {exc}")

    return allegations_list


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

            core_props = _extract_workfolder_core_props(wf_props)
            commentary = _fetch_workfolder_commentary(wf_id, tracker)
            allegations = _fetch_workfolder_allegations(wf_id, tracker)

            prior_cases.append({
                "workfolder_id":      wf_id,
                "is_primary_subject": is_primary,
                "mapping_title":      title_text,
                **core_props,
                "commentary":         commentary,
                "allegations":        allegations,
            })
        except Exception as exc:
            logger.warning(f"⚠️ Failed processing mapping item: {exc}")

    logger.info(f"✅ {len(prior_cases)} prior case(s) found for Subject {subject_id}")

    return {
        "result": {
            "subject_id": subject_id,
            "first_name": first_name,
            "last_name": last_name,
            "dob": dob,
            "prior_cases": prior_cases,
            "prior_case_count": len(prior_cases),
        },
        "provenance": tracker.get_provenance_block()
    }