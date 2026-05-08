# ----------------------------------------------------------------
# Agent 3: Similar Case Retrieval — Subject Traversal Strategy
# ----------------------------------------------------------------
#
# WHY TRAVERSAL (not Allegations_All list filter):
#   The Allegations_All list endpoint returns 0 rows for every tested
#   filter variant (Allegations_AllegationsType$Identity.Id, ID, etc.),
#   even though the AppWorks UI shows data when filtered by the same ID.
#   The UI filter uses an internal mechanism NOT exposed via the
#   entityservice REST list API.
#
#   Instead, we traverse:
#     1. Current case  → Workfolder_SubjectsRelationship
#     2. Each subject  → Subject_SubjectWorkfolderMapping   (same as f2)
#     3. Each mapping  → SubjectWorkfolderMapping_WorkfolderRelation
#     4. Historical WF → Workfolder_AllegationsRelationship
#     5. Each allegation → Allegations_AllegationsType
#     6. Match type ID against resolved IDs from current case
#
#   All of these endpoints are proven working (confirmed in f1/f2 logs).
# ----------------------------------------------------------------

import logging
import re
import yaml
import os
from datetime import datetime, timezone
from semantic_layer.appworks_auth import fetch
from semantic_layer.semantic_model import SimilarCasesResult

logger = logging.getLogger(__name__)

_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "../../config/manifest.yaml")


# ── Config ────────────────────────────────────────────────────────

def _load_f3_config() -> dict:
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
    """Extract parent workfolder ID from Allegations list row."""
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
    item_href = links.get("item", {}).get("href", "")
    if item_href:
        return _extract_id(item_href)
    return ""


def _parse_aw_date(raw: str):
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _apply_manifest_filters(
    candidates: list,
    max_per_type: int,
    required_status: str,
    lookback_years: int,
) -> list:
    """
    Apply manifest constraints AFTER broad candidate collection.
    This keeps fetching broad (other_dev_api style) and filtering explicit.
    """
    now = datetime.now(timezone.utc)
    min_dt = datetime(now.year - max(0, int(lookback_years)), now.month, now.day, tzinfo=timezone.utc)
    required_status_norm = (required_status or "").strip().lower()

    filtered = []
    per_type_counts = {}
    seen_alleg = set()

    for row in candidates:
        case_status = (row.get("status") or "").strip().lower()
        if required_status_norm and case_status != required_status_norm:
            continue

        dt = _parse_aw_date(row.get("date_received"))
        if dt and dt < min_dt:
            continue

        alleg_id = row.get("allegation_id")
        if alleg_id and alleg_id in seen_alleg:
            continue
        if alleg_id:
            seen_alleg.add(alleg_id)

        ftype = row.get("fraud_type", "UNKNOWN")
        cnt = per_type_counts.get(ftype, 0)
        if cnt >= max_per_type:
            continue
        per_type_counts[ftype] = cnt + 1
        filtered.append(row)

    return filtered


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

def _resolve_allegation_type_ids(case_id, fraud_types: list) -> list:
    """
    Path 1 — resolve from current case allegations (proven working in f1 logs):
      Workfolder_AllegationsRelationship → Allegations entity
      → relationship:Allegations_AllegationsType → AllegationType entity
      → AllegationType_AllegationTypeDescription

    Path 2 — fallback: name-match AllegationType list
    """
    seen_ids: set = set()
    result: list = []

    if case_id:
        try:
            rel_url = f"/entities/Workfolder/items/{case_id}/relationships/Workfolder_AllegationsRelationship"
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
                    logger.info(f"  ✅ Resolved '{desc}' -> {type_id}")

            if result:
                return result
            logger.info("[Path1] no matches — trying Path 2")

        except Exception as exc:
            logger.warning(f"Path 1 type resolution failed: {exc}")

    logger.info(f"[Path2] name-match for {fraud_types}")
    try:
        res = _safe_fetch("/entities/AllegationType/lists/AllegationType_All")
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


