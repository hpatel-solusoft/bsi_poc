# ----------------------------------------------------------------
# Agent 3: Similar Case Retrieval — Broad Fetch + Manifest Filtering
# ----------------------------------------------------------------
#
# Strategy:
#   Stage A (broad candidate pool — with early-exit guard):
#     - Resolve AllegationType IDs from current case allegations
#     - Fetch Allegations_All rows per type ID (list endpoint)
#     - Read wf_id + allegation status from list-row Properties
#       (no extra per-row API call needed for status)
#     - EARLY-EXIT per type once (allocated quota * OVERFETCH_FACTOR)
#       unique workfolders have been accumulated — avoids scanning hundreds
#       of rows when only a handful of results are needed
#     - Fetch Workfolder + FinancialRelationship only for kept candidates
#
#   Stage B (manifest filters):
#     - required_status  — matched against allegation status from list row
#     - similarity_lookback_years
#     - max_total_results split across fraud types
#
# All filter parameters are read from manifest at runtime — no hardcoding.
# ----------------------------------------------------------------

import logging
import re
import yaml
import os
import json 
from typing import List, Optional, Any  
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from appworks.appworks_auth import fetch, fetch_list
from semantic_layer.entity_contracts import SimilarCasesResult
from appworks.appworks_paths import AppWorksPaths
logger = logging.getLogger(__name__)

_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "../../config/manifest.yaml")
# How many candidates to collect per type before stopping the list scan.
# e.g. max_per_type=3, factor=5 → stop after 15 unique workfolders per type.
_OVERFETCH_FACTOR = 5
_DEFAULT_TOTAL_RESULTS_LIMIT = 3

# Outcome string templates for similar-case candidates.
# Defined here so wording has a single edit point (Issue #12).
# Use .format(type_id=...) when building each candidate dict.
_OUTCOME_ALLEGATION_MATCH = "Findings: Allegation type match — type_id={type_id}"
_OUTCOME_SUBJECT_TRAVERSAL = "Findings: Subject history traversal — type_id={type_id}"


# ── Config ────────────────────────────────────────────────────────

def _load_similar_cases_config() -> dict:
    try:
        with open(_MANIFEST_PATH) as f:
            manifest = yaml.safe_load(f)
        for tool in manifest.get("tools", []):
            if tool.get("name") == "search_similar_cases":
                return tool.get("config", {})
    except Exception as exc:
        logger.warning(f"Could not load manifest config: {exc}")
    return {}


# ── Low-level helpers ─────────────────────────────────────────────

def _extract_id(href: str) -> str:
    if href:
        return href.rstrip("/").split("/")[-1]
    return ""


def _safe_fetch(href: str) -> dict:
    try:
        return fetch(href)
    except Exception as exc:
        logger.warning(f"fetch failed [{href}]: {exc}")
        return {}


def _rel_href(links: dict, key: str) -> str:
    for k in (f"relationship:{key}", key):
        v = links.get(k)
        if isinstance(v, dict):
            h = v.get("href", "")
            if h:
                return h
    return ""


def _fetch_embedded(href: str, key: str) -> list:
    if not href:
        return []
    res = _safe_fetch(href)
    return res.get("_embedded", {}).get(key, [])


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


def _allegation_id_from_item(alleg_item: dict) -> str:
    """Extract allegation ID from an Allegations list row using multiple fallback patterns."""
    if not isinstance(alleg_item, dict):
        return ""
    links = alleg_item.get("_links", {})
    href = links.get("self", {}).get("href", "")
    if href:
        alleg_id = _extract_id(href)
        if alleg_id:
            return alleg_id
    props = alleg_item.get("Properties", {})
    for key in (
        "Allegations_Id",
        "Allegation_Id",
        "Id",
        "allegation_id",
        "Allegations_AllegationId",
        "Allegations_Allegation$Identity",
        "Allegation$Identity",
    ):
        raw = props.get(key) or alleg_item.get(key)
        if isinstance(raw, dict):
            raw_id = raw.get("Id") or raw.get("id")
            if raw_id:
                return str(raw_id).strip()
        elif raw:
            return str(raw).strip()
    for key in (
        "relationship:Allegations",
        "relationship:Allegations_Self",
        "relationship:Allegations_Allegation",
        "relationship:Allegation",
    ):
        href = links.get(key, {}).get("href", "")
        if href:
            alleg_id = _extract_id(href)
            if alleg_id:
                return alleg_id
    item_href = links.get("item", {}).get("href", "")
    if item_href:
        alleg_id = _extract_id(item_href)
        if alleg_id:
            return alleg_id
    return ""


