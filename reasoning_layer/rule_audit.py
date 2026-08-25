"""
Owns: assembling the Rule Audit inventory (Functional Specification D4,
GET /rule_audit/{case_id}) — a complete, per-rule listing of every
inferred fact for a case with full provenance, so an investigator can
review what the system found and why BEFORE deciding what to reject
through POST /reject_inference (rejection.py). D4's own "Why This
Endpoint Is Necessary" note is explicit: "The Rejection Handler only
works well if investigators can first see all inferred facts in one
place. Without this view, rejection decisions are made blind." This
module is that view — the UI's Reject buttons read their
match_id (or subject_id_a/subject_id_b/rule_id/relationship_type)
parameters straight off entries returned here (or off
fraud_network.py's edges, for the network-membership rules; both
surfaces use the same field names on purpose so the UI never has to
translate between them).

WHY THIS IS A SEPARATE MODULE FROM rules_fired.py, NOT A REUSE OF IT:
Same reasoning report_generation.py's own docstring gives for the same
question. rules_fired.py assembles a fixed 14-entry, per-RULE
AGGREGATE (count + one summarised confidence) for the pipeline's own
run-scoped output contract (Functional Spec A.4) — it is explicitly
"assembled in exactly one place and never reconstructed by a caller,"
and this module does not touch it. D4 needs the opposite grain:
per-INSTANCE detail (which specific subject pair, which specific
timestamp) across the case's full subject scope, standalone — callable
any time an investigator opens the review panel, not only inside a
pipeline run. So this module runs its own read.

Does NOT own: rule execution (rule_engine.py), rule content
(rules/*.cypher), the rules_fired aggregate (rules_fired.py), or any
write.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from reasoning_layer import rule_registry
from reasoning_layer.case_staleness import get_last_inference_change_at_raw
from reasoning_layer.neo4j_client import get_session
from reasoning_layer.rejection import build_match_id
from reasoning_layer.scope import resolve_scope
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

from reasoning_layer.rule_audit_queries import PRIMARY_SUBJECT_QUERY, PROP_QUERIES, REL_QUERIES


def _envelope(result: Dict[str, Any]) -> dict:
    return {
        "result": result,
        "provenance": graph_provenance("reasoning_layer.rule_audit.get_rule_audit"),
    }


def get_rule_audit(case_id: str) -> dict:
    """
    Assemble the complete rule-by-rule inference inventory for a case
    (D4). Standalone GET — resolves its own primary subject and scope
    from the graph rather than depending on a live pipeline run.

    Args:
        case_id: required, non-empty.

    Returns (inside the standard {result, provenance} envelope):
        {
          "case_id": ..., "primary_subject_id": ...,
          "rules": [
            {
              "rule_id": ..., "rule_description": ...,
              "fired": bool,
              "inferred_relationships": [
                {subject_id_a, subject_id_b, relationship_type,
                 confidence, asserted_at, corroborated,
                 status: "active" | "rejected", match_id,
                 # AI-30/AI-31: cascade attribution — populated for
                 # Rule_02/04/06/08/09/13 rows (every rule cascade.py can
                 # ever auto-invalidate/reinstate — Rule_09's are always
                 # null, since it can never itself be a downstream
                 # target; see reasoning_layer/cascade.py's
                 # DOWNSTREAM_DEPENDENTS/_NETWORK_TYPE_BY_RULE_ID
                 # comments). null on every other rule's rows, which
                 # have no cascade concept of their own. When and by
                 # whom this fact was most recently auto-invalidated (if
                 # currently rejected because of that) or reinstated (if
                 # currently active again after having been
                 # auto-invalidated) — the exact fields a manual
                 # rejection's own POST /reject_inference /
                 # /revert_rejection response already returns
                 # (investigator_id, changed_at, rule_id), mirrored here
                 # for a cascaded fact so a caller viewing this later
                 # (not just at the moment of the original reject/revert
                 # call) still sees who/when/which-rule.
                 auto_invalidated: bool | None,
                 invalidated_by_rule_id: str | None,
                 invalidated_reason: str | None,
                 invalidated_by_investigator: str | None,
                 invalidated_at: str | None,
                 reinstated_by_rule_id: str | None,
                 reinstated_reason: str | None,
                 reinstated_by_investigator: str | None,
                 reinstated_at: str | None}
              ],
            }
          ],
          # AI-31: case-wide graph-change staleness signal — see
          # reasoning_layer/rejection.py's _touch_case_last_inference_change.
          # None until the first reject/revert call for this case.
          "last_inference_change_at": str | None,
        }

    Every rejectable rule_id from rejection.RULE_IDS_REJECTABLE is
    always present, fired or not — the same "fixed-shape contract, not
    a list of hits" discipline rules_fired.py documents, so a consumer
    iterating this can rely on every rule being present.

    A case whose Subject has not appeared in the graph yet (ETL/pipeline
    never ran) is not an error: primary_subject_id is None and every
    rule reports fired=false with an empty inferred_relationships list.

    Raises:
        ValueError: case_id missing or blank.
        GraphUnavailableError / Neo4jError: propagated unchanged.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("get_rule_audit requires a non-empty case_id")
    case_id = str(case_id).strip()

    rule_names = rule_registry.get_rule_names()

    with get_session() as session:
        primary_record = session.run(PRIMARY_SUBJECT_QUERY, case_id=case_id).single()

    primary_subject_id = primary_record["primary_subject_id"] if primary_record else None
    # AI-31: case-wide graph-change staleness signal, read via the
    # shared reasoning_layer.case_staleness reader (also used by AI-32's
    # narrative-staleness check) rather than a second private query
    # here — deliberately its own read, independent of whether a
    # primary subject has been flagged yet: a Case node can exist (and
    # have already been reject/revert-touched) before ETL has flagged
    # is_primary on any Subject, and this signal must not silently
    # disappear for that window.
    last_inference_change_at = get_last_inference_change_at_raw(case_id)

    if primary_subject_id:
        scope = resolve_scope(case_id=case_id, subject_id=primary_subject_id)
    else:
        logger.warning(
            "get_rule_audit: case_id=%s has no Subject flagged is_primary — "
            "has ETL run for this case? Returning an empty audit.",
            case_id,
        )
        scope = {"scope_subject_ids": [], "scope_case_ids": [case_id]}

    rules: List[Dict[str, Any]] = []
    with get_session() as session:
        for rule_id in rule_registry.ALL_RULE_IDS:
            if rule_id == rule_registry.MODIFIER_RULE_ID:
                # Rule 14 is a confidence modifier on existing edges, not
                # an independently rejectable/auditable fact — see
                # rejection.py's module docstring for the same exclusion.
                continue

            query = REL_QUERIES.get(rule_id) or PROP_QUERIES.get(rule_id)
            rows = session.run(
                query,
                scope_subject_ids=scope["scope_subject_ids"],
                scope_case_ids=scope.get("scope_case_ids", [case_id]),
                case_id=case_id,
                subject_id=primary_subject_id,
            ).data()

            inferred_relationships = [
                {
                    "subject_id_a": row["subject_id_a"],
                    "subject_id_b": row["subject_id_b"],
                    "relationship_type": row["relationship_type"],
                    "confidence": row["confidence"] or "Unresolved",
                    "asserted_at": row["asserted_at"],
                    "corroborated": bool(row["corroborated"]),
                    "status": row["status"] or "active",
                    # v3 instance-level Reject/Revert contract (AI-28/
                    # AI-33): the UI reads this straight off the row and
                    # echoes it back on POST /reject_inference or
                    # /revert_rejection, rather than reconstructing
                    # subject_id_a/subject_id_b/rule_id itself. Present
                    # for every row here, since every rule_id this loop
                    # visits is in RULE_IDS_REJECTABLE (rule_registry.
                    # MODIFIER_RULE_ID — the one rule_id excluded above —
                    # has no rejectable instance at all).
                    "match_id": build_match_id(rule_id, row["subject_id_a"], row["subject_id_b"]),
                    # AI-30/AI-31: cascade attribution — who auto-
                    # invalidated/reinstated this fact, which upstream
                    # rule triggered it, when, and why. Present for
                    # Rule_02/04/06/08/09/13 rows (the only queries above
                    # that RETURN these columns; Rule_09's are always
                    # null — see that query's own comment for why);
                    # .get(...) rather than row[...] is what makes every
                    # other rule's rows degrade to None instead of a
                    # KeyError.
                    "auto_invalidated": row.get("auto_invalidated"),
                    "invalidated_by_rule_id": row.get("invalidated_by_rule_id"),
                    "invalidated_reason": row.get("invalidated_reason"),
                    "invalidated_by_investigator": row.get("invalidated_by_investigator"),
                    "invalidated_at": row.get("invalidated_at"),
                    "reinstated_by_rule_id": row.get("reinstated_by_rule_id"),
                    "reinstated_reason": row.get("reinstated_reason"),
                    "reinstated_by_investigator": row.get("reinstated_by_investigator"),
                    "reinstated_at": row.get("reinstated_at"),
                }
                for row in rows
                if row["subject_id_a"] is not None
            ]
            rules.append(
                {
                    "rule_id": rule_id,
                    "rule_description": rule_names.get(rule_id, rule_id),
                    "fired": len(inferred_relationships) > 0,
                    "inferred_relationships": inferred_relationships,
                }
            )

    result = {
        "case_id": case_id,
        "primary_subject_id": primary_subject_id,
        "rules": rules,
        # AI-31: case-wide graph-change staleness signal — see
        # _CASE_STALENESS_QUERY above and reasoning_layer/rejection.py's
        # _touch_case_last_inference_change. None until the first
        # reject_inference/revert_rejection call for this case.
        "last_inference_change_at": last_inference_change_at,
    }
    logger.info(
        "get_rule_audit: case_id=%s primary_subject_id=%s rules_fired=%d/%d",
        case_id,
        primary_subject_id,
        sum(1 for r in rules if r["fired"]),
        len(rules),
    )
    return _envelope(result)