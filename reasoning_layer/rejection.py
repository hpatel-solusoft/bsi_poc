"""
Owns: the WRITE side of the Human-in-the-Loop rejection mechanism
(Developer Specification Section 5.2; Functional Specification D2,
POST /reject_inference). Called by exactly one route,
api/server.py's POST /reject_inference — never by an LLM, never
dispatcher-routed, never registered in manifest.yaml (Neo4j write, not
an AppWorks call; the manifest governs the latter only).

reasoning_layer/graph_load.py already owns the READ side of this
mechanism — every rule file's own NOT EXISTS { MATCH (:Rejection ...) }
guard is the other read side. This module is the one and only place a
:Rejection node is ever created.

CONTRACT (v2 — case + rule level, not edge level):
The frontend can only ever POST {case_id, rule_id, reason,
investigator_id} — it has no
way to know, and should never have to know, the internal subject
pairing a rule matched on. So "reject" here means: reject every
CURRENTLY ACTIVE fact this rule_id produced within this case's
reasoning scope (the same primary-subject + one-hop population
reasoning_layer/scope.py resolves, and the same population
reasoning_layer/rule_audit.py already shows the investigator before
they click Reject — so what gets rejected is exactly what was visible
on screen). This is a bulk operation over however many instances that
rule fired in this case (zero, one, or many), not a lookup of one
specific instance.

Per-instance suppression still happens underneath, one :Rejection node
per instance, keyed exactly the way it always was (see the encoding
table below) — so every rule file's own guard, graph_load.py's read
side, and revert_rejection all keep working unchanged. What changed is
only how the SET of instances to reject is *located*: previously the
caller supplied subject_id_a/subject_id_b directly; now this module
finds every active instance itself and rejects all of them in one
transaction.

THE HARD PART, AND WHY IT NEEDS ITS OWN MODULE:
What an investigator's case+rule click does NOT tell this module is
the internal :Rejection key encoding each rule file's own guard checks
against — and that encoding is NOT uniform:

  - SHARES_EMPLOYER_WITH / SHARES_ADDRESS_WITH / SHARES_ALIAS_PATTERN_WITH
    (Rules 1/3/5): from_key/to_key = the two subject_ids, unordered.
  - MEMBER_OF_FRAUD_NETWORK (Rules 2/4/6/9): from_key = one subject_id,
    to_key = "<network_type>:<network_key>" — a composite string keyed
    off properties that live on the :FraudNetwork node, not on the
    request. Four different rules, four different network_type prefixes
    ("Employer" / "Address" / "Identity" / "CheckSplit"), NONE of which
    are safe to hardcode a second time here. So this module reads
    network_type/network_key off the live :FraudNetwork node each
    matched subject is actually connected to.
  - HAS_PRIOR_GUILTY_CASE / APPEARS_IN_CASE (Rules 7/10): subject -> case.
    NOTE the target case is NOT necessarily $case_id (Rule 7's prior
    case, Rule 10's merge target) — scoping is by subject, not by
    filtering the target case.
  - CASE_RISK_ESCALATION / FASTTRACK_RECOMMENDATION (Rules 8/13): these
    are properties on the :Case node, not a relationship at all, and by
    construction there is at most one active instance per case — so
    case_id alone already disambiguates these two rule families
    completely.
  - WAGE_CORROBORATION (Rule 12): a property on every :Allegation the
    wage record corroborates for this case — there can be more than one
    allegation, so this is genuinely a bulk case.
  - CROSS_CASE_HUB (Rule 11): a property on the :Subject node itself,
    scoped by the case's reasoning scope.

_RULE_SPECS below is the one and only place this per-rule-type encoding
knowledge lives. Every other module that ever needs to correlate a
:Rejection back to what it suppressed (report_generation.py,
fraud_network.py, rule_audit.py) reads the already-written from_key/
to_key off the :Rejection node — none of them re-derive this encoding.

ATTRIBUTION NOTE: this platform has no authenticated session yet.
reject_inference and revert_rejection both now take investigator_id as
a required argument, and every :Rejection node written (and every
per-family rejected_by-equivalent field) is stamped with that value
instead of a fixed placeholder. THIS IS A KNOWN GAP, NOT A DESIGN
CHOICE: investigator_id is presently trusted as supplied by the
caller (the frontend's currently-logged-in investigator, sent in the
request body). Once real auth exists, wire the authenticated user id
through here from the request's session/JWT instead of trusting a
client-supplied body field for it.

Does NOT own: rule execution (rule_engine.py), rule content
(rules/*.cypher), or reading back rejected facts for display
(report_generation.py, rule_audit.py, fraud_network.py all do their own
reads).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.case_store import update_rules_fired_instance_status
from reasoning_layer.neo4j_client import get_session
from reasoning_layer.scope import resolve_scope
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

class InferenceNotFoundError(LookupError):
    """
    Raised when no currently-ACTIVE inferred fact matches rule_id within
    case_id's reasoning scope.

    Two honest reasons this happens, neither a server error:
      1. Everything this rule found in this case was already rejected
         (a second click on an already-rejected rule).
      2. The rule never actually fired for this case at all.
    The route maps this to HTTP 404, not 500 — the request was
    understood; there is just nothing live to reject.
    """


# --- Families: how "the rule's findings" are located and marked ---
_FAMILY_SYMMETRIC_EDGE = "symmetric_edge"       # Rules 1, 3, 5
_FAMILY_SUBJECT_CASE_EDGE = "subject_case_edge"  # Rules 7, 10
_FAMILY_NETWORK_EDGE = "network_edge"           # Rules 2, 4, 6, 9
_FAMILY_SUBJECT_FLAG = "subject_flag"           # Rule 11
_FAMILY_CASE_FLAG = "case_flag"                 # Rules 8, 13
_FAMILY_ALLEGATION_FLAG = "allegation_flag"     # Rule 12


@dataclass(frozen=True)
class _RuleSpec:
    family: str
    relationship_type: str  # the client-facing type name D2's contract uses


# The one and only per-rule encoding table (see module docstring).
# relationship_type values match exactly what report_generation.py and
# every rule file's own Rejection guard already use, so a Rejection
# written here is read back correctly everywhere else without change.
_RULE_SPECS: Dict[str, _RuleSpec] = {
    "Rule_01_Shared_Employer": _RuleSpec(_FAMILY_SYMMETRIC_EDGE, "SHARES_EMPLOYER_WITH"),
    "Rule_03_Shared_Address": _RuleSpec(_FAMILY_SYMMETRIC_EDGE, "SHARES_ADDRESS_WITH"),
    "Rule_05_Alias_Identity": _RuleSpec(_FAMILY_SYMMETRIC_EDGE, "SHARES_ALIAS_PATTERN_WITH"),
    "Rule_07_Prior_Guilty": _RuleSpec(_FAMILY_SUBJECT_CASE_EDGE, "HAS_PRIOR_GUILTY_CASE"),
    "Rule_10_Merged_Case_Propagation": _RuleSpec(_FAMILY_SUBJECT_CASE_EDGE, "APPEARS_IN_CASE"),
    "Rule_02_Employer_Fraud_Network": _RuleSpec(_FAMILY_NETWORK_EDGE, "MEMBER_OF_FRAUD_NETWORK"),
    "Rule_04_Address_Fraud_Network": _RuleSpec(_FAMILY_NETWORK_EDGE, "MEMBER_OF_FRAUD_NETWORK"),
    "Rule_06_Identity_Fraud_Network": _RuleSpec(_FAMILY_NETWORK_EDGE, "MEMBER_OF_FRAUD_NETWORK"),
    "Rule_09_PCA_CheckSplit": _RuleSpec(_FAMILY_NETWORK_EDGE, "MEMBER_OF_FRAUD_NETWORK"),
    "Rule_11_Cross_Case_Hub": _RuleSpec(_FAMILY_SUBJECT_FLAG, "CROSS_CASE_HUB"),
    "Rule_08_Recidivist_Escalation": _RuleSpec(_FAMILY_CASE_FLAG, "CASE_RISK_ESCALATION"),
    "Rule_13_FastTrack_Escalation": _RuleSpec(_FAMILY_CASE_FLAG, "FASTTRACK_RECOMMENDATION"),
    "Rule_12_SLAM_Wage_Corroboration": _RuleSpec(_FAMILY_ALLEGATION_FLAG, "WAGE_CORROBORATION"),
    # Rule_14 is a cross-cutting confidence modifier on an existing edge
    # (Developer Spec Section 6.0), not an independent inferred fact —
    # rejecting the base edge already removes its Rule 14 elevation.
    # There is deliberately no entry for it here.
}

RULE_IDS_REJECTABLE: List[str] = sorted(_RULE_SPECS)


def _build_cached_instance_matcher(spec: _RuleSpec, items: List[Dict[str, Optional[str]]]):
    """
    Build the predicate core.case_store.update_rules_fired_instance_status
    uses to find, in the CACHED rules_fired snapshot's "instances" list,
    the same instances `items` (this call's subject_id_a/subject_id_b rows,
    straight from _locate_and_reject / _locate_and_revert) just changed in
    Neo4j. This is the one place that translates the per-family encoding in
    _RULE_SPECS/subject_id_a/subject_id_b onto the field names
    reasoning_layer/rules_fired.py's _instance() puts on a cached instance
    (subject_id, related_subject_id, related_case_id, related_network_key)
    — mirroring the module docstring's per-family knowledge, just aimed at
    the cache instead of Neo4j.

    Matches against the whole `items` list at once (not one item at a
    time) because reject/revert are bulk operations — one rule_id click
    can affect several instances in a single call.
    """
    if spec.family == _FAMILY_SYMMETRIC_EDGE:
        pairs = {frozenset((it["subject_id_a"], it["subject_id_b"])) for it in items}
        return lambda inst: frozenset(
            (inst.get("subject_id"), inst.get("related_subject_id"))
        ) in pairs

    if spec.family == _FAMILY_SUBJECT_CASE_EDGE:
        pairs = {(it["subject_id_a"], it["subject_id_b"]) for it in items}
        return lambda inst: (inst.get("subject_id"), inst.get("related_case_id")) in pairs

    if spec.family == _FAMILY_NETWORK_EDGE:
        # subject_id_b is "<network_type>:<network_key>"; the cached instance
        # only carries the bare network_key (related_network_key), and the
        # row is collapsed to ONE representative subject per network (see
        # rules_fired.py's Rule_02/04/06/09 queries), so network_key alone
        # is the reliable match key — not subject_id.
        network_keys = {
            (it["subject_id_b"].split(":", 1)[1] if it["subject_id_b"] and ":" in it["subject_id_b"]
             else it["subject_id_b"])
            for it in items
        }
        return lambda inst: inst.get("related_network_key") in network_keys

    if spec.family == _FAMILY_SUBJECT_FLAG:
        subject_ids = {it["subject_id_a"] for it in items}
        return lambda inst: inst.get("subject_id") in subject_ids

    if spec.family == _FAMILY_CASE_FLAG:
        # At most one active instance per case (module docstring) — the
        # cached instance for these rules carries related_case_id but no
        # subject_id, so case_id is already the full disambiguator.
        case_ids = {it["subject_id_b"] for it in items}
        return lambda inst: inst.get("related_case_id") in case_ids

    if spec.family == _FAMILY_ALLEGATION_FLAG:
        # The cached instance has no allegation_id field to match against
        # (rules_fired.py's Rule_12 row never surfaces one — see
        # _INSTANCE_KEYS), so this matches every cached instance for the
        # rejected/reverted subject(s) within this rule_id — the same
        # bulk-by-subject scope _BULK_REJECT_ALLEGATION_FLAG itself uses.
        subject_ids = {it["subject_id_a"] for it in items}
        return lambda inst: inst.get("subject_id") in subject_ids

    return lambda inst: False  # pragma: no cover


# --- Cypher: one bulk locate-and-reject statement per family. Every SET
# is scoped to $rule_id AND status = "active" so a second reject on an
# already-rejected rule simply finds nothing (never a double-write),
# and every write happens inside the one session reject_inference uses
# so a caller never observes a partial rejection. ---

_PRIMARY_SUBJECT_QUERY = """
MATCH (s:Subject)-[r:APPEARS_IN_CASE]->(:Case {case_id: $case_id})
WHERE r.is_primary = true
RETURN s.subject_id AS primary_subject_id
LIMIT 1
"""

_BULK_REJECT_SYMMETRIC_EDGE = """
MATCH (a:Subject)-[r:{rel_type}]-(b:Subject)
WHERE r.source_rule = $rule_id AND r.status = "active"
  AND a.subject_id < b.subject_id
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
SET r.status = "rejected",
    r.rejection_reason = $reason,
    r.rejected_by = $investigator_id,
    r.rejected_at = $rejected_at