def _allegation_status_from_item(alleg_item: dict) -> str:
    """
    Read allegation status directly from list-row Properties.
    Returns lower-cased status string or '' if not present.
    No extra API call required.
    """
    props = alleg_item.get("Properties", {})
    raw = (
        props.get("Allegations_Status")
        or props.get("Allegations_AllegationStatus")
        or ""
    )
    return str(raw).strip().lower()


def _allegation_date_from_item(alleg_item: dict) -> str:
    """Read date_received from list-row Properties — no extra fetch."""
    props = alleg_item.get("Properties", {})
    return (
        props.get("Allegations_DateReceived")
        or props.get("Allegations_DateReported")
        or ""
    )

def _allegation_comment_from_item(alleg_item: dict) -> str:
    """Read the business allegation description/comment from an Allegations row."""
    props = alleg_item.get("Properties", {}) if isinstance(alleg_item, dict) else {}
    raw = (
        props.get("Allegations_Comment")
        # or props.get("Allegations_AllegationCloseComment")
        or ""
    )
    comment = str(raw).strip() if raw is not None else ""
    if comment:
        return comment

    # Fallback removed: only use Allegations_Comment.
    # links = alleg_item.get("_links", {}) if isinstance(alleg_item, dict) else {}
    # item_href = (
    #     links.get("item", {}).get("href", "")
    #     or links.get("self", {}).get("href", "")
    # )
    # if not item_href:
    #     return ""

    # full_item = _safe_fetch(item_href)
    # full_props = full_item.get("Properties", {}) if isinstance(full_item, dict) else {}
    # full_raw = (
    #     full_props.get("Allegations_Comment")
    #     or full_props.get("Allegations_AllegationCloseComment")
    #     or ""
    # )
    # return str(full_raw).strip() if full_raw is not None else ""

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

# ── Financial fetch per API guide ─────────────────────────────────

def _fetch_financial_calculated(wf_id: str) -> float:
    """
    Fetch Financial_Calculated from Workfolder_FinancialRelationship.
    Returns sum of all Financial_Calculated values found, or None.
    """
    href = AppWorksPaths.Workfolder.financial(wf_id)
    res = _safe_fetch(href)
    records = res.get("_embedded", {}).get("Workfolder_FinancialRelationship", [])
    if not records:
        return None

    total = 0.0
    found = False
    for rec in records:
        props = rec.get("Properties", {})
        raw = props.get("Financial_Calculated")
        if raw is not None:
            try:
                total += float(raw)
                found = True
            except (TypeError, ValueError):
                pass
    return round(total, 4) if found else None


# ── Manifest filter (Stage B) ─────────────────────────────────────

def _apply_manifest_filters(
    candidates: list,
    required_status: str,
    lookback_years: int,
    type_quotas: dict = None,
    max_total_results: int = _DEFAULT_TOTAL_RESULTS_LIMIT,
) -> list:
    """Apply manifest constraints. All parameters come from manifest config."""
    now = datetime.now(timezone.utc)
    min_dt = datetime(
        now.year - max(0, int(lookback_years)), now.month, now.day, tzinfo=timezone.utc
    )
    required_status_norm = (required_status or "").strip().lower()

    filtered = []
    per_type_counts = {}
    seen_alleg = set()
    type_quotas = type_quotas or {}

    for row in candidates:
        if max_total_results > 0 and len(filtered) >= max_total_results:
            break

        # Filter 1: required_status
        if required_status_norm:
            case_status = (row.get("status") or "").strip().lower()
            # BSI specific: "open or closed" is a compound requirement.
            # If specified, we allow any case that has a status (non-empty).
            if required_status_norm == "open or closed":
                if not case_status:
                    continue
            elif case_status != required_status_norm:
                continue

        # Filter 2: lookback window
        dt = _parse_aw_date(row.get("date_received"))
        if lookback_years > 0 and dt and dt < min_dt:
            continue

        # Filter 3: deduplicate by allegation_id
        alleg_id = row.get("allegation_id")
        if alleg_id and alleg_id in seen_alleg:
            continue
        if alleg_id:
            seen_alleg.add(alleg_id)

        # Filter 4: allocated per-type quota under the aggregate result cap
        ftype = row.get("fraud_type", "UNKNOWN")
        cnt = per_type_counts.get(ftype, 0)
        quota = type_quotas.get(ftype, max_total_results)
        if quota <= 0 or cnt >= quota:
            continue
        per_type_counts[ftype] = cnt + 1
        filtered.append(row)

    return filtered


