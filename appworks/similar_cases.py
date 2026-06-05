# appworks/similar_cases.py
# ----------------------------------------------------------------
# Agent 3: Similar Case Retrieval — Heuristic Ranking Engine
# ----------------------------------------------------------------

import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from appworks.appworks_utils import parse_aw_date, extract_workfolder_id_from_allegation
import config.settings as settings
from appworks.appworks_paths import AppWorksPaths

# ── NEW: Architecture Standard Imports ───────────────────────
from appworks.appworks_utils import safe_fetch, extract_id_from_href
from appworks.provenance import ProvenanceTracker

logger = logging.getLogger(__name__)


def _build_case_summary(wf_props: dict) -> str:
    """
    Combines the distinct AppWorks comment fields into a single summary
    context block for the LLM to reason over.
    """
    allegation_desc = (wf_props.get("WorkfolderAllegationsDescription") or "").strip()
    analyst_note    = (wf_props.get("WorkfolderAnalystComments") or "").strip()
    reviewer_note   = (wf_props.get("WorkfolderReviewerComments") or "").strip()

    parts = []
    if allegation_desc: parts.append(f"Allegation Details: {allegation_desc}")
    if analyst_note:    parts.append(f"Analyst Notes: {analyst_note}")
    if reviewer_note:   parts.append(f"Reviewer Notes: {reviewer_note}")

    if not parts:
        fallback = wf_props.get("WorkfolderDescription") or wf_props.get("Workfolder_CaseDescription") or ""
        if fallback: parts.append(f"General Description: {fallback}")

    return " | ".join(parts)

def _normalise_to_type_dicts(fraud_types: list) -> list:
    """Extracts target IDs and descriptions from the LLM's list of dicts."""
    result = []
    for ft in (fraud_types or []):
        if isinstance(ft, dict):
            type_id = str(ft.get("id") or ft.get("type_id", "")).strip()
            desc = ft.get("desc") or ft.get("description", "")
            if type_id: 
                result.append((type_id, desc))
    return result

def search_similar_cases(
    case_id: str,
    fraud_types: list,
    max_total_results: int = 3,
    **kwargs
) -> dict:
    
    max_per_type = settings.SIMILAR_CASES_MAX_PER_TYPE
    required_status = settings.SIMILAR_CASES_REQUIRED_STATUS.lower()
    
    logger.info(f"Starting similar case search | active_case: {case_id} | max_total: {max_total_results}")
    
    # Initialize the standardized ProvenanceTracker
    tracker = ProvenanceTracker("Workfolder", case_id)
    
    allegation_types = _normalise_to_type_dicts(fraud_types)
    candidates = []

    def _fetch_wf(row):
        wid = extract_workfolder_id_from_allegation(row)
        if not wid or str(wid) == str(case_id): 
            return None
            
        res_props, _ = safe_fetch(AppWorksPaths.Workfolder.item(wid), "Workfolder")
        if res_props:
            tracker.add_source("Workfolder", wid) # Dynamically log the source
            return wid, res_props
        return None
    
    for type_id, type_desc in allegation_types:
        #list_href = AppWorksPaths.Allegations.case_allegations_with_fileter(type_id, max_per_type, required_status)
        list_href = AppWorksPaths.Allegations.case_allegations_by_type_id(type_id)    
        # Track that we hit this specific allegation type list
        tracker.add_source("AllegationType", type_id)
        
        # Note: AppWorks list endpoints return the payload differently than single items, 
        # so we handle the unpack manually here instead of relying on safe_fetch's standard tuple
        try:
            from appworks.appworks_auth import fetch
            list_res = fetch(list_href)
            rows = list_res.get("_embedded", {}).get("Allegations_All", []) if list_res else []
        except Exception as e:
            logger.error(f"Failed to fetch allegations list for type {type_id}: {e}")
            rows = []

        type_candidates = []
        fetch_limit = max_per_type * 3 
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(_fetch_wf, rows[:fetch_limit]))
            
        for res in results:
            if not res: continue
            wid, props = res
            status = str(props.get("WorkfolderStatus") or props.get("Status") or "").strip().lower()
            dest = str(props.get("DESTINATION") or "").strip().lower()
            is_closed = (status == "closed" or "closed" in dest)
            
            if required_status == "closed" and not is_closed:
                continue
                
            date_received = props.get("WorkfolderDateReceived") or ""
            type_candidates.append({
                "wf_id": wid,
                "wf_props": props,
                "date_received": date_received
            })

        type_candidates.sort(key=lambda x: parse_aw_date(x["date_received"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        for cand in type_candidates[:max_per_type]:
            wf_props = cand["wf_props"]
            wid = cand["wf_id"]

            candidates.append({
                "case_id": wid,
                "complaint_no": str(wf_props.get("WorkfolderComplaintNumber")),
                "allegation_type": type_desc,
                "summary": _build_case_summary(wf_props),
                "date_received": cand["date_received"],
                "date_closed": wf_props.get("WorkfolderDateClosed") or "", 
                "fraud_amount": wf_props.get("WorkfolderFraudAmount"),
                "similarity_score": 1.0,
                "match_reasons": ["Recent closed case matching the requested fraud type."]
            })

    candidates.sort(
        key=lambda x: parse_aw_date(x["date_received"]) or datetime.min.replace(tzinfo=timezone.utc), 
        reverse=True
    )
    
    final_matches = candidates[:max_total_results]

    return {
        "result": {
            "matches": final_matches,
            "top_n_returned": len(final_matches),
            "total_candidates_scored": len(candidates)
        },
        # ── NEW: Dynamic Provenance Envelope ───────────────────────
        "provenance": tracker.get_provenance_block()
    }


def get_allegation_types(**kwargs) -> dict:
    logger.info("Fetching AppWorks Allegation Types catalog.")
    
    tracker = ProvenanceTracker("Catalog", "AllegationType")
    
    try:
        from appworks.appworks_auth import fetch
        raw = fetch(AppWorksPaths.Allegations.allegation_type_manage())
        items = raw if isinstance(raw, list) else raw.get("_embedded", {}).get("AllegationType_ManageAllegationType", [])
    except Exception as e:
        logger.error(f"Failed to fetch allegation types catalog: {e}")
        items = []

    seen_type_ids = set()
    allegation_types = []
    
    for item in items:
        type_props = item.get("Properties", {})
        href = item.get("_links", {}).get("item", {}).get("href", "")
        type_id = extract_id_from_href(href)
        
        if not type_id or type_id in seen_type_ids:
            continue
            
        seen_type_ids.add(type_id)
        tracker.add_source("AllegationType", type_id) # Log the specific types retrieved
        
        allegation_types.append({
            "type_id":      type_id,
            "short_code":   type_props.get("AllegationType_AllegationTypeShortDesc", ""),
            "description":  type_props.get("AllegationType_AllegationTypeDescription", ""),
            "default_text": type_props.get("AllegationType_AllegationTypeDefaults", ""),
        })

    return {
        "result": {
            "allegation_types": allegation_types,
            "total_types":      len(allegation_types),
        },
        "provenance": tracker.get_provenance_block()
    }