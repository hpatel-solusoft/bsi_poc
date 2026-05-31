# appworks/similar_cases.py
# ----------------------------------------------------------------
# Agent 3: Similar Case Retrieval — Heuristic Ranking Engine
# ----------------------------------------------------------------

import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import config.settings as settings
from appworks.appworks_auth import fetch
from appworks.appworks_paths import AppWorksPaths

logger = logging.getLogger(__name__)

def _extract_id(href: str) -> str:
    return href.rstrip("/").split("/")[-1] if href else ""

def _safe_fetch(href: str) -> dict:
    try:
        return fetch(href)
    except Exception as exc:
        logger.warning(f"Fetch failed [{href}]: {exc}")
        return {}

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

    # Fallback to general description if all specific notes are empty
    if not parts:
        fallback = wf_props.get("WorkfolderDescription") or wf_props.get("Workfolder_CaseDescription") or ""
        if fallback: parts.append(f"General Description: {fallback}")

    return " | ".join(parts)

def _calculate_heuristic_score(candidate_props: dict, active_case_props: dict) -> float:
    """
    Calculates a baseline similarity score so the Python tool can truncate 
    the pool before handing the Top N to the LLM for final reasoning.
    """
    if not active_case_props: 
        return 1.0 

    score = 0.0
    
    # 1. Narrative Overlap (High weight)
    cand_narrative = str(candidate_props.get("WorkFolderAllegation", "")).lower()
    active_narrative = str(active_case_props.get("WorkFolderAllegation", "")).lower()
    if active_narrative and active_narrative in cand_narrative:
        score += 0.4

    # 2. Financial Exposure Proximity (Medium weight)
    try:
        cand_amt = float(candidate_props.get("WorkfolderFraudAmount") or 0)
        active_amt = float(active_case_props.get("WorkfolderFraudAmount") or 0)
        if active_amt > 0 and cand_amt > 0:
            if abs(cand_amt - active_amt) / active_amt <= 0.25:
                score += 0.3
    except ValueError: 
        pass

    # 3. Source/Destination Routing (Low weight)
    if candidate_props.get("DESTINATION") == active_case_props.get("DESTINATION"):
        score += 0.15
    if candidate_props.get("WorkfolderSource") == active_case_props.get("WorkfolderSource"):
        score += 0.15

    return min(score, 1.0)


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

def _workfolder_id_from_allegation_item(alleg_item: dict) -> str:
    """Extract parent workfolder ID from Allegations list row (no extra fetch)."""
    props = alleg_item.get("Properties", {})
    links = alleg_item.get("_links", {})
    for key in (
        "Allegations_Workfolder$Identity",
        "Allegations_Workfolder",
        "Workfolder$Identity",
        "Workfolder",
    ):
        raw = props.get(key)
        if isinstance(raw, dict):
            raw_id = raw.get("Id") or raw.get("id")
            if raw_id:
                return str(raw_id).strip()
        elif raw:
            return str(raw).strip()
    for key in ("relationship:Allegations_Workfolder", "relationship:Workfolder"):
        href = links.get(key, {}).get("href", "")
        if href:
            return _extract_id(href)

    # NEW: If still not found, fetch the individual Allegation item. 
    # List rows often have limited projections.
    item_href = links.get("item", {}).get("href", "") or links.get("self", {}).get("href", "")
    if item_href:
        alleg_res = _safe_fetch(item_href)
        # Check properties of the fetched item
        props_full = alleg_res.get("Properties", {})
        links_full = alleg_res.get("_links", {})
        for key in ("Allegations_Workfolder$Identity", "Workfolder$Identity", "Allegations_Workfolder", "Workfolder"):
            raw = props_full.get(key)
            if isinstance(raw, dict):
                raw_id = raw.get("Id") or raw.get("id")
                if raw_id: return str(raw_id).strip()
            elif raw:
                return str(raw).strip()
        # Check links of the fetched item
        for key in ("relationship:Allegations_Workfolder", "relationship:Workfolder"):
            h = links_full.get(key, {}).get("href", "")
            if h: return _extract_id(h)
            
    return ""