def _allocate_type_quotas(allegation_types: list, total_limit: int) -> tuple[dict, dict]:
    """
    Split the aggregate similar-case cap evenly across fraud types.

    Examples:
      2 types, total 10 -> 5 + 5
      3 types, total 10 -> 4 + 3 + 3
    """
    if not allegation_types or total_limit <= 0:
        return {}, {}

    type_count = len(allegation_types)
    base = total_limit // type_count
    remainder = total_limit % type_count
    by_id = {}
    by_desc = {}

    for index, (type_id, desc) in enumerate(allegation_types):
        quota = base + (1 if index < remainder else 0)
        fraud_type_desc = desc or str(type_id)
        by_id[type_id] = quota
        by_desc[fraud_type_desc] = quota

    return by_id, by_desc


def _names_match(fraud_type: str, aw_desc: str) -> bool:
    ft = fraud_type.strip().upper()
    aw = aw_desc.strip().upper()
    if ft in aw or aw in ft:
        return True
    ft_base = re.sub(r'[\d_\-]+$', '', ft).strip()
    aw_base = re.sub(r'[\d_\-]+$', '', aw).strip()
    if ft_base and aw_base and (ft_base in aw_base or aw_base in ft_base):
        return True
    fw = ft.split()[0] if ft.split() else ""
    aw_w = aw.split()[0] if aw.split() else ""
    if len(fw) >= 4 and fw == aw_w:
        return True
    return False


# ── Step 1: Resolve AllegationType IDs from current case ─────────

def _resolve_case_fraud_types(case_id: str, base_signatures: list = None, complaint_description: str = None) -> list:
    fraud_types: list = []
    seen: set = set()

    def add_signature(value):
        if not value:
            return
        text = str(value).strip()
        if not text:
            return
        if text not in seen:
            seen.add(text)
            fraud_types.append(text)

    if base_signatures:
        for signature in base_signatures:
            add_signature(signature)

    add_signature(complaint_description)

    if not case_id:
        return fraud_types

    try:
        rel_url = AppWorksPaths.Workfolder.allegations(case_id)
        
        items = _fetch_embedded(rel_url, "Workfolder_AllegationsRelationship")
        for item in items:
            alleg_href = item.get("_links", {}).get("self", {}).get("href", "")
            if not alleg_href:
                continue
            alleg_res = _safe_fetch(alleg_href)

            # Add the allegation row's comment/description if present.
            alleg_comment = _allegation_comment_from_item(alleg_res)
            add_signature(alleg_comment)

            type_href = _rel_href(alleg_res.get("_links", {}), "Allegations_AllegationsType")
            if not type_href:
                continue
            type_res = _safe_fetch(type_href)
            props = type_res.get("Properties", {})
            add_signature(props.get("AllegationType_AllegationTypeDescription"))
            add_signature(props.get("AllegationType_AllegationTypeShortDesc"))
            add_signature(props.get("AllegationType_AllegationTypeDefaults"))
    except Exception as exc:
        logger.warning(f"Failed to resolve case fraud types from case {case_id}: {exc}")
    return fraud_types