RETURN a.subject_id AS subject_id_a, b.subject_id AS subject_id_b
"""

_BULK_REJECT_SUBJECT_CASE_EDGE = """
MATCH (a:Subject)-[r:{rel_type}]->(c:Case)
WHERE r.source_rule = $rule_id AND r.status = "active"
  AND a.subject_id IN $scope_subject_ids
SET r.status = "rejected",
    r.rejection_reason = $reason,
    r.rejected_by = $investigator_id,
    r.rejected_at = $rejected_at
RETURN a.subject_id AS subject_id_a, c.case_id AS subject_id_b
"""

# Rejects every currently-active membership edge this rule wrote for any
# in-scope subject — each row is one subject's membership in one
# network, mirroring exactly what the Fraud Network / Rule Audit screens
# show as separate rejectable rows.
_BULK_REJECT_NETWORK_EDGE = """
MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE r.source_rule = $rule_id AND r.status = "active"
  AND a.subject_id IN $scope_subject_ids
SET r.status = "rejected",
    r.rejection_reason = $reason,
    r.rejected_by = $investigator_id,
    r.rejected_at = $rejected_at
RETURN a.subject_id AS subject_id_a, n.network_type AS network_type, n.network_key AS network_key
"""

_BULK_REJECT_SUBJECT_FLAG = """
MATCH (a:Subject)
WHERE a.cross_case_source_rule = $rule_id AND a.is_cross_case = true
  AND a.subject_id IN $scope_subject_ids
