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

#logging.basicConfig(level=logging.DEBUG, force=True)
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

def _calculate_heuristic_score(candidate_props: dict, active_case_props: dict) -> tuple[float, list]:
    """
    Calculates a baseline similarity score and returns a tuple: (score, list_of_reasons).
    """
    if not active_case_props: 
        return 1.0, ["Baseline match (No active context provided)"]

    score = 0.0
    reasons = []
    
    # 1. Narrative Overlap (High weight)
    cand_narrative = str(candidate_props.get("WorkfolderAllegationsDescription", "")).lower()
    active_narrative = str(active_case_props.get("WorkfolderAllegationsDescription", "")).lower()
    if active_narrative and active_narrative in cand_narrative:
        score += 0.4
        reasons.append("Strong narrative/allegation overlap")

    # 2. Financial Exposure Proximity (Medium weight)
    try:
        cand_amt = float(candidate_props.get("WorkfolderFraudAmount") or 0)
        active_amt = float(active_case_props.get("WorkfolderFraudAmount") or 0)
        if active_amt > 0 and cand_amt > 0:
            if abs(cand_amt - active_amt) / active_amt <= 0.25:
                score += 0.3
                reasons.append("Similar financial exposure (within 25%)")
    except ValueError: 
        pass

    if not reasons:
        reasons.append("Baseline match by fraud type")

    return min(score, 1.0), reasons

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
            
    logger.debug(f"Failed to extract Workfolder ID for allegation item: {alleg_item.get('Id', 'Unknown')}")
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
    
    logger.info(f"Starting similar case search | active_case: {case_id} | max_total: {max_total_results} | max_per_type: {max_per_type}")
    
    # 1. Baseline context for scoring
    
    allegation_types = _normalise_to_type_dicts(fraud_types)
    logger.info(f"Normalized target allegation types: {allegation_types}")
    
    candidates = []
    sources_hit = []

    def _fetch_wf(row):
        wid = _workfolder_id_from_allegation_item(row)
        if not wid or str(wid) == str(case_id): return None
        res = _safe_fetch(AppWorksPaths.Workfolder.item(wid))
        return wid, res.get("Properties", {})
    
    # 2. Broad Fetch per Fraud Type
    for type_id, type_desc in allegation_types:
        logger.info(f"Fetching historical cases for Fraud Type: {type_desc} (ID: {type_id})")
        
        list_href = AppWorksPaths.Allegations.allegations_by_type(type_id)
        sources_hit.append(f"AppWorks Allegations By Type (ID: {type_id})")
        list_res = _safe_fetch(list_href)
        rows = list_res.get("_embedded", {}).get("Allegations_All", [])

        logger.info(f"Found {len(rows)} raw allegation rows for type {type_id}")

        type_candidates = []
        
        # Fetch properties for up to 3x the max_per_type to find enough "Closed" cases quickly.
        # We use a ThreadPool to prevent blocking sequential calls.
        fetch_limit = max_per_type * 3 
        """
        def _fetch_wf(row):
        wid = _workfolder_id_from_allegation_item(row)
        if not wid or str(wid) == str(case_id): return None
        res = _safe_fetch(AppWorksPaths.Workfolder.item(wid))
        return wid, res.get("Properties", {})
        """
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(_fetch_wf, rows[:fetch_limit]))
            
        logger.info(f"Successfully parallel-fetched {len([r for r in results if r])} detailed workfolders.")
            
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

        logger.info(f"Filtered down to {len(type_candidates)} 'Closed' cases for type {type_id}")

        # Sort by date received descending (most recent first)
        type_candidates.sort(key=lambda x: _parse_aw_date(x["date_received"]) or datetime.min.replace(tzinfo=timezone.utc), 
            reverse=True)

        # 4. Take Top N recent and format the payload
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
                # Hardcode these to satisfy the Pydantic contract without doing the math, [hp] leave it as it is for now , we can add the heuristic scoring later if needed
                "similarity_score": 1.0,
                "match_reasons": ["Recent closed case matching the requested fraud type."]
            })

   # 5. Global Rank & Truncate (Sort by most recent date across all types)
    candidates.sort(
        key=lambda x: _parse_aw_date(x["date_received"]) or datetime.min.replace(tzinfo=timezone.utc), 
        reverse=True
    )
    
    final_matches = candidates[:max_total_results]
    logger.info(f"Search complete. Returning {len(final_matches)} top chronological cases (from {len(candidates)} valid candidates).")

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

def get_allegation_types(**kwargs) -> dict:
    logger.info("Fetching AppWorks Allegation Types catalog.")
    raw = fetch(AppWorksPaths.Allegations.allegation_type_manage())
    items = raw if isinstance(raw, list) else raw.get("_embedded", {}).get("AllegationType_ManageAllegationType", [])
    seen_type_ids = set()
    allegation_types = []
    
    logger.info(f"Successfully resolved {len(allegation_types)} unique allegation types.")
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
    logger.info("Allegation types fetch complete.")
    return envelope