# ── Step 2: Subject Traversal ─────────────────────────────────────

def _get_subjects_for_case(case_id: str) -> list:
    """
    Get Subject entity IDs linked to this workfolder.

    Workfolder → Workfolder_SubjectsRelationship
               → Subjects entity (join table)
               → Subjects_Subject → Subject entity ID
    """
    subject_ids: list = []
    seen: set = set()

    rel_href = f"/entities/Workfolder/items/{case_id}/relationships/Workfolder_SubjectsRelationship"
    subj_items = _fetch_embedded(rel_href, "Workfolder_SubjectsRelationship")
    logger.info(f"[Traversal] {len(subj_items)} subject link(s) for case {case_id}")

    for item in subj_items:
        subjects_href = item.get("_links", {}).get("self", {}).get("href", "")
        if not subjects_href:
            continue
        subjects_res = _safe_fetch(subjects_href)
        subject_href = _rel_href(subjects_res.get("_links", {}), "Subjects_Subject")
        subject_id = _extract_id(subject_href)
        if subject_id and subject_id not in seen:
            seen.add(subject_id)
            subject_ids.append(subject_id)

    logger.info(f"[Traversal] Resolved subject IDs: {subject_ids}")
    return subject_ids


def _get_historical_workfolders(subject_id: str, exclude_case_id: str) -> list:
    """
    Get historical workfolder IDs for a subject (same pattern as f2 enrichment).

    Subject → Subject_SubjectWorkfolderMapping
            → SubjectWorkfolderMapping_WorkfolderRelation → workfolder_id
    """
    wf_ids: list = []
    seen: set = set()

    mapping_href = f"/entities/Subject/items/{subject_id}/childEntities/Subject_SubjectWorkfolderMapping"
    mappings = _fetch_embedded(mapping_href, "Subject_SubjectWorkfolderMapping")
    logger.info(f"[Traversal]   Subject {subject_id}: {len(mappings)} mapping(s)")

    for m in mappings:
        mapping_id = m.get("Identity", {}).get("Id")
        if not mapping_id:
            continue

        item_url = (
            f"/entities/Subject/items/{subject_id}"
            f"/childEntities/Subject_SubjectWorkfolderMapping/items/{mapping_id}"
        )
        item_res = _safe_fetch(item_url)
        wf_link = _rel_href(item_res.get("_links", {}), "SubjectWorkfolderMapping_WorkfolderRelation")
        wf_id = _extract_id(wf_link)

        if wf_id and wf_id != str(exclude_case_id) and wf_id not in seen:
            seen.add(wf_id)
            wf_ids.append(wf_id)

    logger.info(f"[Traversal]   Subject {subject_id}: historical WF IDs = {wf_ids}")
    return wf_ids


def _find_matching_allegations(wf_id: str, target_type_ids: set) -> list:
    """
    Return allegations from a historical workfolder whose AllegationType
    is in target_type_ids.
    """
    matches: list = []

    alleg_rel = f"/entities/Workfolder/items/{wf_id}/relationships/Workfolder_AllegationsRelationship"
    alleg_items = _fetch_embedded(alleg_rel, "Workfolder_AllegationsRelationship")

    for item in alleg_items:
        alleg_href = item.get("_links", {}).get("self", {}).get("href", "")
        if not alleg_href:
            continue
        alleg_id = _extract_id(alleg_href)
        alleg_res = _safe_fetch(alleg_href)

        type_href = _rel_href(alleg_res.get("_links", {}), "Allegations_AllegationsType")
        type_id = _extract_id(type_href)

        if type_id and type_id in target_type_ids:
            props = alleg_res.get("Properties", {})
            matches.append({
                "allegation_id": alleg_id,
                "type_id":       type_id,
                "status":        props.get("Allegations_Status") or props.get("Allegations_AllegationStatus", ""),
                "date_received": props.get("Allegations_DateReceived", ""),
                "comment":       props.get("Allegations_Comment") or "",
            })

    return matches


