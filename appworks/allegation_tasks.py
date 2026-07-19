# appworks/allegation_tasks.py
# ----------------------------------------------------------------
# Agent 5 support: BSI standard investigative task catalogue
# ----------------------------------------------------------------
# Backs the get_allegation_type_tasks tool (AI-16 / Section 8.5).
#
# The Investigation Plan agent selects its steps from two independent
# candidate pools:
#   * catalog_tasks    — BSI's configured task types. That is what this
#                        module fetches, from the AppWorks
#                        AllegationTypeTask catalogue.
#   * rule_aware_tasks — recommendations derived from which inference rules
#                        fired (reasoning_layer/investigation_tasks.py).
#
# Keeping them in separate modules matters: the catalogue is AppWorks'
# system of record and changes when BSI configures a task, while the rule
# mapping is Phase 2 reasoning. Neither may silently overwrite the other.
#
# SCOPE OF THE UPSTREAM ENDPOINT: AllegationTypeTask_ManageAllegationTypeTasks
# returns a FLAT, GLOBAL list of task types. Each row carries only TaskName,
# AllegationTypeTask_IsDefaultTask and Show_IN_UI — there is no allegation
# type on the row and no relationship link to one. This module therefore
# returns the catalogue as-is and does NOT pretend to filter by allegation
# type: inventing an association AppWorks does not publish would put tasks
# in front of an investigator under a false justification. The allegation
# types on the case are echoed back as requested_types for context only.
# ----------------------------------------------------------------

import logging
from typing import Any, Dict, List, Optional

from appworks.appworks_paths import AppWorksPaths
from appworks.appworks_utils import extract_id_from_href
from utils.provenance import ProvenanceTracker

logger = logging.getLogger(__name__)

_LIST_KEY = "AllegationTypeTask_ManageAllegationTypeTasks"


def _normalize(value: Any) -> str:
    return str(value or "").strip()


def get_allegation_type_tasks(
    allegation_types: Optional[List[str]] = None, **kwargs
) -> Dict[str, Any]:
    """
    Return BSI's configured investigative task catalogue.

    Args:
        allegation_types: the allegation type labels on the current case.
            Echoed back as requested_types for context. The upstream
            catalogue is global and carries no allegation-type association,
            so this does not filter the result — see the module note.

    Returns the standard {result, provenance} envelope:
        result.catalog_tasks   — [{task_id, task_type, is_default_task,
                                   source: "catalog"}]
        result.default_tasks   — the subset BSI marks as default, i.e. the
                                 standard opening set for an investigation
        result.requested_types — the allegation types the caller supplied
        result.total_tasks     — len(catalog_tasks)

    Tasks hidden in the BSI UI (Show_IN_UI false) are excluded: a task BSI
    has withdrawn from its own interface should not be recommended here.

    A catalogue fetch failure degrades to an empty task list rather than
    raising — the Investigation Plan must still be produced from rule-aware
    tasks and LLM synthesis when AppWorks is unavailable.
    """
    requested_types = [t for t in (_normalize(x) for x in (allegation_types or [])) if t]
    logger.info(
        "get_allegation_type_tasks: requested_types=%s",
        requested_types or "(none supplied)",
    )

    tracker = ProvenanceTracker("Catalog", "AllegationTypeTask")

    try:
        from appworks.appworks_auth import fetch
        raw = fetch(AppWorksPaths.AllegationTypeTask.manage_allegation_type_tasks())
        items = raw if isinstance(raw, list) else raw.get("_embedded", {}).get(_LIST_KEY, [])
    except Exception as exc:  # noqa: BLE001 — see docstring: degrade, don't fail the plan
        logger.error("get_allegation_type_tasks: catalogue fetch failed: %s", exc)
        items = []

    catalog_tasks: List[Dict[str, Any]] = []
    seen_task_ids = set()

    for item in items:
        props = item.get("Properties", {})
        task_name = _normalize(props.get("TaskName"))
        if not task_name:
            continue

        # Respect BSI's own visibility flag. Absent means visible.
        if props.get("Show_IN_UI") is False:
            continue

        href = item.get("_links", {}).get("item", {}).get("href", "")
        task_id = extract_id_from_href(href)
        if task_id and task_id in seen_task_ids:
            continue
        if task_id:
            seen_task_ids.add(task_id)
            tracker.add_source("AllegationTypeTask", task_id)

        catalog_tasks.append({
            "task_id": task_id,
            "task_type": task_name,
            "is_default_task": bool(props.get("AllegationTypeTask_IsDefaultTask", False)),
            # Section 8.5 / AI-16: every task declares its origin, so a step
            # selected from the catalogue stays distinguishable from one
            # derived from a fired rule.
            "source": "catalog",
        })

    # Deterministic order: BSI's default tasks first (they are the standard
    # opening set), then alphabetically. Two runs over an unchanged
    # catalogue return identical output.
    catalog_tasks.sort(key=lambda t: (not t["is_default_task"], t["task_type"].lower()))
    default_tasks = [t["task_type"] for t in catalog_tasks if t["is_default_task"]]

    logger.info(
        "get_allegation_type_tasks: catalog_tasks=%d default_tasks=%d",
        len(catalog_tasks), len(default_tasks),
    )

    return {
        "result": {
            "catalog_tasks": catalog_tasks,
            "default_tasks": default_tasks,
            "requested_types": requested_types,
            "total_tasks": len(catalog_tasks),
        },
        "provenance": tracker.get_provenance_block(),
    }