SET a.is_cross_case = false,
    a.cross_case_rejected = true,
    a.cross_case_rejection_reason = $reason,
    a.cross_case_rejected_by = $investigator_id,
    a.cross_case_rejected_at = $rejected_at
RETURN a.subject_id AS subject_id_a
"""

# Case-level flags: at most one active instance can ever exist per
# case_id (the rule SETs a fixed property name on the one :Case node),
# so case_id alone fully disambiguates these two families — no bulk
# fan-out is possible here, but the shape is kept identical to the
# other families for a uniform caller.
_BULK_REJECT_CASE_FLAG: Dict[str, str] = {
    "Rule_08_Recidivist_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.risk_escalation_source_rule = $rule_id
          AND c.risk_escalation_status = "active"
        SET c.risk_escalation_status = "rejected",
            c.risk_escalation_rejection_reason = $reason,
            c.risk_escalation_rejected_by = $investigator_id,
            c.risk_escalation_rejected_at = $rejected_at
        RETURN c.risk_escalation_subject_id AS subject_id_a
    """,
    # Rule 13 does not stamp an escalating-subject id onto :Case (see
    # rules/wave2/rule_13_fasttrack_escalation.cypher — it is scoped to
    # the PRIMARY subject only), so subject_id_a is supplied by the
    # caller from the case's own primary subject, resolved from the
    # graph rather than trusted from client input.
    "Rule_13_FastTrack_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.fasttrack_recommendation_rule = $rule_id
          AND c.fasttrack_recommendation_status = "active"
        SET c.fasttrack_recommendation_status = "rejected",
            c.fasttrack_recommendation_rejection_reason = $reason,
            c.fasttrack_recommendation_rejected_by = $investigator_id,
            c.fasttrack_recommendation_rejected_at = $rejected_at
        RETURN $subject_id_a AS subject_id_a
    """,
}

_BULK_REJECT_ALLEGATION_FLAG = """
MATCH (c:Case {case_id: $case_id})-[:HAS_ALLEGATION]->(al:Allegation)
      -[:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject)