# ── Main service ──────────────────────────────────────────────────

def search_similar_cases(
    case_id=None,
    fraud_types=None,
    complaint_description=None,
) -> dict:
    """
    Broad fetch first (other_dev_api style), then manifest filtering.

    Stage A (broad):
      - Resolve allegation type IDs from current case
      - Query Allegations_All by each type ID
      - Build full candidate pool (excluding current case only)

    Stage B (manifest filters):
      - required_status
      - similarity_lookback_years
      - max_results_per_type
    """
    cfg = _load_f3_config()
    max_per_type = int(cfg.get("max_results_per_type", 5))
    required_status = str(cfg.get("required_status", "") or "")
    lookback_years = int(cfg.get("similarity_lookback_years", 3))
    # Permission-gated rollout flags (kept OFF by default in manifest):
    # - enable_broad_fetch_stage: other-dev style broad pull first, filter second.
    # - fallback_to_raw_when_filtered_empty: if filters remove all matches, return raw.
    enable_broad_fetch = bool(cfg.get("enable_broad_fetch_stage", False))
    fallback_to_raw = bool(cfg.get("fallback_to_raw_when_filtered_empty", False))

    # Guard against positional call swap
    if isinstance(case_id, list) and (fraud_types is None or isinstance(fraud_types, str)):
        case_id, fraud_types = (str(fraud_types) if fraud_types else None), case_id

    fraud_types = fraud_types or []

    logger.info(
        f"Similar Case Retrieval: fraud_types={fraud_types} "
        f"case={case_id} max_per_type={max_per_type}"
    )

    # Step 1: Resolve target AllegationType IDs
    allegation_types = _resolve_allegation_type_ids(case_id, fraud_types)
    logger.info(
        f"Resolved {len(allegation_types)} type(s): "
        f"{[(t['id'], t['description']) for t in allegation_types]}"
    )

    if not allegation_types:
        logger.warning("No AllegationType IDs resolved — returning empty result.")
        return _build_result([], allegation_types, fraud_types)

    target_type_ids: set = {t["id"] for t in allegation_types}
    type_id_to_desc: dict = {t["id"]: t["description"] for t in allegation_types}

    candidates: list = []
    if enable_broad_fetch:
        # Stage A: broad candidate fetch from Allegations_All
        seen_pair: set = set()
        wf_cache: dict = {}

        for type_id in target_type_ids:
            list_href = f"/entities/Allegations/lists/Allegations_All?Allegations_AllegationsType$Identity.Id={type_id}"
            list_res = _safe_fetch(list_href)
            rows = list_res.get("_embedded", {}).get("Allegations_All", [])
            logger.info(f"[Broad Fetch] type={type_id} returned {len(rows)} allegation row(s)")

            for alleg_row in rows:
                wf_id = _workfolder_id_from_allegation_item(alleg_row)
                if not wf_id or wf_id == str(case_id):
                    continue
                alleg_href = alleg_row.get("_links", {}).get("self", {}).get("href", "")
                alleg_id = _extract_id(alleg_href)
                pair = (wf_id, alleg_id)
                if pair in seen_pair:
                    continue
                seen_pair.add(pair)

                if wf_id not in wf_cache:
                    wf_res = _safe_fetch(f"/entities/Workfolder/items/{wf_id}")
                    wf_cache[wf_id] = wf_res.get("Properties", {})
                wf_props = wf_cache.get(wf_id, {})
                if not wf_props:
                    continue

                fraud_type_desc = type_id_to_desc.get(type_id, type_id)
                summary = (
                    wf_props.get("WorkfolderDescription")
                    or wf_props.get("Workfolder_CaseDescription")
                    or f"Historical {fraud_type_desc} allegation"
                )
                candidates.append({
                    "case_id":              wf_id,
                    "allegation_id":        alleg_id,
                    "similarity_score":     1.0,
                    "fraud_type":           fraud_type_desc,
                    "outcome":              "Allegation type match (broad candidate)",
                    "summary":              summary,
                    "status":               wf_props.get("WorkfolderStatus", ""),
                    "date_received":        wf_props.get("WorkfolderDateReceived", ""),
                    "financial_calculated": 0.0,
                })
    else:
        # Legacy traversal-only behavior kept as default until approval.
        seen_wf_ids: set = {str(case_id)} if case_id else set()
        seen_alleg_ids: set = set()
        type_counts: dict = {t["id"]: 0 for t in allegation_types}
        subject_ids: list = _get_subjects_for_case(case_id) if case_id else []
        for subject_id in subject_ids:
            hist_wf_ids = _get_historical_workfolders(subject_id, exclude_case_id=str(case_id or ""))
            for wf_id in hist_wf_ids:
                if wf_id in seen_wf_ids:
                    continue
                if all(count >= max_per_type for count in type_counts.values()):
                    break
                seen_wf_ids.add(wf_id)
                wf_res = _safe_fetch(f"/entities/Workfolder/items/{wf_id}")
                wf_props = wf_res.get("Properties", {})
                matches = _find_matching_allegations(wf_id, target_type_ids)
                for match in matches:
                    type_id = match["type_id"]
                    alleg_id = match["allegation_id"]
                    if alleg_id in seen_alleg_ids or type_counts.get(type_id, 0) >= max_per_type:
                        continue
                    seen_alleg_ids.add(alleg_id)
                    type_counts[type_id] = type_counts.get(type_id, 0) + 1
                    fraud_type_desc = type_id_to_desc.get(type_id, type_id)
                    summary = (
                        wf_props.get("WorkfolderDescription")
                        or wf_props.get("Workfolder_CaseDescription")
                        or f"Historical {fraud_type_desc} allegation"
                    )
                    candidates.append({
                        "case_id":              wf_id,
                        "allegation_id":        alleg_id,
                        "similarity_score":     1.0,
                        "fraud_type":           fraud_type_desc,
                        "outcome":              "Subject history — allegation type match",
                        "summary":              summary,
                        "status":               match.get("status", ""),
                        "date_received":        match.get("date_received", ""),
                        "financial_calculated": 0.0,
                    })

    # Stage B: manifest filtering
    similar_cases = _apply_manifest_filters(
        candidates=candidates,
        max_per_type=max_per_type,
        required_status=required_status,
        lookback_years=lookback_years,
    )
    if fallback_to_raw and not similar_cases and candidates:
        logger.info(
            "[Fallback] Filters removed all similar cases; returning raw candidate pool "
            "(fallback_to_raw_when_filtered_empty=true)"
        )
        similar_cases = candidates

    logger.info(
        f"search_similar_cases done: raw={len(candidates)} filtered={len(similar_cases)} "
        f"| type_ids={list(target_type_ids)} | input={fraud_types} "
        f"| filters(status={required_status or 'ANY'}, lookback_years={lookback_years}, max_per_type={max_per_type}) "
        f"| broad_fetch={enable_broad_fetch} fallback_to_raw={fallback_to_raw}"
    )

    return _build_result(
        similar_cases=similar_cases,
        allegation_types=allegation_types,
        fraud_types=fraud_types,
        raw_count=len(candidates),
        required_status=required_status,
        lookback_years=lookback_years,
        max_per_type=max_per_type,
    )


def _build_result(
    similar_cases: list,
    allegation_types: list,
    fraud_types: list,
    raw_count: int = 0,
    required_status: str = "",
    lookback_years: int = 0,
    max_per_type: int = 0,
) -> dict:
    type_id_list = [t["id"] for t in allegation_types]
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
                "required_status": required_status,
                "similarity_lookback_years": lookback_years,
                "max_results_per_type": max_per_type,
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