def _resolve_allegation_type_ids(case_id, fraud_types: list) -> list:
    seen_ids: set = set()
    result: list = []

    if case_id:
        try:
            rel_url = AppWorksPaths.Workfolder.allegations(case_id)
            rel_res = _safe_fetch(rel_url)
            alleg_items = rel_res.get("_embedded", {}).get("Workfolder_AllegationsRelationship", [])
            logger.info(f"[Path1] {len(alleg_items)} allegation item(s) for type resolution")

            for item in alleg_items:
                alleg_href = item.get("_links", {}).get("self", {}).get("href", "")
                if not alleg_href:
                    continue
                alleg_res = _safe_fetch(alleg_href)
                type_href = _rel_href(alleg_res.get("_links", {}), "Allegations_AllegationsType")
                type_id = _extract_id(type_href)
                if not type_id or type_id in seen_ids:
                    continue

                type_res = _safe_fetch(type_href)
                props = type_res.get("Properties", {})
                desc = (
                    props.get("AllegationType_AllegationTypeDescription")
                    or props.get("AllegationType_AllegationTypeShortDesc")
                    or ""
                )
                logger.info(f"  Type {type_id}: '{desc}'")

                if desc and any(_names_match(f, desc) for f in fraud_types):
                    seen_ids.add(type_id)
                    result.append({"id": type_id, "description": desc})
                    logger.info(f"  Resolved '{desc}' -> {type_id}")

            if result:
                return result
            logger.info("[Path1] no matches — trying Path 2")

        except Exception as exc:
            logger.warning(f"Path 1 type resolution failed: {exc}")

    logger.info(f"[Path2] name-match for {fraud_types}")
    try:
        res = _safe_fetch(AppWorksPaths.Allegations.allegation_type_all())
        items = res.get("_embedded", {}).get("AllegationType_All", [])
        logger.info(f"[Path2] {len(items)} AllegationType items")
        for item in items:
            type_id = _extract_id(item.get("_links", {}).get("self", {}).get("href", ""))
            if not type_id or type_id in seen_ids:
                continue
            props = item.get("Properties", {})
            desc = (
                props.get("AllegationType_AllegationTypeDescription")
                or props.get("AllegationType_AllegationTypeShortDesc")
                or ""
            )
            if desc and any(_names_match(f, desc) for f in fraud_types):
                seen_ids.add(type_id)
                result.append({"id": type_id, "description": desc})
                logger.info(f"  [Path2] '{desc}' -> {type_id}")
    except Exception as exc:
        logger.warning(f"Path 2 type resolution failed: {exc}")

    return result


# ── Main service ──────────────────────────────────────────────────

