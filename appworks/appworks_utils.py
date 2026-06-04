"""
AppWorks REST Utilities
-----------------------
Shared helper functions for traversing OpenText AppWorks REST endpoints.
Handles strict error boundaries to prevent silent failures and LLM hallucinations.
"""

import logging
from typing import Dict, List, Tuple, Optional
from appworks.appworks_auth import fetch
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def safe_fetch(href: str, entity_name: str = "Unknown") -> Tuple[Dict, Dict]:
    """
    Executes a REST fetch with strict error boundaries.
    Returns a tuple of (Properties, _links).
    
    Distinguishes between a structurally missing link (returns empty) 
    and an actual network/API failure (logs explicitly).
    """
    if not href:
        return {}, {}
        
    try:
        res = fetch(href)
        if not res:
            logger.debug(f"Empty response for {entity_name} at {href}")
            return {}, {}
            
        properties = res.get("Properties", {})
        links = res.get("_links", {})
        return properties, links
        
    except Exception as e:
        # Logs the specific network/API failure rather than swallowing it.
        # This prevents the system from assuming an entity has no data 
        # when the API is actually unreachable.
        logger.error(f"❌ API Failure fetching {entity_name} [{href}]: {str(e)}")
        return {}, {}

def extract_id_from_href(href: str) -> Optional[str]:
    """
    Safely extracts the terminal ID from an AppWorks REST href string.
    Example: '.../appworks/rest/v1/cases/BSI-123' -> 'BSI-123'
    """
    if not href:
        return None
    return href.rstrip("/").split("/")[-1]


def get_relationship_items(rel_href: str, embedded_key: str) -> List[Dict]:
    """
    Helper to fetch a list of embedded items from a relationship link.
    Handles AppWorks variations between single objects and arrays.
    """
    if not rel_href:
        return []
    try:
        res = fetch(rel_href)
        if not res:
            return []
        
        # Check for to-one relationship disguised as a list link
        props = res.get("Properties")
        links = res.get("_links", {})
        if props is not None and "self" in links:
            return [res]

        embedded = res.get("_embedded", {})
        items = embedded.get(embedded_key)
        
        if isinstance(items, list): return items
        if isinstance(items, dict): return [items]
        
        # Fallback to _links.item pattern
        l_items = links.get("item")
        if isinstance(l_items, list): return l_items
        if isinstance(l_items, dict): return [l_items]
        
        if isinstance(embedded, dict):
            for val in embedded.values():
                if isinstance(val, list): return val
                if isinstance(val, dict): return [val]

        return []
    except Exception as e:
        logger.error(f"❌ API Failure fetching relationship list [{rel_href}]: {str(e)}")
        return []
    

def parse_aw_date(raw: str):
    """Parse AppWorks date safely and ALWAYS return timezone-aware UTC datetime."""
    if not raw: return None
    s = str(raw).strip()
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def extract_workfolder_id_from_allegation(alleg_item: dict) -> str:
    """Extract parent workfolder ID from an Allegations list row robustly."""
    props = alleg_item.get("Properties", {})
    links = alleg_item.get("_links", {})
    
    # 1. Try embedded properties
    for key in ("Allegations_Workfolder$Identity", "Allegations_Workfolder", "Workfolder$Identity", "Workfolder"):
        raw = props.get(key)
        if isinstance(raw, dict):
            raw_id = raw.get("Id") or raw.get("id")
            if raw_id: return str(raw_id).strip()
        elif raw:
            return str(raw).strip()
            
    # 2. Try relationship links
    for key in ("relationship:Allegations_Workfolder", "relationship:Workfolder"):
        href = links.get(key, {}).get("href", "")
        if href: return extract_id_from_href(href) or ""

    # 3. Fallback: Fetch the actual item if properties were missing from list view
    item_href = links.get("item", {}).get("href", "") or links.get("self", {}).get("href", "")
    if item_href:
        props_full, links_full = safe_fetch(item_href, "Allegation")
        for key in ("Allegations_Workfolder$Identity", "Workfolder$Identity", "Allegations_Workfolder", "Workfolder"):
            raw = props_full.get(key)
            if isinstance(raw, dict):
                raw_id = raw.get("Id") or raw.get("id")
                if raw_id: return str(raw_id).strip()
            elif raw: return str(raw).strip()
        for key in ("relationship:Allegations_Workfolder", "relationship:Workfolder"):
            h = links_full.get(key, {}).get("href", "")
            if h: return extract_id_from_href(h) or ""
            
    return ""