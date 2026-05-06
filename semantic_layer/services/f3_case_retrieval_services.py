# semantic_layer/services/f3_case_retrieval_services.py
# ----------------------------------------------------------------
# Agent 3: Similar Case Retrieval
# ----------------------------------------------------------------
# Finds similar archive references by allegation type, using the
# manifest config for result limits.
#
# Lightweight allegation-type ID resolution:
#   Fetches ONLY Workfolder_AllegationsRelationship to get type IDs.
#   Does NOT call build_case_header_data() — avoids double-fetching the
#   full intake tree (subjects, addresses, aliases).
# ----------------------------------------------------------------

import logging
import yaml
import os
from datetime import datetime, timezone
from semantic_layer.appworks_auth import fetch
from semantic_layer.semantic_model import SimilarCasesResult, SimilarCaseMatch

logger = logging.getLogger(__name__)

# ── Manifest config ───────────────────────────────────────────────
_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "../../config/manifest.yaml")

def _load_f3_config() -> dict:
    """Load the search_similar_cases config block from manifest."""
    try:
        with open(_MANIFEST_PATH) as f:
            manifest = yaml.safe_load(f)
        for tool in manifest.get("tools", []):
            if tool.get("name") == "search_similar_cases":
                return tool.get("config", {})
    except Exception as e:
        logger.warning(f"Could not load manifest config: {e}")
    return {}


# ── Helpers ───────────────────────────────────────────────────────

def _extract_id(href: str) -> str | None:
    if href:
        return href.rstrip("/").split("/")[-1]
    return None


def _safe_fetch(href: str) -> dict:
    try:
        return fetch(href)
    except Exception as exc:
        logger.warning(f"fetch failed [{href}]: {exc}")
        return {}


def _fetch_props_links(href: str) -> tuple[dict, dict]:
    res = _safe_fetch(href)
    return res.get("Properties", {}), res.get("_links", {})


def _workfolder_id_from_allegation(alleg_item: dict) -> str:
    """
    Extract the parent Workfolder id from an Allegations list item if the
    list endpoint exposes it. This function intentionally does not fetch
    allegation details.
    """
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
        wf_id = _extract_id(href)
        if wf_id:
            return wf_id

    return ""


def _fetch_embedded(href: str, key: str) -> list:
    try:
        res = fetch(href)
        return res.get("_embedded", {}).get(key, [])
    except Exception as exc:
        logger.warning(f"embedded fetch failed [{href}]: {exc}")
        return []


def _allegation_archive_id(alleg_item: dict) -> str:
    links = alleg_item.get("_links", {})
    for key in ("item", "self"):
        ref_id = _extract_id(links.get(key, {}).get("href", ""))
        if ref_id:
            return ref_id
    props = alleg_item.get("Properties", {})
    for key in ("Id", "id", "Identity", "AllegationsId"):
        raw = props.get(key)
        if isinstance(raw, dict):
            raw = raw.get("Id") or raw.get("id")
        if raw:
            return str(raw).strip()
    return "unknown"


def _allegation_summary(alleg_item: dict, fraud_type: str) -> str:
    props = alleg_item.get("Properties", {})
    return (
        props.get("WorkfolderDescription")
        or props.get("Workfolder_CaseDescription")
        or props.get("Allegations_Allegation")
        or props.get("Allegations_Comment")
        or f"Archived allegation matching {fraud_type}"
    )


def _current_allegation_archive_ids(case_id: str) -> set[str]:
    try:
        wf_res = fetch(f"/entities/Workfolder/items/{case_id}")
        alleg_href = wf_res.get("_links", {}).get(
            "relationship:Workfolder_AllegationsRelationship", {}
        ).get("href", "")
        if not alleg_href:
            return set()
        alleg_items = _fetch_embedded(alleg_href, "Workfolder_AllegationsRelationship")
        return {
            archive_id
            for archive_id in (_allegation_archive_id(item) for item in alleg_items)
            if archive_id and archive_id != "unknown"
        }
    except Exception as exc:
        logger.warning(f"Failed resolving current allegation IDs for {case_id}: {exc}")
        return set()


# ── AllegationType ID resolution (lightweight) ────────────────────