def search_similar_cases(
    case_id=None,
    fraud_types=None,
    complaint_description=None,
) -> dict:
    """
    Broad fetch from Allegations_All per allegation type, then manifest filtering.

    Stage A (broad — with early-exit guard):
      - Resolve allegation type IDs from current case
      - Query Allegations_All by each type ID (one request per type)
      - Read wf_id + status + date directly from list-row Properties
        (zero extra fetches for filtering decisions)
      - STOP collecting per type once (max_per_type * OVERFETCH_FACTOR)
        unique workfolders accumulated — avoids hundreds of HTTP calls
      - Fetch Workfolder + FinancialRelationship only for kept candidates

    Stage B (manifest filters — all values from manifest config):
      - required_status  (allegation status, resolved from list row)
      - similarity_lookback_years
      - max_total_results split across fraud types
    """
    print("===============================")
    print(fraud_types)

    cfg = _load_similar_cases_config()
    max_per_type        = int(cfg.get("max_results_per_type", 3))
    max_total_results   = int(cfg.get("max_total_results", _DEFAULT_TOTAL_RESULTS_LIMIT))
    required_status     = str(cfg.get("required_status", "Closed"))
    lookback_years      = int(cfg.get("similarity_lookback_years", 4))
    enable_broad_fetch  = bool(cfg.get("enable_broad_fetch_stage", True))
    fallback_to_raw     = bool(cfg.get("fallback_to_raw_when_filtered_empty", True))

    # Guard against positional call swap
    if isinstance(case_id, list) and (fraud_types is None or isinstance(fraud_types, str)):
        case_id, fraud_types = (str(fraud_types) if fraud_types else None), case_id

    fraud_types = fraud_types or []
    # fraud_types = _resolve_case_fraud_types(
    #     case_id,
    #     base_signatures=fraud_types,
    #     complaint_description=complaint_description,
    # )

    logger.info(
        f"Similar Case Retrieval: fraud_types={fraud_types} case={case_id} "
        f"max_per_type={max_per_type} max_total_results={max_total_results} "
        f"lookback_years={lookback_years} "
        f"required_status={required_status or 'ANY'}"
    )

    # Step 1: Resolve target AllegationType IDs
    # allegation_types = _resolve_allegation_type_ids(case_id, fraud_types)
    allegation_types = _normalise_to_type_dicts(fraud_types)
    # allegation_types = fraud_types
    logger.info(
        f"Resolved {len(allegation_types)} type(s): "
        f"{[(t[0], t[1]) for t in allegation_types]}"
    )

    if not allegation_types:
        logger.warning("No AllegationType IDs resolved — returning empty result.")
        return _build_result([], allegation_types, fraud_types, cfg=cfg)

    target_type_ids: set  = {t[0] for t in allegation_types}
    type_id_to_desc: dict = {t[0]: t[1] for t in allegation_types}
    type_quotas_by_id, type_quotas_by_desc = _allocate_type_quotas(
        allegation_types,
        max_total_results,
    )
    logger.info(f"Similar Case Retrieval quotas by type: {type_quotas_by_id}")

    candidates: list = []

    if enable_broad_fetch:
        # ── Stage A: broad fetch with per-type early-exit ─────────────────
        # Collecting budget = allocated quota * OVERFETCH_FACTOR unique workfolders
        # per type.  Once the budget is hit we break out of the row loop for
        # that type, capping the number of expensive workfolder fetches.
        required_status_norm = required_status.strip().lower()

        now    = datetime.now(timezone.utc)
        min_dt = (
            datetime(now.year - max(0, lookback_years), now.month, now.day, tzinfo=timezone.utc)
            if lookback_years > 0
            else None
        )

        # Shared caches across types
        wf_cache:  dict = {}   # wf_id -> Properties dict
        fin_cache: dict = {}   # wf_id -> Financial_Calculated (float | None)
        global_seen_pair: set = set()
        
        # Step A1: Identify candidate rows and unique workfolders to fetch
        type_to_rows = {}
        all_wf_ids_to_fetch = set()
        
        for type_id in target_type_ids:
            type_quota = type_quotas_by_id.get(type_id, max_per_type)
            if type_quota <= 0:
                continue
            collection_budget = max(type_quota * _OVERFETCH_FACTOR, type_quota + 1)
            list_href = AppWorksPaths.Allegations.allegations_by_type(type_id)
            list_res = _safe_fetch(list_href)
            rows = list_res.get("_embedded", {}).get("Allegations_All", [])
            type_to_rows[type_id] = rows
            
            seen_wf_for_type = set()
            for alleg_row in rows:
                if len(seen_wf_for_type) >= collection_budget: break
                wf_id = _workfolder_id_from_allegation_item(alleg_row)
                if not wf_id or wf_id == str(case_id): continue
                
                # Check pre-filter (date)
                date_raw = _allegation_date_from_item(alleg_row)
                if min_dt and date_raw:
                    dt = _parse_aw_date(date_raw)
                    if dt and dt < min_dt: continue
                
                seen_wf_for_type.add(wf_id)
                all_wf_ids_to_fetch.add(wf_id)

        # Step A2: Parallel fetch for Workfolders and Financials
        def _fetch_full_wf(wid):
            res = _safe_fetch(AppWorksPaths.Workfolder.item(wid))
            props = res.get("Properties", {})
            fins = _fetch_financial_calculated(wid)
            return wid, props, fins

        logger.info(f"[Parallel Fetch] Starting batch fetch for {len(all_wf_ids_to_fetch)} workfolders...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(_fetch_full_wf, all_wf_ids_to_fetch))
        
        for wid, props, fins in results:
            wf_cache[wid] = props
            fin_cache[wid] = fins

        # Step A3: Build candidates
        for type_id in target_type_ids:
            type_quota = type_quotas_by_id.get(type_id, max_per_type)
            if type_quota <= 0:
                continue
            collection_budget = max(type_quota * _OVERFETCH_FACTOR, type_quota + 1)
            rows = type_to_rows.get(type_id, [])
            fraud_type_desc = type_id_to_desc.get(type_id) or str(type_id)
            seen_wf_for_type = set()
            
            for alleg_row in rows:
                if len(seen_wf_for_type) >= collection_budget: break
                wf_id = _workfolder_id_from_allegation_item(alleg_row)
                if not wf_id or wf_id == str(case_id) or wf_id not in wf_cache: continue
                
                alleg_id = _allegation_id_from_item(alleg_row)
                pair = (wf_id, alleg_id)
                if pair in global_seen_pair: continue
                
                # Date pre-filter again (to stay consistent with previous logic)
                date_raw = _allegation_date_from_item(alleg_row)
                if min_dt and date_raw:
                    dt = _parse_aw_date(date_raw)
                    if dt and dt < min_dt: continue

                wf_props = wf_cache[wf_id]
                if not wf_props: continue
                
                seen_wf_for_type.add(wf_id)
                global_seen_pair.add(pair)

                # Resolved status
                destination = (wf_props.get("DESTINATION") or "").strip()
                destination_lower = destination.lower()
                if "closed" in destination_lower:
                    resolved_status = "closed"
                elif destination_lower:
                    resolved_status = destination_lower
                else:
                    resolved_status = (
                        (wf_props.get("WorkfolderStatus") or "").strip().lower()
                        or (wf_props.get("Status") or "").strip().lower()
                        or None
                    )

                workfolder_summary = (
                    wf_props.get("WorkfolderDescription")
                    or wf_props.get("Workfolder_CaseDescription")
                    or f"Historical {fraud_type_desc} allegation"
                )
                description = _allegation_comment_from_item(alleg_row) or None
                summary = workfolder_summary
                if destination: summary = f"{summary} [{destination}]"
                date_received = date_raw or wf_props.get("WorkfolderDateReceived") or None

                candidates.append({
                    "case_id":              wf_id,
                    "complaint_no":         wf_props.get("WorkfolderComplaintNumber"),
                    "allegation_id":        alleg_id,
                    "similarity_score":     1.0,
                    "fraud_type":           fraud_type_desc,
                    "outcome":              _OUTCOME_ALLEGATION_MATCH.format(type_id=type_id),
                    "summary":              summary,
                    "description":          description,
                    "status":               resolved_status,
                    "date_received":        date_received,
                    "financial_calculated": wf_props.get("WorkfolderFraudAmount") or None,
                })

            logger.info(
                f"[Broad Fetch] type={type_id} kept {len(seen_wf_for_type)} "
                f"workfolder(s) (budget={collection_budget})"
            )

    else:
        # ── Legacy traversal path (used when enable_broad_fetch_stage=false) ──
        seen_wf_ids:   set = {str(case_id)} if case_id else set()
        seen_alleg_ids: set = set()
        # type_counts:   dict = {t["id"]: 0 for t in allegation_types}
        type_counts:   dict = {t[0]: 0 for t in allegation_types}
        type_quotas:   dict = type_quotas_by_id or {t[0]: max_per_type for t in allegation_types}
        subject_ids = _get_subjects_for_case(case_id) if case_id else []
        for subject_id in subject_ids:
            hist_wf_ids = _get_historical_workfolders(
                subject_id, exclude_case_id=str(case_id or "")
            )
            for wf_id in hist_wf_ids:
                if wf_id in seen_wf_ids:
                    continue
                if all(count >= type_quotas.get(type_id, max_per_type) for type_id, count in type_counts.items()):
                    break
                seen_wf_ids.add(wf_id)
                wf_res   = _safe_fetch(AppWorksPaths.Workfolder.item(wf_id))
                wf_props = wf_res.get("Properties", {})
                matches  = _find_matching_allegations(wf_id, target_type_ids)
                fin_calculated = _fetch_financial_calculated(wf_id)
                for match in matches:
                    type_id  = match["type_id"]
                    alleg_id = match["allegation_id"]
                    if alleg_id in seen_alleg_ids or type_counts.get(type_id, 0) >= type_quotas.get(type_id, max_per_type):
                        continue
                    seen_alleg_ids.add(alleg_id)
                    type_counts[type_id] = type_counts.get(type_id, 0) + 1
                    fraud_type_desc = type_id_to_desc.get(type_id) or str(type_id)
                    workfolder_summary = (
                        wf_props.get("WorkfolderDescription")
                        or wf_props.get("Workfolder_CaseDescription")
                        or f"Historical {fraud_type_desc} allegation"
                    )
                    description = match.get("comment") or None
                    candidates.append({
                        "case_id":              wf_id,  
                        "complaint_no":         wf_props.get("WorkfolderComplaintNumber"),
                        "allegation_id":        alleg_id,
                        "similarity_score":     1.0,
                        "fraud_type":           fraud_type_desc,
                        "outcome":              _OUTCOME_SUBJECT_TRAVERSAL.format(type_id=type_id),
                        "summary":              workfolder_summary,
                        "description":          description,
                        "status":               match.get("status") or None,
                        "date_received":        match.get("date_received") or None,
                        "financial_calculated": wf_props.get("WorkfolderFraudAmount") or None,
                    })

    # ── Stage B: manifest post-filtering ─────────────────────────────────
    similar_cases = _apply_manifest_filters(
        candidates=candidates,
        required_status=required_status,
        lookback_years=lookback_years,
        type_quotas=type_quotas_by_desc,
        max_total_results=max_total_results,
    )

    if fallback_to_raw and not similar_cases and candidates:
        logger.info(
            "[Fallback] Filters removed all similar cases; returning raw candidate pool "
            "(fallback_to_raw_when_filtered_empty=true)"
        )
        similar_cases = _apply_manifest_filters(
            candidates=candidates,
            required_status="",
            lookback_years=0,
            type_quotas=type_quotas_by_desc,
            max_total_results=max_total_results,
        )

    logger.info(
        f"search_similar_cases done: raw={len(candidates)} filtered={len(similar_cases)} "
        f"| type_ids={list(target_type_ids)} | input={fraud_types} "
        f"| filters(status={required_status or 'ANY'}, lookback_years={lookback_years}, "
        f"max_per_type={max_per_type}, max_total_results={max_total_results}, "
        f"type_quotas={type_quotas_by_id}) "
        f"| broad_fetch={enable_broad_fetch} fallback_to_raw={fallback_to_raw}"
    )

    return _build_result(
        similar_cases=similar_cases,
        allegation_types=allegation_types,
        fraud_types=fraud_types,
        raw_count=len(candidates),
        cfg=cfg,
        type_quotas=type_quotas_by_id,
    )


# ── Legacy traversal helpers (used when enable_broad_fetch_stage=false) ──

def _get_subjects_for_case(case_id: str) -> list:
    subject_ids: list = []
    seen: set = set()
    rel_href = AppWorksPaths.Workfolder.subjects(case_id)
    subj_items = _fetch_embedded(rel_href, "Workfolder_SubjectsRelationship")
    logger.info(f"[Traversal] {len(subj_items)} subject link(s) for case {case_id}")
    for item in subj_items:
        subjects_href = item.get("_links", {}).get("self", {}).get("href", "")
        if not subjects_href:
            continue
        subjects_res  = _safe_fetch(subjects_href)
        subject_href  = _rel_href(subjects_res.get("_links", {}), "Subjects_Subject")
        subject_id    = _extract_id(subject_href)
        if subject_id and subject_id not in seen:
            seen.add(subject_id)
            subject_ids.append(subject_id)
    logger.info(f"[Traversal] Resolved subject IDs: {subject_ids}")
    return subject_ids


def _get_historical_workfolders(subject_id: str, exclude_case_id: str) -> list:
    wf_ids: list = []
    seen: set = set()
    mapping_href = (
         AppWorksPaths.Subject.workfolder_mappings(subject_id)
    )
    mappings = _fetch_embedded(mapping_href, "Subject_SubjectWorkfolderMapping")
    logger.info(f"[Traversal]   Subject {subject_id}: {len(mappings)} mapping(s)")
    for m in mappings:
        mapping_id = m.get("Identity", {}).get("Id")
        if not mapping_id:
            continue
        item_url = (
           AppWorksPaths.Subject.workfolder_mapping_item(subject_id, mapping_id)
        )
        item_res = _safe_fetch(item_url)
        wf_link  = _rel_href(
            item_res.get("_links", {}), "SubjectWorkfolderMapping_WorkfolderRelation"
        )
        wf_id = _extract_id(wf_link)
        if wf_id and wf_id != str(exclude_case_id) and wf_id not in seen:
            seen.add(wf_id)
            wf_ids.append(wf_id)
    logger.info(f"[Traversal]   Subject {subject_id}: historical WF IDs = {wf_ids}")
    return wf_ids


def _find_matching_allegations(wf_id: str, target_type_ids: set) -> list:
    matches: list = []
    alleg_rel = (
        
        AppWorksPaths.Workfolder.allegations(wf_id)
    )
    alleg_items = _fetch_embedded(alleg_rel, "Workfolder_AllegationsRelationship")
    for item in alleg_items:
        alleg_href = item.get("_links", {}).get("self", {}).get("href", "")
        if not alleg_href:
            continue
        alleg_id  = _extract_id(alleg_href)
        alleg_res = _safe_fetch(alleg_href)
        type_href = _rel_href(alleg_res.get("_links", {}), "Allegations_AllegationsType")
        type_id   = _extract_id(type_href)
        type_key  = int(type_id) if str(type_id).isdigit() else type_id
        if type_key and type_key in target_type_ids:
            props = alleg_res.get("Properties", {})
            matches.append({
                "allegation_id": alleg_id,
                "type_id":       type_key,
                "status": (
                    props.get("Allegations_Status")
                    or props.get("Allegations_AllegationStatus")
                    or None
                ),
                "date_received": props.get("Allegations_DateReceived") or None,
                "comment":       (
                    props.get("Allegations_Comment")
                    or props.get("Allegations_AllegationCloseComment")
                    or None
                ),
            })
    return matches


# ── Result builder ────────────────────────────────────────────────

def _build_result(
    similar_cases: list,
    allegation_types: list,
    fraud_types: list,
    raw_count: int = 0,
    cfg: dict = None,
    type_quotas: dict = None,
) -> dict:
    cfg = cfg or {}
    required_status = str(cfg.get("required_status", "") or "")
    lookback_years  = int(cfg.get("similarity_lookback_years", 0))
    max_per_type    = int(cfg.get("max_results_per_type", 0))
    max_total_results = int(cfg.get("max_total_results", _DEFAULT_TOTAL_RESULTS_LIMIT))
    type_quotas = type_quotas or {}
    # type_id_list    = [t["id"] for t in allegation_types]
    type_id_list = [t[0] for t in allegation_types]
 

    query_summary = (
        f"Found {len(similar_cases)} similar archive match(es) "
        f"after filtering {raw_count} broad candidate(s) "
        f"across {len(allegation_types)} fraud types."
    )
    return {
        "result": SimilarCasesResult(
            query_summary=query_summary,
            matches=similar_cases,
            top_n_returned=len(similar_cases),
            raw_matches_found=raw_count,
            manifest_filters_applied={
                "required_status":           required_status,
                "similarity_lookback_years": lookback_years,
                "max_results_per_type":      max_per_type,
                "max_total_results":         max_total_results,
                "type_quotas":               type_quotas,
            },
        ).model_dump(),
        "provenance": {
            "sources": [
                f"AppWorks Allegations_All broad fetch "
                f"(type IDs: {type_id_list}, resolved from: {fraud_types})"
            ],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by":  "AppWorks REST retrieval + manifest post-filtering",
        },
    }

def _normalise_to_type_dicts(fraud_types: list) -> list:
    result = []
    seen = set()
 
    for ft in (fraud_types or []):
        type_id = ""
        desc = ""
 
        if isinstance(ft, dict):
            type_id = str(ft.get("type_id") or ft.get("id", "")).strip()
            desc    = ft.get("description", "")
 
        elif isinstance(ft, str) and ft.strip().startswith("{"):
            try:
                parsed  = json.loads(ft)
                type_id = str(parsed.get("type_id") or parsed.get("id", "")).strip()
                desc    = parsed.get("description", "")
            except Exception:
                logger.warning(f"[normalise] failed to parse JSON string: {ft}")
                continue
 
        elif isinstance(ft, str) and ":" in ft and ft.split(":")[0].strip().isdigit():
            parts   = ft.split(":", 1)
            type_id = parts[0].strip()
            desc    = parts[1].strip() if len(parts) > 1 else ""
 
        elif isinstance(ft, str) and ft.strip().isdigit():
            type_id = ft.strip()
            desc    = ""
 
        elif isinstance(ft, (int, float)):
            type_id = str(int(ft))
            desc    = ""
 
        else:
            logger.warning(f"[normalise] skipping unresolvable: {ft}")
            continue
        type_id = int(type_id)
        if type_id and type_id not in seen:
            seen.add(type_id)
            result.append((type_id, desc))
    return result

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