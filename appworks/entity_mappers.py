"""
Entity Mappers
--------------
Shared domain parsers to convert OpenText AppWorks REST payloads 
into canonical BSI semantic dictionaries. Ensures DRY compliance across agents.
"""

import logging
from typing import Dict, List, Any

from appworks.appworks_utils import safe_fetch, extract_id_from_href, get_relationship_items
from appworks.provenance import ProvenanceTracker

logger = logging.getLogger(__name__)

def map_workfolder_core(wf_props: Dict[str, Any]) -> Dict[str, Any]:
    """Standardizes extraction of core workfolder properties."""
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

def map_commentary(rel_href: str, tracker: ProvenanceTracker) -> List[Dict]:
    """Shared mapper to fetch and normalize Workfolder Commentary."""
    logger.info("💬 Fetching commentary...")
    commentary_list = []
    if not rel_href:
        return commentary_list
        
    items = get_relationship_items(rel_href, "Workfolder_WorkfolderCommentaryNewRelationship")
    
    for item in items:
        try:
            embedded_props = item.get("Properties", {})
            comment_text = embedded_props.get("WorkfolderCommentary_Comment")
            self_href = item.get("_links", {}).get("self", {}).get("href", "")
            commentary_type = None
            
            if self_href:
                tracker.add_source("WorkfolderCommentary", extract_id_from_href(self_href))
                _, item_links = safe_fetch(self_href, "WorkfolderCommentary")
                type_href = item_links.get("relationship:WorkfolderCommentary_CommentaryTypeRelationship", {}).get("href", "")
                
                if type_href:
                    type_props, _ = safe_fetch(type_href, "CommentaryType")
                    if type_props:
                        tracker.add_source("CommentaryType", extract_id_from_href(type_href))
                        commentary_type = type_props.get("Type")

            commentary_list.append({
                "commentary_text": comment_text,
                "commentary_type": commentary_type,
            })
        except Exception as exc:
            logger.warning(f"⚠️ Failed processing commentary item: {exc}")

    return commentary_list

def map_allegations(rel_href: str, tracker: ProvenanceTracker) -> List[Dict]:
    """Shared mapper to fetch and normalize allegations."""
    logger.info("📋 Fetching allegations...")
    allegations_list = []
    if not rel_href:
        return allegations_list

    items = get_relationship_items(rel_href, "Workfolder_AllegationsRelationship")

    for item in items:
        try:
            alleg_self_href = item.get("_links", {}).get("self", {}).get("href", "")
            
            alleg_props, alleg_links = safe_fetch(alleg_self_href, "Allegation")
            if alleg_props:
                tracker.add_source("Allegation", extract_id_from_href(alleg_self_href))

            alleg_type_href = item.get("_links", {}).get("relationship:Allegations_AllegationsType", {}).get("href", "")
            if not alleg_type_href:
                alleg_type_href = alleg_links.get("relationship:Allegations_AllegationsType", {}).get("href", "")

            type_props, _ = safe_fetch(alleg_type_href, "AllegationType")
            if type_props:
                tracker.add_source("AllegationType", extract_id_from_href(alleg_type_href))

            agency_href = alleg_links.get("relationship:Allegations_Source", {}).get("href", "")
            agency_props, _ = safe_fetch(agency_href, "Agency")
            if agency_props:
                tracker.add_source("Agency", extract_id_from_href(agency_href))

            allegation_description = (
                type_props.get("AllegationType_AllegationTypeDescription")
                or type_props.get("AllegationType_AllegationTypeShortDesc")
                or type_props.get("AllegationType_AllegationTypeDefaults")
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
                    "short_desc":  type_props.get("AllegationType_AllegationTypeShortDesc"),
                    "defaults":    type_props.get("AllegationType_AllegationTypeDefaults"),
                },
                "source_agency": {
                    "name":              agency_props.get("Agency_AgencyName"),
                    "short_description": agency_props.get("Agency_AgencyShortDescription"),
                },
            })
        except Exception as exc:
            logger.error(f"⚠️ Error mapping individual allegation: {exc}")

    return allegations_list