def _resolve_allegation_type_ids(
    case_id: str | None,
    fraud_types: list[str],
) -> list[dict]:
    """
    Resolves AllegationType IDs for the given fraud_types list.

    Strategy:
      1. If case_id is provided: read the workfolder's allegation relationship to
         get the type IDs used in that specific case. This is the most accurate path.
      2. If case_id is None (or workfolder fetch fails): fall back to searching the
         AllegationType list endpoint by description match. This allows search_similar_cases
         to work without a case_id in scope.

    Does NOT re-fetch subjects, addresses, or aliases.
    """
    seen_type_ids: set[str] = set()
    allegation_types: list[dict] = []

    # ── Path 1: resolve from workfolder allegation links (preferred) ─
    if case_id:
        try:
            wf_res   = fetch(f"/entities/Workfolder/items/{case_id}")
            wf_links = wf_res.get("_links", {})

            alleg_rel_href = wf_links.get(
                "relationship:Workfolder_AllegationsRelationship", {}
            ).get("href")

            if alleg_rel_href:
                alleg_items = _fetch_embedded(alleg_rel_href, "Workfolder_AllegationsRelationship")
                logger.info(f"Found {len(alleg_items)} allegation(s) via workfolder for type ID resolution")

                for alleg_item in alleg_items:
                    try:
                        # Try to resolve type_id from links
                        type_id = None
                        alleg_type_href = alleg_item.get("_links", {}).get(
                            "relationship:Allegations_AllegationsType", {}
                        ).get("href", "")

                        if not alleg_type_href:
                            # Try detail fetch if link missing in list
                            item_href = alleg_item.get("_links", {}).get("self", {}).get("href", "")
                            if item_href:
                                item_res = _safe_fetch(item_href)
                                alleg_type_href = item_res.get("_links", {}).get(
                                    "relationship:Allegations_AllegationsType", {}
                                ).get("href", "")

                        if alleg_type_href:
                            type_id = _extract_id(alleg_type_href)
                            
                        if not type_id or type_id in seen_type_ids:
                            continue

                        # Fetch properties to match against fraud_types names
                        type_props, _ = _fetch_props_links(alleg_type_href)
                        desc = (
                            type_props.get("AllegationType_AllegationTypeDescription")
                            or type_props.get("AllegationType_AllegationTypeShortDesc")
                            or "Unknown"
                        )

                        desc_upper = desc.upper()
                        if any(f.upper() in desc_upper or desc_upper in f.upper() for f in fraud_types):
                            seen_type_ids.add(type_id)
                            allegation_types.append({"id": type_id, "description": desc})
                            logger.info(f"  Matched Type: {desc} (ID: {type_id})")

                    except Exception as exc:
                        logger.warning(f"Failed processing allegation for type resolution: {exc}")

                if allegation_types:
                    return allegation_types

        except Exception as exc:
            logger.warning(f"Workfolder-based type resolution failed for {case_id}: {exc}")

    # ── Path 2: name-based fallback via AllegationType list ──────────
    # Used when case_id is absent or path 1 found nothing.
    logger.info(f"Falling back to name-based AllegationType resolution for: {fraud_types}")
    try:
        type_list_res = _safe_fetch("/entities/AllegationType/lists/AllegationType_All")
        type_items    = type_list_res.get("_embedded", {}).get("AllegationType_All", [])

        for item in type_items:
            try:
                type_self_href = item.get("_links", {}).get("self", {}).get("href", "")
                type_id = _extract_id(type_self_href)
                if not type_id or type_id in seen_type_ids:
                    continue

                props = item.get("Properties", {})
                desc  = (
                    props.get("AllegationType_AllegationTypeDescription")
                    or props.get("AllegationType_AllegationTypeShortDesc")
                    or ""
                )
                if not desc:
                    continue

                desc_upper = desc.upper()
                if any(f.upper() in desc_upper or desc_upper in f.upper() for f in fraud_types):
                    seen_type_ids.add(type_id)
                    allegation_types.append({"id": type_id, "description": desc})

            except Exception as exc:
                logger.warning(f"Failed processing type list item: {exc}")

    except Exception as exc:
        logger.warning(f"Name-based AllegationType resolution failed: {exc}")

    return allegation_types


# ── Main service function ─────────────────────────────────────────