WHERE al.wage_corroboration_rule = $rule_id AND al.wage_corroboration_status = "active"
SET al.wage_corroboration_status = "rejected",
    al.wage_corroboration_rejection_reason = $reason,
    al.wage_corroboration_rejected_by = $investigator_id,
    al.wage_corroboration_rejected_at = $rejected_at
RETURN a.subject_id AS subject_id_a, al.allegation_id AS allegation_id
"""

_MERGE_REJECTION = """
MERGE (rej:Rejection {
    relationship_type: $relationship_type,
    from_key: $from_key,
    to_key: $to_key
})
ON CREATE SET rej.status = "active",
              rej.rejected_by = $investigator_id,
              rej.rejected_at = $rejected_at,
              rej.reason = $reason,
              rej.rule_id = $rule_id,
              rej.case_id = $case_id
RETURN elementId(rej) AS rejection_id
"""


def _resolve_case_scope(session, case_id: str) -> Dict[str, Any]:
    """
    Same scope a Rule Audit read of this case would use (D4) — primary
    subject plus reasoning_layer/scope.py's one-hop population. Rejecting
    against this exact scope means "reject every finding this rule
    produced that the investigator could actually see for this case",
    never a wider, silent, whole-graph sweep.
    """
    primary_record = session.run(_PRIMARY_SUBJECT_QUERY, case_id=case_id).single()
    primary_subject_id = primary_record["primary_subject_id"] if primary_record else None
    if primary_subject_id:
        scope = resolve_scope(case_id=case_id, subject_id=primary_subject_id)
    else:
        logger.warning(
            "reject_inference: case_id=%s has no Subject flagged is_primary — "
            "has ETL run for this case? Treating scope as empty.", case_id,
        )
        scope = {"scope_subject_ids": [], "scope_case_ids": [case_id]}
    scope["primary_subject_id"] = primary_subject_id
    return scope


def _locate_and_reject(
    session, rule_id: str, spec: _RuleSpec, case_id: str, scope: Dict[str, Any],
    reason: str, investigator_id: str, rejected_at: str,
) -> List[Dict[str, Optional[str]]]:
    """
    Runs the one bulk locate-and-SET statement for this rule's family and
    returns one dict per instance rejected, each carrying subject_id_a /
    subject_id_b (for the response) and from_key/to_key (the exact
    :Rejection key every rule file's own guard already checks against).
    """
    scope_subject_ids = scope["scope_subject_ids"]

    if spec.family == _FAMILY_SYMMETRIC_EDGE:
        query = _BULK_REJECT_SYMMETRIC_EDGE.format(rel_type=spec.relationship_type)
        rows = session.run(
            query, rule_id=rule_id, scope_subject_ids=scope_subject_ids,
            reason=reason, investigator_id=investigator_id, rejected_at=rejected_at,
        ).data()
        return [
            {
                "subject_id_a": r["subject_id_a"], "subject_id_b": r["subject_id_b"],
                "from_key": min(r["subject_id_a"], r["subject_id_b"]),
                "to_key": max(r["subject_id_a"], r["subject_id_b"]),
            }
            for r in rows
        ]

    if spec.family == _FAMILY_SUBJECT_CASE_EDGE:
        query = _BULK_REJECT_SUBJECT_CASE_EDGE.format(rel_type=spec.relationship_type)
        rows = session.run(
            query, rule_id=rule_id, scope_subject_ids=scope_subject_ids,
            reason=reason, investigator_id=investigator_id, rejected_at=rejected_at,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": r["subject_id_b"],
             "from_key": r["subject_id_a"], "to_key": r["subject_id_b"]}
            for r in rows
        ]

    if spec.family == _FAMILY_NETWORK_EDGE:
        rows = session.run(
            _BULK_REJECT_NETWORK_EDGE, rule_id=rule_id, scope_subject_ids=scope_subject_ids,
            reason=reason, investigator_id=investigator_id, rejected_at=rejected_at,
        ).data()
        return [
            {
                "subject_id_a": r["subject_id_a"],
                "subject_id_b": f'{r["network_type"]}:{r["network_key"]}',
                "from_key": r["subject_id_a"],
                "to_key": f'{r["network_type"]}:{r["network_key"]}',
            }
            for r in rows
        ]

    if spec.family == _FAMILY_SUBJECT_FLAG:
        rows = session.run(
            _BULK_REJECT_SUBJECT_FLAG, rule_id=rule_id, scope_subject_ids=scope_subject_ids,
            reason=reason, investigator_id=investigator_id, rejected_at=rejected_at,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": None,
             "from_key": r["subject_id_a"], "to_key": r["subject_id_a"]}
            for r in rows
        ]

    if spec.family == _FAMILY_CASE_FLAG:
        query = _BULK_REJECT_CASE_FLAG[rule_id]
        rows = session.run(
            query, rule_id=rule_id, case_id=case_id,
            subject_id_a=scope.get("primary_subject_id"),
            reason=reason, investigator_id=investigator_id, rejected_at=rejected_at,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": case_id,
             "from_key": r["subject_id_a"], "to_key": case_id}
            for r in rows
        ]

    if spec.family == _FAMILY_ALLEGATION_FLAG:
        rows = session.run(
            _BULK_REJECT_ALLEGATION_FLAG, rule_id=rule_id, case_id=case_id,
            reason=reason, investigator_id=investigator_id, rejected_at=rejected_at,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": r["allegation_id"],
             "from_key": r["subject_id_a"], "to_key": r["allegation_id"]}
            for r in rows
        ]

    raise ValueError(f"Unhandled rule family '{spec.family}' for {rule_id}")  # pragma: no cover


def _envelope(result: Dict[str, Any]) -> dict:
    """Standard {result, provenance} envelope (Principle 8), identical in
    shape to every other direct-call reasoning_layer module."""
    return {
        "result": result,
        "provenance": graph_provenance(
            "reasoning_layer.rejection.reject_inference", ["Neo4j write — :Rejection"],
        ),
    }


def reject_inference(case_id: str, rule_id: str, reason: str, investigator_id: str) -> dict:
    """
    Record an investigator's decision to overrule every currently-active
    fact one rule produced for one case (Functional Specification D2,
    v2 contract — case_id + rule_id + reason + investigator_id, matching
    what the frontend can actually supply).

    A suppression, not a deletion: every underlying relationship/property
    this rule set to active within the case's reasoning scope is set to
    "rejected", never removed, and a permanent :Rejection audit record is
    written for each one. Future pipeline runs check for these records
    before re-asserting the same facts — every rule file's own guard
    already does this; this function is what makes a guard find
    something.

    Args:
        case_id: required. The case the rejection is issued from.
        rule_id: required. Which rule's output to reject, e.g.
            "Rule_01_Shared_Employer". Must be one of RULE_IDS_REJECTABLE.
        reason: required. Free-text reason — mandatory here because,
            unlike the old per-edge contract, this is a bulk action and
            the audit trail is the only record of why it was taken.
        investigator_id: required. Identifies who is rejecting — stamped
            onto every :Rejection node written (rejected_by / equivalent
            per-family field) instead of the previous fixed "unattributed"
            placeholder. See the module docstring's ATTRIBUTION NOTE —
            once real auth exists this should come from the authenticated
            session, not a client-supplied field.

    Returns (inside the standard {result, provenance} envelope):
        {"accepted": true, "case_id": ..., "rule_id": ...,
         "relationship_type": ..., "reason": ..., "investigator_id": ...,
         "rejected_count": N,
         "rejected_items": [{"subject_id_a": ..., "subject_id_b": ...}, ...],
         "rejected_at": ...}

    Raises:
        ValueError: case_id/rule_id/reason/investigator_id blank, or
            rule_id unknown.
        InferenceNotFoundError: nothing currently active matches rule_id
            within this case's scope — see the class docstring for the
            two honest reasons this happens. Mapped to HTTP 404 by the
            route, not 500.
        GraphUnavailableError / Neo4jError: propagated unchanged.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("reject_inference requires a non-empty case_id")
    if not rule_id or not str(rule_id).strip():
        raise ValueError("reject_inference requires a non-empty rule_id")
    if not reason or not str(reason).strip():
        raise ValueError("reject_inference requires a non-empty reason")
    if not investigator_id or not str(investigator_id).strip():
        raise ValueError("reject_inference requires a non-empty investigator_id")

    case_id = str(case_id).strip()
    rule_id = str(rule_id).strip()
    reason = str(reason).strip()
    investigator_id = str(investigator_id).strip()

    spec = _RULE_SPECS.get(rule_id)
    if spec is None:
        raise ValueError(
            f"Unknown or non-rejectable rule_id={rule_id!r}. "
            f"Must be one of: {RULE_IDS_REJECTABLE}"
        )

    rejected_at = datetime.now(timezone.utc).isoformat()

    with get_session() as session:
        scope = _resolve_case_scope(session, case_id)
        instances = _locate_and_reject(
            session, rule_id, spec, case_id, scope, reason, investigator_id, rejected_at,
        )

        if not instances:
            logger.info(
                "reject_inference: NOT FOUND case_id=%s rule_id=%s — "
                "no active fact in scope to reject", case_id, rule_id,
            )
            raise InferenceNotFoundError(
                f"No active inferred facts found for rule_id={rule_id!r} in "
                f"case_id={case_id!r}. It may already be rejected, or the rule "
                f"never fired for this case."
            )

        rejected_items = []
        for instance in instances:
            session.run(
                _MERGE_REJECTION,
                relationship_type=spec.relationship_type,
                from_key=instance["from_key"], to_key=instance["to_key"],
                investigator_id=investigator_id, rejected_at=rejected_at,
                reason=reason, rule_id=rule_id, case_id=case_id,
            )
            rejected_items.append({
                "subject_id_a": instance["subject_id_a"],
                "subject_id_b": instance["subject_id_b"],
            })

    logger.info(
        "reject_inference: REJECTED case_id=%s rule_id=%s relationship_type=%s "
        "investigator_id=%s count=%d",
        case_id, rule_id, spec.relationship_type, investigator_id, len(rejected_items),
    )

    # Sync the cached rules_fired snapshot (CS-4 + case_ai_summary_store) so
    # the stored JSON reflects this rejection's status + reason right away.
    # Best-effort and non-blocking (see update_rules_fired_instance_status'
    # docstring) — Neo4j above is already the authoritative write; a miss or
    # failure here never fails this request.
    update_rules_fired_instance_status(
        case_id=case_id,
        rule_id=rule_id,
        action="reject",
        investigator_id=investigator_id,
        reason=reason,
        timestamp=rejected_at,
        matches=_build_cached_instance_matcher(spec, rejected_items),
    )

    result = {
        "accepted": True,
        "case_id": case_id,
        "rule_id": rule_id,
        "relationship_type": spec.relationship_type,
        "reason": reason,
        "investigator_id": investigator_id,
        "rejected_count": len(rejected_items),
        "rejected_items": rejected_items,
        "rejected_at": rejected_at,
    }
    return _envelope(result)


# ---------------------------------------------------------------------------
# REVERT — undo a rejection (Case Summary "Revert" action)
# ---------------------------------------------------------------------------
# The exact inverse of the reject write above, built from the same
# _RULE_SPECS table and the same case-scoped bulk-locate approach, so the
# two can never disagree about where a given rule's state lives or which
# instances a case_id + rule_id click covers.
#
# Revert CLEARS the rejection marker rather than setting a "reverted"
# status: leaving status="reverted" behind would mean every rule file's
# own NOT EXISTS { MATCH (:Rejection ...) } guard, and every
# "status = active" filter, would still treat the fact as suppressed.
# Only restoring status to "active" and deleting the :Rejection node
# actually lets the rule fire again next run. The revert itself is
# still auditable though: reverted_by/revert_reason/reverted_at are
# SET on the same node/relationship (mirroring rejected_by/
# rejection_reason/rejected_at) so the graph carries a visible record
# of who re-approved it and why, the same way it does for a rejection.
_REVERT_SYMMETRIC_EDGE = """
MATCH (a:Subject)-[r:{rel_type}]-(b:Subject)
WHERE r.source_rule = $rule_id AND r.status = "rejected"
  AND a.subject_id < b.subject_id
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
SET r.status = "active",
    r.reverted_by = $investigator_id,
    r.revert_reason = $reason,
    r.reverted_at = $reverted_at
REMOVE r.rejection_reason, r.rejected_by, r.rejected_at
RETURN a.subject_id AS subject_id_a, b.subject_id AS subject_id_b
"""

_REVERT_SUBJECT_CASE_EDGE = """
MATCH (a:Subject)-[r:{rel_type}]->(c:Case)
WHERE r.source_rule = $rule_id AND r.status = "rejected"
  AND a.subject_id IN $scope_subject_ids
SET r.status = "active",
    r.reverted_by = $investigator_id,
    r.revert_reason = $reason,
    r.reverted_at = $reverted_at
REMOVE r.rejection_reason, r.rejected_by, r.rejected_at
RETURN a.subject_id AS subject_id_a, c.case_id AS subject_id_b
"""

_REVERT_NETWORK_EDGE = """
MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE r.source_rule = $rule_id AND r.status = "rejected"
  AND a.subject_id IN $scope_subject_ids
SET r.status = "active",
    r.reverted_by = $investigator_id,
    r.revert_reason = $reason,
    r.reverted_at = $reverted_at
REMOVE r.rejection_reason, r.rejected_by, r.rejected_at
RETURN a.subject_id AS subject_id_a, n.network_type AS network_type, n.network_key AS network_key
"""

_REVERT_SUBJECT_FLAG = """
MATCH (a:Subject)
WHERE a.cross_case_source_rule = $rule_id AND a.cross_case_rejected = true
  AND a.subject_id IN $scope_subject_ids
SET a.is_cross_case = true,
    a.cross_case_reverted_by = $investigator_id,
    a.cross_case_revert_reason = $reason,
    a.cross_case_reverted_at = $reverted_at
REMOVE a.cross_case_rejected, a.cross_case_rejection_reason,
       a.cross_case_rejected_by, a.cross_case_rejected_at
RETURN a.subject_id AS subject_id_a
"""

_REVERT_CASE_FLAG: Dict[str, str] = {
    "Rule_08_Recidivist_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.risk_escalation_source_rule = $rule_id
          AND c.risk_escalation_status = "rejected"
        SET c.risk_escalation_status = "active",
            c.risk_escalation_reverted_by = $investigator_id,
            c.risk_escalation_revert_reason = $reason,
            c.risk_escalation_reverted_at = $reverted_at
        REMOVE c.risk_escalation_rejection_reason, c.risk_escalation_rejected_by,
               c.risk_escalation_rejected_at
        RETURN c.risk_escalation_subject_id AS subject_id_a
    """,
    "Rule_13_FastTrack_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.fasttrack_recommendation_rule = $rule_id
          AND c.fasttrack_recommendation_status = "rejected"
        SET c.fasttrack_recommendation_status = "active",
            c.fasttrack_recommendation_reverted_by = $investigator_id,
            c.fasttrack_recommendation_revert_reason = $reason,
            c.fasttrack_recommendation_reverted_at = $reverted_at
        REMOVE c.fasttrack_recommendation_rejection_reason,
               c.fasttrack_recommendation_rejected_by,
               c.fasttrack_recommendation_rejected_at
        RETURN $subject_id_a AS subject_id_a
    """,
}

_REVERT_ALLEGATION_FLAG = """
MATCH (c:Case {case_id: $case_id})-[:HAS_ALLEGATION]->(al:Allegation)
      -[:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject)
WHERE al.wage_corroboration_rule = $rule_id AND al.wage_corroboration_status = "rejected"
SET al.wage_corroboration_status = "active",
    al.wage_corroboration_reverted_by = $investigator_id,
    al.wage_corroboration_revert_reason = $reason,
    al.wage_corroboration_reverted_at = $reverted_at
REMOVE al.wage_corroboration_rejection_reason, al.wage_corroboration_rejected_by,
       al.wage_corroboration_rejected_at
RETURN a.subject_id AS subject_id_a, al.allegation_id AS allegation_id
"""

# Deleting the :Rejection node is what actually lets the rule fire again on
# the next pipeline run — every rule file guards on its absence.
_DELETE_REJECTION = """
MATCH (rej:Rejection {relationship_type: $relationship_type,
                      from_key: $from_key, to_key: $to_key})
WHERE rej.rule_id = $rule_id AND rej.case_id = $case_id
DELETE rej
RETURN count(*) AS deleted
"""


def _locate_and_revert(
    session, rule_id: str, spec: _RuleSpec, case_id: str, scope: Dict[str, Any],
    investigator_id: str, reason: str, reverted_at: str,
) -> List[Dict[str, Optional[str]]]:
    scope_subject_ids = scope["scope_subject_ids"]
    audit_params = dict(investigator_id=investigator_id, reason=reason, reverted_at=reverted_at)

    if spec.family == _FAMILY_SYMMETRIC_EDGE:
        query = _REVERT_SYMMETRIC_EDGE.format(rel_type=spec.relationship_type)
        rows = session.run(
            query, rule_id=rule_id, scope_subject_ids=scope_subject_ids, **audit_params,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": r["subject_id_b"],
             "from_key": min(r["subject_id_a"], r["subject_id_b"]),
             "to_key": max(r["subject_id_a"], r["subject_id_b"])}
            for r in rows
        ]

    if spec.family == _FAMILY_SUBJECT_CASE_EDGE:
        query = _REVERT_SUBJECT_CASE_EDGE.format(rel_type=spec.relationship_type)
        rows = session.run(
            query, rule_id=rule_id, scope_subject_ids=scope_subject_ids, **audit_params,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": r["subject_id_b"],
             "from_key": r["subject_id_a"], "to_key": r["subject_id_b"]}
            for r in rows
        ]

    if spec.family == _FAMILY_NETWORK_EDGE:
        rows = session.run(
            _REVERT_NETWORK_EDGE, rule_id=rule_id, scope_subject_ids=scope_subject_ids,
            **audit_params,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"],
             "subject_id_b": f'{r["network_type"]}:{r["network_key"]}',
             "from_key": r["subject_id_a"],
             "to_key": f'{r["network_type"]}:{r["network_key"]}'}
            for r in rows
        ]

    if spec.family == _FAMILY_SUBJECT_FLAG:
        rows = session.run(
            _REVERT_SUBJECT_FLAG, rule_id=rule_id, scope_subject_ids=scope_subject_ids,
            **audit_params,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": None,
             "from_key": r["subject_id_a"], "to_key": r["subject_id_a"]}
            for r in rows
        ]

    if spec.family == _FAMILY_CASE_FLAG:
        query = _REVERT_CASE_FLAG[rule_id]
        rows = session.run(
            query, rule_id=rule_id, case_id=case_id,
            subject_id_a=scope.get("primary_subject_id"), **audit_params,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": case_id,
             "from_key": r["subject_id_a"], "to_key": case_id}
            for r in rows
        ]

    if spec.family == _FAMILY_ALLEGATION_FLAG:
        rows = session.run(
            _REVERT_ALLEGATION_FLAG, rule_id=rule_id, case_id=case_id, **audit_params,
        ).data()
        return [
            {"subject_id_a": r["subject_id_a"], "subject_id_b": r["allegation_id"],
             "from_key": r["subject_id_a"], "to_key": r["allegation_id"]}
            for r in rows
        ]

    raise ValueError(f"Unhandled rule family '{spec.family}' for {rule_id}")  # pragma: no cover


