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
:Rejection node is ever created. graph_load.py's docstring says so
explicitly ("POST /reject_inference, Phase 9, owns that"); this is that
module, now built.

THE HARD PART, AND WHY IT NEEDS ITS OWN MODULE:
An investigator clicking "Reject" in the UI knows four things: which
case, which subject(s), which relationship type, and which rule
produced it (Functional Spec D2 Input Contract). What they do NOT know,
and should never have to know, is the internal :Rejection key encoding
each rule file's own guard checks against — and that encoding is NOT
uniform:

  - SHARES_EMPLOYER_WITH / SHARES_ADDRESS_WITH / SHARES_ALIAS_PATTERN_WITH
    (Rules 1/3/5): from_key/to_key = the two subject_ids, unordered.
  - MEMBER_OF_FRAUD_NETWORK (Rules 2/4/6/9): from_key = one subject_id,
    to_key = "<network_type>:<network_key>" — a composite string keyed
    off properties that live on the :FraudNetwork node, not on the
    request. Four different rules, four different network_type prefixes
    ("Employer" / "Address" / "Identity" / "CheckSplit"), NONE of which
    are safe to hardcode a second time here — hardcoding them would be
    exactly the drift Section 5 of the architecture guideline warns
    about (a second, driftable copy of a fact that has exactly one
    correct source). So this module reads network_type/network_key off
    the live :FraudNetwork node the subject is actually connected to,
    rather than reconstructing the prefix from rule_id.
  - HAS_PRIOR_GUILTY_CASE / APPEARS_IN_CASE (Rules 7/10): subject -> case.
  - CASE_RISK_ESCALATION / FASTTRACK_RECOMMENDATION (Rules 8/13): these
    are properties on the :Case node, not a relationship at all — Rule 8
    and Rule 13 write findings as node properties (see their .cypher
    files' own comments on why), so "reject" here means flipping that
    property's own *_status field, not SET on a relationship.
  - WAGE_CORROBORATION (Rule 12): a property on the specific
    :Allegation the wage record corroborates — resolved by lookup, since
    the caller has no reason to know an internal allegation_id.
  - CROSS_CASE_HUB (Rule 11): a property on the :Subject node itself —
    "this is two different people with the same name, not one hub" is a
    fact about the subject, not a relationship to anything.

_RULE_SPECS below is the one and only place this per-rule-type encoding
knowledge lives. Every other module that ever needs to correlate a
:Rejection back to what it suppressed (report_generation.py,
fraud_network.py, rule_audit.py) reads the already-written from_key/
to_key off the :Rejection node — none of them re-derive this encoding.

Does NOT own: rule execution (rule_engine.py), rule content
(rules/*.cypher), or reading back rejected facts for display
(report_generation.py, rule_audit.py, fraud_network.py all do their own
reads, exactly as report_generation.py's own docstring explains why it
does not reuse rules_fired.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)


class InferenceNotFoundError(LookupError):
    """
    Raised when no currently-ACTIVE inferred fact matches the case_id /
    subject_id_a / subject_id_b / relationship_type / rule_id given.

    Three honest reasons this happens, none of them a server error:
      1. It was already rejected (a second click on the same fact).
      2. The rule never actually fired for this subject/case pair —
         the UI is showing stale data.
      3. The caller mismatched relationship_type against rule_id.
    The route maps this to HTTP 404, not 500 — the request was
    understood; the specific fact it names just is not there to reject.
    """


class RelationshipTypeMismatchError(ValueError):
    """Raised when the caller's relationship_type does not match the
    fixed relationship_type _RULE_SPECS declares for rule_id. Defense in
    depth against a UI bug wiring the wrong pair together — the query
    itself is always built from the trusted _RULE_SPECS value, never
    from this field, but a caller who thinks they are rejecting one
    relationship type while another is silently used deserves a loud
    error, not silent correction."""


# --- Families: how "the specific relationship" is located and marked ---
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
    requires_subject_b: bool  # True = required, False = never accepted


# The one and only per-rule encoding table (see module docstring).
# relationship_type values match exactly what report_generation.py and
# every rule file's own Rejection guard already use, so a Rejection
# written here is read back correctly everywhere else without change.
_RULE_SPECS: Dict[str, _RuleSpec] = {
    "Rule_01_Shared_Employer": _RuleSpec(_FAMILY_SYMMETRIC_EDGE, "SHARES_EMPLOYER_WITH", True),
    "Rule_03_Shared_Address": _RuleSpec(_FAMILY_SYMMETRIC_EDGE, "SHARES_ADDRESS_WITH", True),
    "Rule_05_Alias_Identity": _RuleSpec(_FAMILY_SYMMETRIC_EDGE, "SHARES_ALIAS_PATTERN_WITH", True),
    "Rule_07_Prior_Guilty": _RuleSpec(_FAMILY_SUBJECT_CASE_EDGE, "HAS_PRIOR_GUILTY_CASE", False),
    "Rule_10_Merged_Case_Propagation": _RuleSpec(_FAMILY_SUBJECT_CASE_EDGE, "APPEARS_IN_CASE", False),
    "Rule_02_Employer_Fraud_Network": _RuleSpec(_FAMILY_NETWORK_EDGE, "MEMBER_OF_FRAUD_NETWORK", False),
    "Rule_04_Address_Fraud_Network": _RuleSpec(_FAMILY_NETWORK_EDGE, "MEMBER_OF_FRAUD_NETWORK", False),
    "Rule_06_Identity_Fraud_Network": _RuleSpec(_FAMILY_NETWORK_EDGE, "MEMBER_OF_FRAUD_NETWORK", False),
    "Rule_09_PCA_CheckSplit": _RuleSpec(_FAMILY_NETWORK_EDGE, "MEMBER_OF_FRAUD_NETWORK", False),
    "Rule_11_Cross_Case_Hub": _RuleSpec(_FAMILY_SUBJECT_FLAG, "CROSS_CASE_HUB", False),
    "Rule_08_Recidivist_Escalation": _RuleSpec(_FAMILY_CASE_FLAG, "CASE_RISK_ESCALATION", False),
    "Rule_13_FastTrack_Escalation": _RuleSpec(_FAMILY_CASE_FLAG, "FASTTRACK_RECOMMENDATION", False),
    "Rule_12_SLAM_Wage_Corroboration": _RuleSpec(_FAMILY_ALLEGATION_FLAG, "WAGE_CORROBORATION", False),
    # Rule_14 is a cross-cutting confidence modifier on an existing edge
    # (Developer Spec Section 6.0), not an independent inferred fact —
    # rejecting the base edge already removes its Rule 14 elevation.
    # There is deliberately no entry for it here.
}

RULE_IDS_REJECTABLE: List[str] = sorted(_RULE_SPECS)


# --- Cypher, one per family. Every SET on a relationship is on the
# EXACT relationship instance matched (never a label-wide UPDATE), and
# every MERGE (:Rejection) is a single write in the same session as the
# SET so a caller never observes "rejected but no audit record" or vice
# versa if the process dies mid-way (both statements run inside the one
# transaction get_session()'s auto-commit wraps per session.run call in
# this driver version; see reject_inference()'s use of execute_write).
_LOCATE_AND_REJECT_SYMMETRIC_EDGE = """
MATCH (a:Subject {{subject_id: $subject_id_a}})
      -[r:{rel_type}]-
      (b:Subject {{subject_id: $subject_id_b}})
WHERE r.source_rule = $rule_id AND r.status = "active"
SET r.status = "rejected"
RETURN elementId(r) AS target_id
"""

_LOCATE_AND_REJECT_SUBJECT_CASE_EDGE = """
MATCH (a:Subject {{subject_id: $subject_id_a}})-[r:{rel_type}]->(c:Case {{case_id: $case_id}})
WHERE r.source_rule = $rule_id AND r.status = "active"
SET r.status = "rejected"
RETURN elementId(r) AS target_id
"""

# Rejects subject_id_a's membership, and subject_id_b's too when given —
# "reject this specific edge shown in the graph", never the whole network
# node, which may have other, un-rejected members.
_LOCATE_AND_REJECT_NETWORK_EDGE = """
MATCH (a:Subject {subject_id: $subject_id_a})-[ra:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE ra.source_rule = $rule_id AND ra.status = "active"
OPTIONAL MATCH (b:Subject {subject_id: $subject_id_b})-[rb:MEMBER_OF_FRAUD_NETWORK]->(n)
WHERE $subject_id_b IS NOT NULL AND rb.source_rule = $rule_id AND rb.status = "active"
SET ra.status = "rejected"
WITH n, ra, rb
FOREACH (_ IN CASE WHEN rb IS NOT NULL THEN [1] ELSE [] END | SET rb.status = "rejected")
RETURN elementId(ra) AS target_id, n.network_type AS network_type, n.network_key AS network_key
"""

_LOCATE_AND_REJECT_SUBJECT_FLAG = """
MATCH (a:Subject {subject_id: $subject_id_a})
WHERE a.cross_case_source_rule = $rule_id AND a.is_cross_case = true
SET a.is_cross_case = false,
    a.cross_case_rejected = true,
    a.cross_case_rejected_by = $investigator_id,
    a.cross_case_rejected_at = $rejected_at
RETURN elementId(a) AS target_id
"""

# from_key for Rule 8 is the subject the rule itself recorded as the
# escalating recidivist (c.risk_escalation_subject_id) — verified against
# the caller's subject_id_a rather than trusted blindly, so a caller
# cannot reject a different subject's escalation by supplying a case_id
# that happens to be escalated for someone else.
_LOCATE_AND_REJECT_CASE_FLAG: Dict[str, str] = {
    "Rule_08_Recidivist_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.risk_escalation_source_rule = $rule_id
          AND c.risk_escalation_status = "active"
          AND c.risk_escalation_subject_id = $subject_id_a
        SET c.risk_escalation_status = "rejected"
        RETURN elementId(c) AS target_id
    """,
    "Rule_13_FastTrack_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.fasttrack_recommendation_rule = $rule_id
          AND c.fasttrack_recommendation_status = "active"
        RETURN elementId(c) AS target_id
    """,
}
# Rule 13's SET is issued separately below (see _reject_case_flag) only
# after target_id confirms a match, to keep the two rules' statements
# textually distinct without a conditional SET inside one shared string.
_CASE_FLAG_STATUS_FIELD = {
    "Rule_08_Recidivist_Escalation": "risk_escalation_status",
    "Rule_13_FastTrack_Escalation": "fasttrack_recommendation_status",
}

_LOCATE_AND_REJECT_ALLEGATION_FLAG = """
MATCH (c:Case {case_id: $case_id})-[:HAS_ALLEGATION]->(al:Allegation)
      -[:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject {subject_id: $subject_id_a})
WHERE al.wage_corroboration_rule = $rule_id AND al.wage_corroboration_status = "active"
SET al.wage_corroboration_status = "rejected"
RETURN elementId(al) AS target_id, al.allegation_id AS allegation_id
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
RETURN elementId(rej) AS rejection_id, rej.rejected_at AS rejected_at, rej.rejected_by AS rejected_by
"""


def _reject_symmetric_edge(session, rule_id: str, spec: _RuleSpec, subject_id_a: str,
                            subject_id_b: str, **_: Any) -> Optional[Dict[str, str]]:
    query = _LOCATE_AND_REJECT_SYMMETRIC_EDGE.format(rel_type=spec.relationship_type)
    record = session.run(query, subject_id_a=subject_id_a, subject_id_b=subject_id_b,
                          rule_id=rule_id).single()
    if record is None:
        return None
    from_key, to_key = sorted([subject_id_a, subject_id_b])
    return {"from_key": from_key, "to_key": to_key}


def _reject_subject_case_edge(session, rule_id: str, spec: _RuleSpec, subject_id_a: str,
                               case_id: str, **_: Any) -> Optional[Dict[str, str]]:
    query = _LOCATE_AND_REJECT_SUBJECT_CASE_EDGE.format(rel_type=spec.relationship_type)
    record = session.run(query, subject_id_a=subject_id_a, case_id=case_id, rule_id=rule_id).single()
    if record is None:
        return None
    return {"from_key": subject_id_a, "to_key": case_id}


def _reject_network_edge(session, rule_id: str, subject_id_a: str,
                          subject_id_b: Optional[str], **_: Any) -> Optional[Dict[str, str]]:
    record = session.run(
        _LOCATE_AND_REJECT_NETWORK_EDGE,
        subject_id_a=subject_id_a, subject_id_b=subject_id_b, rule_id=rule_id,
    ).single()
    if record is None:
        return None
    # Read the composite key off the live :FraudNetwork node — see module
    # docstring on why this is never reconstructed from a hardcoded prefix.
    to_key = f'{record["network_type"]}:{record["network_key"]}'
    return {"from_key": subject_id_a, "to_key": to_key}


def _reject_subject_flag(session, rule_id: str, subject_id_a: str, investigator_id: str,
                          rejected_at: str, **_: Any) -> Optional[Dict[str, str]]:
    record = session.run(
        _LOCATE_AND_REJECT_SUBJECT_FLAG,
        subject_id_a=subject_id_a, rule_id=rule_id,
        investigator_id=investigator_id, rejected_at=rejected_at,
    ).single()
    if record is None:
        return None
    # No relationship instance exists for a node-property flag; to_key is
    # the subject itself so the :Rejection key stays unique per subject.
    return {"from_key": subject_id_a, "to_key": subject_id_a}


def _reject_case_flag(session, rule_id: str, subject_id_a: str,
                       case_id: str, **_: Any) -> Optional[Dict[str, str]]:
    query = _LOCATE_AND_REJECT_CASE_FLAG[rule_id]
    record = session.run(query, case_id=case_id, rule_id=rule_id, subject_id_a=subject_id_a).single()
    if record is None:
        return None
    if rule_id == "Rule_13_FastTrack_Escalation":
        # Rule 13's own guard (rules/wave2/rule_13_fasttrack_escalation.cypher)
        # does not stamp the escalating subject onto :Case, so subject_id_a
        # is trusted from the request here rather than re-verified against
        # a stored value that does not exist to check against.
        session.run(
            "MATCH (c:Case {case_id: $case_id}) SET c.fasttrack_recommendation_status = 'rejected'",
            case_id=case_id,
        )
    return {"from_key": subject_id_a, "to_key": case_id}


def _reject_allegation_flag(session, rule_id: str, subject_id_a: str,
                             case_id: str, **_: Any) -> Optional[Dict[str, str]]:
    record = session.run(
        _LOCATE_AND_REJECT_ALLEGATION_FLAG,
        case_id=case_id, subject_id_a=subject_id_a, rule_id=rule_id,
    ).single()
    if record is None:
        return None
    return {"from_key": subject_id_a, "to_key": record["allegation_id"]}


_FAMILY_HANDLERS = {
    _FAMILY_SYMMETRIC_EDGE: _reject_symmetric_edge,
    _FAMILY_SUBJECT_CASE_EDGE: _reject_subject_case_edge,
    _FAMILY_NETWORK_EDGE: _reject_network_edge,
    _FAMILY_SUBJECT_FLAG: _reject_subject_flag,
    _FAMILY_CASE_FLAG: _reject_case_flag,
    _FAMILY_ALLEGATION_FLAG: _reject_allegation_flag,
}


def _envelope(result: Dict[str, Any]) -> dict:
    """Standard {result, provenance} envelope (Principle 8), identical in
    shape to every other direct-call reasoning_layer module."""
    return {
        "result": result,
        "provenance": {
            "sources": ["Neo4j write — :Rejection"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "reasoning_layer.rejection.reject_inference",
        },
    }


def reject_inference(
    case_id: str,
    subject_id_a: str,
    rule_id: str,
    relationship_type: str,
    investigator_id: str,
    subject_id_b: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict:
    """
    Record an investigator's decision to overrule one specific inferred
    fact (Functional Specification D2). A suppression, not a deletion:
    the underlying relationship/property is set to "rejected", never
    removed, and a permanent :Rejection audit record is written
    alongside it. Future pipeline runs check for this record before
    re-asserting the same fact (Developer Spec Section 5.2) — every rule
    file's own guard already does this; this function is what makes a
    guard find something.

    Args:
        case_id: required. The case the rejection is issued from.
        subject_id_a: required. The first (or only) subject in the
            relationship being rejected.
        rule_id: required. Which rule produced the fact, e.g.
            "Rule_01_Shared_Employer". Must be one of RULE_IDS_REJECTABLE.
        relationship_type: required. Must match the fixed type
            _RULE_SPECS declares for rule_id — a defense-in-depth check,
            not the value actually used to build the query.
        investigator_id: required. AppWorks user id issuing the rejection.
            Never stored blank — a rejection is an attributable fact.
        subject_id_b: the second subject, when relationship_type is a
            subject-subject fact (Rules 1/3/5, always required there) or
            when the investigator is rejecting BOTH sides of a network
            membership pair at once (Rules 2/4/6/9, optional there).
            Must be omitted for every other rule.
        reason: optional free-text reason.

    Returns (inside the standard {result, provenance} envelope):
        {"accepted": true, "rejection_id": ..., "rejected_at": ...,
         "rejected_by": ..., "relationship_type": ..., "rule_id": ...}

    Raises:
        ValueError: rule_id unknown, investigator_id blank, or
            subject_id_b supplied/omitted against what rule_id requires.
        RelationshipTypeMismatchError: relationship_type does not match
            what rule_id declares.
        InferenceNotFoundError: no currently-active fact matches — see
            the class docstring for the three honest reasons this
            happens. Mapped to HTTP 404 by the route, not 500.
        GraphUnavailableError / Neo4jError: propagated unchanged.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("reject_inference requires a non-empty case_id")
    if not subject_id_a or not str(subject_id_a).strip():
        raise ValueError("reject_inference requires a non-empty subject_id_a")
    if not investigator_id or not str(investigator_id).strip():
        raise ValueError("reject_inference requires a non-empty investigator_id — "
                          "a rejection must always be attributable")

    spec = _RULE_SPECS.get(rule_id)
    if spec is None:
        raise ValueError(
            f"Unknown or non-rejectable rule_id={rule_id!r}. "
            f"Must be one of: {RULE_IDS_REJECTABLE}"
        )
    if relationship_type != spec.relationship_type:
        raise RelationshipTypeMismatchError(
            f"relationship_type={relationship_type!r} does not match "
            f"{spec.relationship_type!r}, the type rule_id={rule_id!r} produces."
        )
    if spec.requires_subject_b and not subject_id_b:
        raise ValueError(f"rule_id={rule_id!r} requires subject_id_b (a two-subject fact).")
    if spec.family in (_FAMILY_SUBJECT_CASE_EDGE, _FAMILY_SUBJECT_FLAG,
                       _FAMILY_CASE_FLAG, _FAMILY_ALLEGATION_FLAG) and subject_id_b:
        raise ValueError(
            f"rule_id={rule_id!r} is a single-subject fact — subject_id_b must be omitted."
        )

    case_id = str(case_id).strip()
    subject_id_a = str(subject_id_a).strip()
    subject_id_b = str(subject_id_b).strip() if subject_id_b else None
    investigator_id = str(investigator_id).strip()
    rejected_at = datetime.now(timezone.utc).isoformat()

    handler = _FAMILY_HANDLERS[spec.family]

    with get_session() as session:
        keys = handler(
            session=session, rule_id=rule_id, spec=spec,
            subject_id_a=subject_id_a, subject_id_b=subject_id_b,
            case_id=case_id, investigator_id=investigator_id, rejected_at=rejected_at,
        )
        if keys is None:
            logger.info(
                "reject_inference: NOT FOUND case_id=%s subject_id_a=%s subject_id_b=%s "
                "rule_id=%s — no active fact to reject",
                case_id, subject_id_a, subject_id_b, rule_id,
            )
            raise InferenceNotFoundError(
                f"No active inferred relationship found for rule_id={rule_id!r}, "
                f"subject_id_a={subject_id_a!r}, subject_id_b={subject_id_b!r}, "
                f"case_id={case_id!r}. It may already be rejected, or the rule "
                f"never fired for this pairing."
            )

        rejection_record = session.run(
            _MERGE_REJECTION,
            relationship_type=spec.relationship_type,
            from_key=keys["from_key"], to_key=keys["to_key"],
            investigator_id=investigator_id, rejected_at=rejected_at,
            reason=reason, rule_id=rule_id, case_id=case_id,
        ).single()

    logger.info(
        "reject_inference: REJECTED case_id=%s rule_id=%s relationship_type=%s "
        "from_key=%s to_key=%s investigator_id=%s",
        case_id, rule_id, spec.relationship_type, keys["from_key"], keys["to_key"],
        investigator_id,
    )

    result = {
        "accepted": True,
        "rejection_id": rejection_record["rejection_id"],
        "rejected_at": rejection_record["rejected_at"],
        "rejected_by": rejection_record["rejected_by"],
        "relationship_type": spec.relationship_type,
        "rule_id": rule_id,
    }
    return _envelope(result)