def search_similar_cases(
    fraud_types: list[str],
    case_id: str | None = None,
    complaint_description: str | None = None,
) -> dict:
    """
    Finds similar cases based on fraud types (AllegationType).
    """
    # Robust input handling for agentic flexibility
    if isinstance(fraud_types, str):
        fraud_types = [s.strip() for s in fraud_types.split(",") if s.strip()]
    
    if not fraud_types:
        logger.warning("search_similar_cases called with empty fraud_types")
        return {
            "result": {
                "query_summary": "No fraud types provided for search.",
                "matches": [],
                "top_n_returned": 0
            },
            "provenance": {
                "sources": [],
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "computed_by": "System Early Return",
            }
        }

    cfg = _load_f3_config()
    max_per_type = int(cfg.get("max_results_per_type", 3))

    # Build keyword set from complaint_description for optional filtering
    desc_keywords: set[str] = set()
    if complaint_description:
        desc_keywords = {
            w.lower() for w in complaint_description.split()
            if len(w) > 3
        }

    logger.info(
        f"Similar Case Retrieval for: {fraud_types} (case={case_id}) | "
        f"config: max_per_type={max_per_type}, source=Allegations_All archive"
        + (f", desc_filter={len(desc_keywords)} keywords" if desc_keywords else "")
    )

    # Step 1: Lightweight allegation type ID resolution
    allegation_types = _resolve_allegation_type_ids(case_id, fraud_types)
    current_allegation_ids = _current_allegation_archive_ids(case_id) if case_id else set()
    logger.info(
        f"Resolved {len(allegation_types)} AllegationType(s): "
        f"{[t['id'] for t in allegation_types]}"
    )

    # Step 2: For each type ID, query Allegations_All and return bounded
    # archive references. Do not hydrate each row into Workfolder details.
    similar_cases: list[dict] = []
    seen_archive_ids_global: set[str] = set()

    for alleg_type in allegation_types:
        type_id     = alleg_type["id"]
        description = alleg_type["description"]
        type_count  = 0  # counter for max_per_type cap
        seen_archive_ids: set[str] = set()

        logger.info(f"Searching type ID {type_id} ({description})")

        list_href = (
            f"/entities/Allegations/lists/Allegations_All"
            f"?Allegations_AllegationsType$Identity.Id={type_id}"
        )
        list_res          = _safe_fetch(list_href)
        matched_alleg_all = list_res.get("_embedded", {}).get("Allegations_All", [])
        logger.info(f"  {len(matched_alleg_all)} raw allegation match(es)")

        skipped_same_case = 0
        scanned = 0

        for alleg_item in reversed(matched_alleg_all):
            if type_count >= max_per_type:
                logger.info(f"  max_per_type={max_per_type} reached for type {type_id}")
                break
            scanned += 1

            try:
                archive_id = _allegation_archive_id(alleg_item)
                wf_id = _workfolder_id_from_allegation(alleg_item)

                if case_id and (wf_id == str(case_id) or archive_id in current_allegation_ids):
                    skipped_same_case += 1
                    continue

                if archive_id in seen_archive_ids or archive_id in seen_archive_ids_global:
                    skipped_same_case += 1
                    continue

                # Build summary for keyword filtering and result
                summary_text = _allegation_summary(alleg_item, description)

                # Optional complaint_description keyword filter
                if desc_keywords:
                    summary_lower = summary_text.lower()
                    if not any(kw in summary_lower for kw in desc_keywords):
                        logger.info(f"  Skipping {archive_id} — no keyword match")
                        skipped_same_case += 1
                        continue

                seen_archive_ids.add(archive_id)
                seen_archive_ids_global.add(archive_id)
                type_count += 1

                # Prefer real workfolder ID; fall back to allegation reference
                resolved_case_id = wf_id if wf_id else f"allegation:{archive_id}"

                similar_cases.append({
                    "case_id":              resolved_case_id,
                    "allegation_id":        archive_id,
                    "similarity_score":     1.0,
                    "fraud_type":           description,
                    "outcome":              "Archived allegation match",
                    "summary":              summary_text,
                    "estimated_loss":       0.0,
                    "financial_calculated": 0.0,
                })

            except Exception as exc:
                logger.warning(f"  Failed processing allegation item: {exc}")

        logger.info(
            f"Type {type_id} summary: scanned={scanned}, returned={type_count}, "
            f"skipped_same_case={skipped_same_case}"
        )

    query_summary = f"Found {len(similar_cases)} similar archive match(es) across {len(allegation_types)} fraud types."
    result_data = {
        "query_summary":  query_summary,
        "matches":        similar_cases,
        "top_n_returned": len(similar_cases)
    }

    validated_result = SimilarCasesResult(**result_data)

    return {
        "result": validated_result.model_dump(),
        "provenance": {
            "sources": [
                f"AppWorks Allegations_All (type IDs: {[t['id'] for t in allegation_types]})"
            ],
            "retrieved_at":  datetime.now(timezone.utc).isoformat(),
            "computed_by":   "AppWorks REST retrieval",
        },
    }