def revert_rejection(case_id: str, rule_id: str, investigator_id: str, reason: str) -> dict:
    """
    Undo every currently-rejected fact rule_id produced for case_id,
    within the same case-scoped population reject_inference used.

    investigator_id and reason are required for the same audit-trail
    reason reject_inference requires them: a revert overrules a prior
    investigator's rejection decision, and this is the only record of
    who did that and why. Both are SET directly on the same node/
    relationship the rejection lived on (reverted_by/revert_reason/
    reverted_at, mirroring rejected_by/rejection_reason/rejected_at),
    since the :Rejection node itself is deleted as part of the revert
    (see the family Cypher below) and can't be the audit record.

    Raises:
        ValueError: case_id/rule_id/investigator_id/reason blank, or
            rule_id unknown.
        InferenceNotFoundError: nothing REJECTED matches — either it was
            never rejected, or it has already been reverted. Mapped to
            404 by the route.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("revert_rejection requires a non-empty case_id")
    if not rule_id or not str(rule_id).strip():
        raise ValueError("revert_rejection requires a non-empty rule_id")
    if not investigator_id or not str(investigator_id).strip():
        raise ValueError("revert_rejection requires a non-empty investigator_id")
    if not reason or not str(reason).strip():
        raise ValueError("revert_rejection requires a non-empty reason")

    case_id = str(case_id).strip()
    rule_id = str(rule_id).strip()
    investigator_id = str(investigator_id).strip()
    reason = str(reason).strip()

    spec = _RULE_SPECS.get(rule_id)
    if spec is None:
        raise ValueError(f"Unknown rule_id '{rule_id}' — cannot revert its rejection")

    reverted_at = datetime.now(timezone.utc).isoformat()

    with get_session() as session:
        scope = _resolve_case_scope(session, case_id)
        instances = _locate_and_revert(
            session, rule_id, spec, case_id, scope, investigator_id, reason, reverted_at,
        )

        if not instances:
            logger.info(
                "revert_rejection: NOT FOUND case_id=%s rule_id=%s — "
                "no rejected fact to revert", case_id, rule_id,
            )
            raise InferenceNotFoundError(
                f"No rejected {spec.relationship_type} fact found for {rule_id} "
                f"on case {case_id} — it may have been reverted already"
            )

        reverted_items = []
        for instance in instances:
            session.run(
                _DELETE_REJECTION,
                relationship_type=spec.relationship_type,
                from_key=instance["from_key"], to_key=instance["to_key"],
                rule_id=rule_id, case_id=case_id,
            )
            reverted_items.append({
                "subject_id_a": instance["subject_id_a"],
                "subject_id_b": instance["subject_id_b"],
            })

    logger.info(
        "revert_rejection: case_id=%s rule_id=%s relationship_type=%s "
        "investigator_id=%s reason=%s count=%d",
        case_id, rule_id, spec.relationship_type, investigator_id, reason, len(reverted_items),
    )

    # Sync the cached rules_fired snapshot (CS-4 + case_ai_summary_store) so
    # the stored JSON reflects this revert's status + reason right away.
    # Best-effort and non-blocking, same as reject_inference above — Neo4j
    # is already the authoritative write.
    update_rules_fired_instance_status(
        case_id=case_id,
        rule_id=rule_id,
        action="revert",
        investigator_id=investigator_id,
        reason=reason,
        timestamp=reverted_at,
        matches=_build_cached_instance_matcher(spec, instances),
    )

    return {
        "result": {
            "reverted": True,
            "case_id": case_id,
            "rule_id": rule_id,
            "relationship_type": spec.relationship_type,
            "investigator_id": investigator_id,
            "reason": reason,
            "status": "active",
            "reverted_count": len(reverted_items),
            "reverted_items": reverted_items,
            "reverted_at": reverted_at,
        },
        "provenance": graph_provenance(
            "reasoning_layer.rejection.revert_rejection",
            ["Neo4j write — rejection cleared"],
        ),
    }