def _parse_aw_date(raw: str):
    """
    Parse AppWorks date safely and ALWAYS return timezone-aware UTC datetime.
    Fixes: offset-naive vs offset-aware comparison error.
    """
    if not raw:
        return None

    s = str(raw).strip()

    # Normalize Z → UTC offset
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None

    # 🔴 CRITICAL FIX: force timezone awareness
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)

def search_similar_cases(
    case_id: str,
    fraud_types: list,
    max_total_results: int = 3,  # Manifest default
    **kwargs  # Absorbs legacy parameters to prevent TypeErrors
) -> dict:
    
    max_per_type = settings.SIMILAR_CASES_MAX_PER_TYPE
    required_status = settings.SIMILAR_CASES_REQUIRED_STATUS.lower()
    
    # 1. Baseline context for scoring
    active_case_props = {}
    if case_id:
        active_res = _safe_fetch(AppWorksPaths.Workfolder.item(case_id))
        active_case_props = active_res.get("Properties", {})

    allegation_types = _normalise_to_type_dicts(fraud_types)
    candidates = []
    sources_hit = []

    def _fetch_wf(row):
        wid = _workfolder_id_from_allegation_item(row)
        if not wid or str(wid) == str(case_id): return None
        res = _safe_fetch(AppWorksPaths.Workfolder.item(wid))
        return wid, res.get("Properties", {})
    
    # 2. Broad Fetch per Fraud Type
    for type_id, type_desc in allegation_types:
        list_href = AppWorksPaths.Allegations.allegations_by_type(type_id)
        sources_hit.append(f"AppWorks Allegations By Type (ID: {type_id})")
        list_res = _safe_fetch(list_href)
        rows = list_res.get("_embedded", {}).get("Allegations_All", [])

        type_candidates = []
        
        # Fetch properties for up to 3x the max_per_type to find enough "Closed" cases quickly.
        # We use a ThreadPool to prevent blocking sequential calls.
        fetch_limit = max_per_type * 3 
        
        def _fetch_wf(row):
            wid = _workfolder_id_from_allegation_item(row)
            if not wid or str(wid) == str(case_id): return None
            res = _safe_fetch(AppWorksPaths.Workfolder.item(wid))
            return wid, res.get("Properties", {})
            
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(_fetch_wf, rows[:fetch_limit]))
            
        # 3. Filter for 'Closed' and map dates
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
                "date_received":date_received
            })

        # Sort by date received descending (most recent first)
        type_candidates.sort(key=lambda x: _parse_aw_date(x["date_received"]) or datetime.min.replace(tzinfo=timezone.utc), 
            reverse=True)

        # 4. Take Top N recent, fetch financials, and calculate heuristic score
        for cand in type_candidates[:max_per_type]:
            wf_props = cand["wf_props"]
            
            sim_score = _calculate_heuristic_score(wf_props, active_case_props)

            candidates.append({
                "complaint_no": wf_props.get("WorkfolderComplaintNumber"),
                "allegation_type": type_desc,
                "summary": _build_case_summary(wf_props),
                "date_received": cand["date_received"],
                "date_closed": wf_props.get("WorkfolderDateClosed") or "", 
                "fraud_amount": wf_props.get("WorkfolderFraudAmount"),
                "similarity_score": round(sim_score, 2)
            })

    # 5. Global Rank & Truncate
    # Sort ALL collected candidates primarily by the heuristic similarity score (descending)
    candidates.sort(key=lambda x: x["similarity_score"], reverse=True)
    final_matches = candidates[:max_total_results]

    # 6. Build the Standard Envelope
    return {
        "result": {
            "matches": final_matches,
            "top_n_returned": len(final_matches),
            "total_candidates_scored": len(candidates)
        },
        "provenance": {
            "sources": sources_hit,
            "computed_by": "AppWorks REST API",
            "active_case_context": case_id
        }
    }

def get_allegation_types() -> dict:
    raw = fetch(AppWorksPaths.Allegations.allegation_type_manage())
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
            "sources":      [AppWorksPaths.Allegations.allegation_type_manage()],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by":  "get_allegation_types",
        }
    }
    return envelope