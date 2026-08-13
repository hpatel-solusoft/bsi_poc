"""
Owns: AI-30 — walking reasoning_layer.rule_registry.DOWNSTREAM_DEPENDENTS
after a reject_inference/revert_rejection call to auto-invalidate (or
re-validate) whatever downstream rule fact rested on the upstream fact
that just changed. Walks multiple hops where the map has them
(Rule 1 -> Rule 2 -> Rule 8), per the cascade design's Flow 1/Flow 2.

Does NOT own: the upstream write itself (reasoning_layer/rejection.py
owns that, and calls cascade_reject/cascade_revert immediately
afterward, once per subject the rejected/reverted instance itself
touches); which relationship type a downstream rule reads from which
upstream rule (reasoning_layer/rule_registry.py's DOWNSTREAM_DEPENDENTS
table owns that, and is checked against the real .cypher files by
tests/verify.py so it cannot silently go stale); or a downstream rule's
real WRITE query (reasoning_layer/rules/*.cypher — re-invoked here via
reasoning_layer.rule_engine on revert, never duplicated as a second
copy of the same Cypher).

CONDITION RE-CHECK IS GENERIC ACROSS EVERY DOWNSTREAM RULE, AND
DELIBERATELY NOT SCOPED TO WHICH UPSTREAM RULE PRODUCED THE FACT: "does
this subject still have at least one ACTIVE relationship of the type
the downstream rule reads." This matters specifically for
MEMBER_OF_FRAUD_NETWORK, written by four different rules (02/04/06/09)
— Rule 8 reads "is this subject a member of ANY active fraud network",
not "is this subject still a member of the SPECIFIC network Rule 2
built". Rejecting Rule 2's contribution must not auto-retract Rule 8 if
Rule 9 independently still has the subject in a network. Matches
DOWNSTREAM_DEPENDENTS' own docstring, and Flow 1 of the cascade design:
"Rule 2 / 4 / 6 / 9 (MEMBER_OF_FRAUD_NETWORK) --> check Rule 8:
condition: does subject still have ANY active MEMBER_OF_FRAUD_NETWORK
edge (any network type)?".

PARTIAL vs FULL INSTANCE REJECTION NEEDS NO SPECIAL-CASE CODE (Flow 2
of the cascade design): the condition re-check above is a pure EXISTS
query across every active edge of that type for the subject, not a
count tied to the one instance that was just rejected — so "2 of 3
network members still active" and "the last active member was just
rejected" both fall out of the exact same query correctly, with
nothing extra to write.

AUTO-INVALIDATION IS ALWAYS DISTINGUISHABLE FROM A MANUAL REJECTION:
the reject direction SETs status="rejected" plus THREE audit fields —
<field>_auto_invalidated=true, <field>_invalidated_by_rule_id=
<upstream rule_id>, and <field>_invalidated_at=<timestamp> — on the
downstream fact, instead of the investigator-attributed rejected_by/
rejected_at/rejection_reason reasoning_layer/rejection.py stamps on a
manual rejection. The case-level field names (risk_escalation_*,
fasttrack_recommendation_*) match the ones already named in the BSI
Phase 2 task list (AI-31); the equivalent pair on a
MEMBER_OF_FRAUD_NETWORK relationship (auto_invalidated/
invalidated_by_rule_id/invalidated_by_investigator_id/rejected_at — no
field-name prefix needed, since it lives on one relationship rather
than two case-level properties) is this module's own extension of the
same idea, for the same auditability, not literally named in the
original cascade design doc but consistent with its intent.

REVERT NEVER JUST FLIPS A STATUS BACK: it re-invokes the downstream
rule's OWN write query via reasoning_layer.rule_engine.execute_rules,
exactly as a normal pipeline run would, so every derived property
(confidence, allegation_type, member list, etc.) gets recomputed fresh
— never a stale auto-invalidated snapshot silently un-rejected. Only
actually reinstated when the fact currently carries THIS module's own
auto_invalidated flag: a condition coming back true for a fact that was
never auto-invalidated in the first place (it was always active, or an
investigator manually rejected it — a DIFFERENT decision this module
must never override) has nothing to reinstate, and is left alone.

WALK DEPTH: capped at 2 (Rule 1 -> Rule 2 -> Rule 8 is the deepest
chain the current ontology has, per rule_registry.DOWNSTREAM_DEPENDENTS);
the cap is defensive headroom for a future rule extending the chain,
not a case that exists today.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from reasoning_layer import rule_engine, rule_registry
from reasoning_layer import scope as scope_resolver

logger = logging.getLogger(__name__)

_MAX_WALK_DEPTH = 2

# Rule_09_PCA_CheckSplit is deliberately absent from this map — see
# rule_registry.DOWNSTREAM_DEPENDENTS' own docstring: it never appears
# as a downstream VALUE (only ever as an upstream key, for Rule_08), so
# it can never be an auto-invalidation TARGET and has nothing to look
# up here.
_NETWORK_TYPE_BY_RULE_ID: Dict[str, str] = {
    "Rule_02_Employer_Fraud_Network": "Employer",
    "Rule_04_Address_Fraud_Network": "Address",
    "Rule_06_Identity_Fraud_Network": "Identity",
}

_CASE_FLAG_FIELDS: Dict[str, Dict[str, Optional[str]]] = {
    "Rule_08_Recidivist_Escalation": {
        "status": "risk_escalation_status",
        "auto_invalidated": "risk_escalation_auto_invalidated",
        "invalidated_by": "risk_escalation_invalidated_by_rule_id",
        "invalidated_reason": "risk_escalation_invalidated_reason",
        "invalidated_by_investigator": "risk_escalation_invalidated_by_investigator_id",
        "invalidated_at": "risk_escalation_invalidated_at",
        "reinstated_by": "risk_escalation_reinstated_by_rule_id",
        "reinstated_reason": "risk_escalation_reinstated_reason",
        "reinstated_at": "risk_escalation_reinstated_at",
        "reinstated_by_investigator": "risk_escalation_reinstated_by_investigator_id",
        "subject_field": "risk_escalation_subject_id",
    },
    "Rule_13_FastTrack_Escalation": {
        "status": "fasttrack_recommendation_status",
        "auto_invalidated": "fasttrack_recommendation_auto_invalidated",
        "invalidated_by": "fasttrack_recommendation_invalidated_by_rule_id",
        "invalidated_reason": "fasttrack_recommendation_invalidated_reason",
        "invalidated_by_investigator": "fasttrack_recommendation_invalidated_by_investigator_id",
        "invalidated_at": "fasttrack_recommendation_invalidated_at",
        "reinstated_by": "fasttrack_recommendation_reinstated_by_rule_id",
        "reinstated_reason": "fasttrack_recommendation_reinstated_reason",
        "reinstated_at": "fasttrack_recommendation_reinstated_at",
        "reinstated_by_investigator": "fasttrack_recommendation_reinstated_by_investigator_id",
        # Rule 13 stamps no escalating-subject id onto :Case — it is
        # scoped to the case's PRIMARY subject only, not a subject the
        # graph records anywhere on the fact itself (see
        # reasoning_layer/rejection.py's _BULK_REJECT_CASE_FLAG comment
        # for the identical point) — so there is no per-subject field
        # to match against here; the case_id alone already disambiguates.
        "subject_field": None,
    },
}

# The relationship_type each downstream rule's :Rejection audit nodes
# use — must match reasoning_layer.rejection._RULE_SPECS exactly (that
# module owns the authoritative mapping; this is a read-only mirror of
# the subset _has_manual_rejection needs). Used ONLY to tell an
# independent MANUAL rejection of this fact (reject_inference — a real
# :Rejection node) apart from this module's own auto-invalidation
# (never writes one) — see _has_manual_rejection's docstring.
_REJECTION_RELATIONSHIP_TYPE_BY_RULE_ID: Dict[str, str] = {
    "Rule_02_Employer_Fraud_Network": "MEMBER_OF_FRAUD_NETWORK",
    "Rule_04_Address_Fraud_Network": "MEMBER_OF_FRAUD_NETWORK",
    "Rule_06_Identity_Fraud_Network": "MEMBER_OF_FRAUD_NETWORK",
    "Rule_08_Recidivist_Escalation": "CASE_RISK_ESCALATION",
    "Rule_13_FastTrack_Escalation": "FASTTRACK_RECOMMENDATION",
}

_STILL_ACTIVE_QUERY = """
MATCH (a:Subject {subject_id: $subject_id})-[r]-()
WHERE type(r) = $relationship_type AND coalesce(r.status, "active") = "active"
RETURN count(r) > 0 AS still_active
"""


def _condition_still_holds(session, subject_id: str, relationship_type: str) -> bool:
    record = session.run(
        _STILL_ACTIVE_QUERY, subject_id=subject_id, relationship_type=relationship_type
    ).single()
    return bool(record and record["still_active"])


# downstream_rule_id -> every DISTINCT relationship_type it depends on,
# derived from rule_registry.DOWNSTREAM_DEPENDENTS itself (never
# hand-duplicated) so it can never drift from the table it's built from.
# A rule fed by more than one upstream relationship type — today only
# Rule_08 (HAS_PRIOR_GUILTY_CASE from Rule_07, MEMBER_OF_FRAUD_NETWORK
# from Rules 2/4/6/9) — gets every distinct type it reads listed here.
# A rule fed by only one relationship_type (Rule_02/04/06, Rule_13)
# gets a single-element list, which collapses the ANY-check below back
# to the plain single-condition check for it.
_REQUIRED_RELATIONSHIP_TYPES: Dict[str, List[str]] = {}
for _upstream_rule_id, _dependents in rule_registry.DOWNSTREAM_DEPENDENTS.items():
    for _dependent in _dependents:
        _types_for_rule = _REQUIRED_RELATIONSHIP_TYPES.setdefault(_dependent["rule_id"], [])
        if _dependent["relationship_type"] not in _types_for_rule:
            _types_for_rule.append(_dependent["relationship_type"])
del _upstream_rule_id, _dependents, _dependent, _types_for_rule


def _any_upstream_condition_holds(session, subject_id: str, downstream_rule_id: str) -> bool:
    """
    Whether AT LEAST ONE of the distinct relationship types
    downstream_rule_id depends on (per _REQUIRED_RELATIONSHIP_TYPES
    above) is currently active for subject_id.

    REJECT-DIRECTION CHECK ONLY — deliberately looser than the
    downstream rule's own Cypher (which requires ALL of them to MATCH a
    fresh row): a downstream fact resting on more than one upstream
    contributor — today only Rule_08, resting on Rule_07's prior-guilty
    finding AND on a fraud-network membership from Rules 2/4/6/9 — is
    only auto-REJECTED once EVERY contributor has been rejected.
    Concretely: rejecting Rule_01 alone (breaking only the network leg)
    must NOT auto-reject Rule_08 while Rule_07's prior-guilty finding is
    still active; only rejecting BOTH legs does. A rule resting on a
    single relationship_type (Rule_02/04/06, Rule_13) has exactly one
    entry in its required list, so this collapses back to that one
    condition either way — unaffected.

    Deliberately NOT reused for the revert direction — see
    _all_upstream_conditions_hold below, which REVERT uses instead. The
    two directions are asymmetric on purpose: it takes rejecting every
    contributor to auto-reject a multi-contributor fact, but it takes
    EVERY contributor being active again to auto-reinstate one — a
    single contributor coming back (e.g. reverting Rule_07 alone) must
    NOT resurrect Rule_08 while Rule_01/02's network leg is still
    rejected. Restoring a serious escalation is held to the stricter
    standard on purpose, even though breaking it is held to the looser
    one.
    """
    required_types = _REQUIRED_RELATIONSHIP_TYPES.get(downstream_rule_id, [])
    if not required_types:
        return True
    return any(_condition_still_holds(session, subject_id, rtype) for rtype in required_types)


def _all_upstream_conditions_hold(session, subject_id: str, downstream_rule_id: str) -> bool:
    """
    Whether EVERY distinct relationship type downstream_rule_id depends
    on (per _REQUIRED_RELATIONSHIP_TYPES above) is currently active for
    subject_id — AND across types.

    REVERT-DIRECTION CHECK ONLY (mirror of _any_upstream_condition_holds
    above, used by REJECT). A downstream fact resting on more than one
    upstream contributor must have ALL of them active again before it is
    auto-reinstated: reverting Rule_07 alone must NOT bring Rule_08 back
    while Rule_01/02's fraud-network leg is still rejected — only once
    BOTH legs are active again does Rule_08 return. A rule resting on a
    single relationship_type (Rule_02/04/06, Rule_13) has exactly one
    entry in its required list, so this collapses back to that one
    condition either way — unaffected.
    """
    required_types = _REQUIRED_RELATIONSHIP_TYPES.get(downstream_rule_id, [])
    if not required_types:
        return True
    return all(_condition_still_holds(session, subject_id, rtype) for rtype in required_types)


_AUTO_INVALIDATE_MEMBERSHIP = """
MATCH (a:Subject {subject_id: $subject_id})-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork {network_type: $network_type})
WHERE coalesce(r.status, "active") = "active"
SET r.status = "rejected",
    r.auto_invalidated = true,
    r.invalidated_by_rule_id = $upstream_rule_id,
    r.invalidated_reason = $reason,
    r.invalidated_by_investigator_id = $investigator_id,
    r.rejected_at = $timestamp
RETURN count(r) AS updated
"""

_WAS_AUTO_INVALIDATED_MEMBERSHIP = """
MATCH (a:Subject {subject_id: $subject_id})-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork {network_type: $network_type})
WHERE r.auto_invalidated = true
RETURN count(r) > 0 AS was_auto_invalidated
"""

# Run after the downstream rule's OWN write query has already restored
# status="active" for real — only clears/records this module's
# bookkeeping, since the rule's write already handled status itself.
_MARK_REINSTATED_MEMBERSHIP = """
MATCH (a:Subject {subject_id: $subject_id})-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork {network_type: $network_type})
WHERE coalesce(r.status, "active") = "active"
SET r.auto_invalidated = false,
    r.reinstated_by_rule_id = $upstream_rule_id,
    r.reinstated_reason = $reason,
    r.reinstated_by_investigator_id = $investigator_id,
    r.reinstated_at = $timestamp
REMOVE r.invalidated_by_rule_id, r.invalidated_reason, r.invalidated_by_investigator_id
"""

_MEMBERSHIP_IS_ACTIVE_QUERY = """
MATCH (a:Subject {subject_id: $subject_id})-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork {network_type: $network_type})
RETURN coalesce(r.status, "active") = "active" AS is_active
"""

# Fallback SET — see _reinstate's "COALESCE TRAP". Sets status="active"
# directly AND the same reinstated bookkeeping _MARK_REINSTATED_MEMBERSHIP
# sets, in one write, since the rule's own write couldn't be relied on
# to have set status here.
_DIRECT_REINSTATE_MEMBERSHIP = """
MATCH (a:Subject {subject_id: $subject_id})-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork {network_type: $network_type})
WHERE r.auto_invalidated = true
SET r.status = "active",
    r.auto_invalidated = false,
    r.reinstated_by_rule_id = $upstream_rule_id,
    r.reinstated_reason = $reason,
    r.reinstated_by_investigator_id = $investigator_id,
    r.reinstated_at = $timestamp
REMOVE r.invalidated_by_rule_id, r.invalidated_reason, r.invalidated_by_investigator_id
RETURN count(r) AS updated
"""


def _auto_invalidate(
    session,
    case_id: str,
    downstream_rule_id: str,
    subject_id: str,
    upstream_rule_id: str,
    reason: str,
    timestamp: str,
    investigator_id: Optional[str],
) -> bool:
    """
    Returns True if something was actually changed (the fact was active
    and is now auto-invalidated) — False if there was nothing to
    invalidate, e.g. it was already rejected (manually or by an earlier
    hop of this same walk).

    `investigator_id` is the investigator who triggered the UPSTREAM
    reject that caused this hop — stamped alongside invalidated_by_
    rule_id/invalidated_reason so a downstream fact's own auto-
    invalidation audit trail names who is ultimately responsible, not
    just which rule. It is never a second, independent investigator
    decision on the downstream fact itself (that is a manual /reject_
    inference call, tracked separately via a real :Rejection node).
    """
    if downstream_rule_id in _NETWORK_TYPE_BY_RULE_ID:
        record = session.run(
            _AUTO_INVALIDATE_MEMBERSHIP,
            subject_id=subject_id,
            network_type=_NETWORK_TYPE_BY_RULE_ID[downstream_rule_id],
            upstream_rule_id=upstream_rule_id,
            reason=reason,
            timestamp=timestamp,
            investigator_id=investigator_id,
        ).single()
        return bool(record and record["updated"])

    if downstream_rule_id in _CASE_FLAG_FIELDS:
        fields = _CASE_FLAG_FIELDS[downstream_rule_id]
        subject_clause = f"AND c.{fields['subject_field']} = $subject_id" if fields["subject_field"] else ""
        query = f"""
        MATCH (c:Case {{case_id: $case_id}})
        WHERE coalesce(c.{fields['status']}, "active") = "active"
          {subject_clause}
        SET c.{fields['status']} = "rejected",
            c.{fields['auto_invalidated']} = true,
            c.{fields['invalidated_by']} = $upstream_rule_id,
            c.{fields['invalidated_reason']} = $reason,
            c.{fields['invalidated_by_investigator']} = $investigator_id,
            c.{fields['invalidated_at']} = $timestamp
        RETURN count(c) AS updated
        """
        record = session.run(
            query,
            case_id=case_id,
            subject_id=subject_id,
            upstream_rule_id=upstream_rule_id,
            reason=reason,
            timestamp=timestamp,
            investigator_id=investigator_id,
        ).single()
        return bool(record and record["updated"])

    logger.warning(
        "cascade: %s is not a recognized auto-invalidation target (absent from "
        "both _NETWORK_TYPE_BY_RULE_ID and _CASE_FLAG_FIELDS) — "
        "DOWNSTREAM_DEPENDENTS references a rule this module doesn't know how "
        "to auto-invalidate yet; add it to one of those two maps",
        downstream_rule_id,
    )
    return False


def _was_auto_invalidated(session, case_id: str, downstream_rule_id: str, subject_id: str) -> bool:
    if downstream_rule_id in _NETWORK_TYPE_BY_RULE_ID:
        record = session.run(
            _WAS_AUTO_INVALIDATED_MEMBERSHIP,
            subject_id=subject_id,
            network_type=_NETWORK_TYPE_BY_RULE_ID[downstream_rule_id],
        ).single()
        return bool(record and record["was_auto_invalidated"])

    if downstream_rule_id in _CASE_FLAG_FIELDS:
        fields = _CASE_FLAG_FIELDS[downstream_rule_id]
        query = f"""
        MATCH (c:Case {{case_id: $case_id}})
        WHERE c.{fields['auto_invalidated']} = true
        RETURN count(c) > 0 AS was_auto_invalidated
        """
        record = session.run(query, case_id=case_id).single()
        return bool(record and record["was_auto_invalidated"])

    return False


def _is_downstream_fact_active(session, case_id: str, downstream_rule_id: str, subject_id: str) -> bool:
    """
    The authoritative outcome check _reinstate uses after re-running
    downstream_rule_id's own write query: is the SPECIFIC fact this
    module is trying to reinstate actually "active" right now? Checking
    this directly — rather than trusting execute_rules' `writes` count
    as a proxy — is what catches the coalesce-guard trap documented on
    _reinstate itself: the rule's write query can report writes > 0
    (other properties genuinely changed) while the one field that
    matters, status, never moved.
    """
    if downstream_rule_id in _NETWORK_TYPE_BY_RULE_ID:
        record = session.run(
            _MEMBERSHIP_IS_ACTIVE_QUERY,
            subject_id=subject_id,
            network_type=_NETWORK_TYPE_BY_RULE_ID[downstream_rule_id],
        ).single()
        return bool(record and record["is_active"])

    if downstream_rule_id in _CASE_FLAG_FIELDS:
        fields = _CASE_FLAG_FIELDS[downstream_rule_id]
        query = f"""
        MATCH (c:Case {{case_id: $case_id}})
        RETURN coalesce(c.{fields['status']}, "active") = "active" AS is_active
        """
        record = session.run(query, case_id=case_id).single()
        return bool(record and record["is_active"])

    return False


def _direct_reinstate(
    session,
    case_id: str,
    downstream_rule_id: str,
    subject_id: str,
    upstream_rule_id: str,
    reason: str,
    timestamp: str,
    investigator_id: Optional[str],
) -> bool:
    """
    Fallback used only when re-running downstream_rule_id's own write
    query did NOT bring the fact back to active — see _reinstate's
    "COALESCE TRAP" section. SETs status="active" directly, the same
    directness _auto_invalidate already uses in the other direction, and
    is exactly as safe here: this module's own auto-invalidation never
    went through the rule's write path or a :Rejection node in the
    first place (see _auto_invalidate), so nothing about a fact this
    module itself put into "rejected" requires going through the rule
    to come back out of it.
    """
    if downstream_rule_id in _NETWORK_TYPE_BY_RULE_ID:
        record = session.run(
            _DIRECT_REINSTATE_MEMBERSHIP,
            subject_id=subject_id,
            network_type=_NETWORK_TYPE_BY_RULE_ID[downstream_rule_id],
            upstream_rule_id=upstream_rule_id,
            reason=reason,
            timestamp=timestamp,
            investigator_id=investigator_id,
        ).single()
        return bool(record and record["updated"])

    if downstream_rule_id in _CASE_FLAG_FIELDS:
        fields = _CASE_FLAG_FIELDS[downstream_rule_id]
        query = f"""
        MATCH (c:Case {{case_id: $case_id}})
        WHERE c.{fields['auto_invalidated']} = true
        SET c.{fields['status']} = "active",
            c.{fields['auto_invalidated']} = false,
            c.{fields['reinstated_by']} = $upstream_rule_id,
            c.{fields['reinstated_reason']} = $reason,
            c.{fields['reinstated_by_investigator']} = $investigator_id,
            c.{fields['reinstated_at']} = $timestamp
        REMOVE c.{fields['invalidated_by']}, c.{fields['invalidated_reason']},
               c.{fields['invalidated_by_investigator']}, c.{fields['invalidated_at']}
        RETURN count(c) AS updated
        """
        record = session.run(
            query,
            case_id=case_id,
            upstream_rule_id=upstream_rule_id,
            reason=reason,
            timestamp=timestamp,
            investigator_id=investigator_id,
        ).single()
        return bool(record and record["updated"])

    return False


def _mark_reinstated(
    session,
    case_id: str,
    downstream_rule_id: str,
    subject_id: str,
    upstream_rule_id: str,
    reason: str,
    timestamp: str,
    investigator_id: Optional[str],
) -> None:
    """
    Runs immediately after rule_engine.execute_rules has re-fired
    downstream_rule_id for real AND _is_downstream_fact_active has
    confirmed the fact is actually active again. That write query knows
    how to set its OWN normal fields (confidence, status, source_rule,
    ...) but has no idea this module's auto_invalidated/
    invalidated_by_rule_id/invalidated_reason fields exist — they were
    never part of any rule's original contract, and neither are the
    reinstated_by_rule_id/reinstated_reason/reinstated_at fields this
    records now, an audit trail of WHY the fact came back matching the
    one already kept for WHY it was auto-invalidated. Deliberately kept
    separate from the rule files themselves rather than teaching every
    downstream rule's .cypher about a bookkeeping concern that belongs
    to the cascade, not the rule. (When the rule's own write couldn't
    restore status — the coalesce trap — _direct_reinstate sets status
    AND records this same bookkeeping itself as part of its own SET, so
    this function is not called on that path.)
    """
    if downstream_rule_id in _NETWORK_TYPE_BY_RULE_ID:
        session.run(
            _MARK_REINSTATED_MEMBERSHIP,
            subject_id=subject_id,
            network_type=_NETWORK_TYPE_BY_RULE_ID[downstream_rule_id],
            upstream_rule_id=upstream_rule_id,
            reason=reason,
            timestamp=timestamp,
            investigator_id=investigator_id,
        )
        return

    if downstream_rule_id in _CASE_FLAG_FIELDS:
        fields = _CASE_FLAG_FIELDS[downstream_rule_id]
        query = f"""
        MATCH (c:Case {{case_id: $case_id}})
        SET c.{fields['auto_invalidated']} = false,
            c.{fields['reinstated_by']} = $upstream_rule_id,
            c.{fields['reinstated_reason']} = $reason,
            c.{fields['reinstated_by_investigator']} = $investigator_id,
            c.{fields['reinstated_at']} = $timestamp
        REMOVE c.{fields['invalidated_by']}, c.{fields['invalidated_reason']},
               c.{fields['invalidated_by_investigator']}, c.{fields['invalidated_at']}
        """
        session.run(
            query,
            case_id=case_id,
            upstream_rule_id=upstream_rule_id,
            reason=reason,
            timestamp=timestamp,
            investigator_id=investigator_id,
        )
        return
    # Every recognized downstream target is handled by one of the two
    # branches above (_NETWORK_TYPE_BY_RULE_ID / _CASE_FLAG_FIELDS) — the
    # same two maps _auto_invalidate and _direct_reinstate dispatch on.
    # Falling through here would mean DOWNSTREAM_DEPENDENTS names a rule
    # neither map knows about, which _auto_invalidate already logs a
    # warning for and refuses to touch; there is nothing left to mark.


_HAS_MANUAL_REJECTION_QUERY = """
MATCH (rej:Rejection {relationship_type: $relationship_type, status: "active"})
WHERE rej.from_key = $from_key AND rej.to_key = $to_key
RETURN count(rej) > 0 AS has_manual_rejection
"""

_MEMBERSHIP_NETWORK_KEY_QUERY = """
MATCH (a:Subject {subject_id: $subject_id})-[:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork {network_type: $network_type})
RETURN n.network_key AS network_key
"""


def _has_manual_rejection(session, case_id: str, downstream_rule_id: str, subject_id: str) -> bool:
    """
    Whether an investigator's OWN, independent manual rejection
    (reasoning_layer.rejection.reject_inference — a real :Rejection
    audit node) exists for THIS specific downstream fact, as distinct
    from it being "rejected" solely because THIS module auto-invalidated
    it (which never writes a :Rejection node — see _auto_invalidate).

    _reinstate's direct-SET fallback must NEVER override a fact a human
    explicitly, independently rejected on its own — only a fact whose
    sole reason for being "rejected" is this module's own
    upstream-triggered bookkeeping. This is the check that keeps those
    two cases apart; see _reinstate's docstring, bug #1 vs bug #2.

    from_key/to_key encoding mirrors reasoning_layer.rejection's
    per-family convention exactly (that module is the only place that
    WRITES a :Rejection node, so this must match it byte for byte):
    network family -> (subject_id, "<network_type>:<network_key>");
    case-flag family -> (subject_id, case_id).
    """
    relationship_type = _REJECTION_RELATIONSHIP_TYPE_BY_RULE_ID.get(downstream_rule_id)
    if relationship_type is None:
        return False

    if downstream_rule_id in _NETWORK_TYPE_BY_RULE_ID:
        network_type = _NETWORK_TYPE_BY_RULE_ID[downstream_rule_id]
        record = session.run(
            _MEMBERSHIP_NETWORK_KEY_QUERY, subject_id=subject_id, network_type=network_type
        ).single()
        if not record or not record.get("network_key"):
            # No membership edge at all — nothing to have a rejection on.
            return False
        from_key = subject_id
        to_key = f"{network_type}:{record['network_key']}"
    elif downstream_rule_id in _CASE_FLAG_FIELDS:
        from_key = subject_id
        to_key = case_id
    else:
        return False

    record = session.run(
        _HAS_MANUAL_REJECTION_QUERY, relationship_type=relationship_type, from_key=from_key, to_key=to_key
    ).single()
    return bool(record and record["has_manual_rejection"])


def _reinstate(
    session,
    case_id: str,
    downstream_rule_id: str,
    subject_id: str,
    upstream_rule_id: str,
    reason: str,
    timestamp: str,
    investigator_id: Optional[str],
) -> bool:
    """
    Re-run downstream_rule_id's REAL write query for subject_id first —
    see the module docstring's "REVERT NEVER JUST FLIPS A STATUS BACK"
    — then VERIFY the fact is actually active afterward before claiming
    success, falling back to a direct SET if it isn't. Only attempts
    any of this (and only ever returns True) if this fact currently
    carries this module's own auto-invalidated flag; returns False
    (does nothing) immediately otherwise, so a fact that was always
    active, or one an investigator rejected manually, is never touched
    by this function.

    TWO DISTINCT BUGS THIS GUARDS AGAINST, BOTH FOUND IN PRODUCTION:

    1. THE STALE :REJECTION GUARD. downstream_rule_id's own .cypher file
       has its OWN `NOT EXISTS {...:Rejection...}` guard (every rule
       file has one — see reasoning_layer/rejection.py's module
       docstring). If the SAME fact this function is trying to
       reinstate was ALSO, independently, manually rejected via
       reject_inference at some point, a real :Rejection node exists
       for it — and re-running the rule here does NOT delete that node
       (only an explicit /revert_rejection call on downstream_rule_id
       itself does). The rule's own guard then correctly blocks the
       re-assertion entirely: execute_rules "runs" but writes NOTHING.

    2. THE COALESCE TRAP — subtler, and NOT caught by only checking
       whether execute_rules wrote anything. The network family's write
       queries (Rule_02/04/06/09) set their relationship's status with
       `ra.status = coalesce(ra.status, "active")` — DELIBERATELY, so a
       normal pipeline re-run can never resurrect a fact a human
       explicitly rejected (see e.g. rule_02_employer_fraud_network
       .cypher's own comment on that line). But coalesce() means "keep
       whatever is already there if it's non-null" — so once THIS
       MODULE sets status="rejected" during auto-invalidation, no
       amount of re-running the rule can ever set it back to "active"
       on its own: coalesce(ra.status, "active") evaluates to
       "rejected" forever. The rule DOES still write its other fields
       (asserted_at, confidence, allegation_type) unconditionally, so
       execute_rules reports writes > 0 — a naive "did it write
       anything" check reports success despite status never having
       moved. (Rule_08/13's case-level status fields are NOT
       coalesce-guarded, so they are not affected by this specific
       trap — but bug #1 still applies to them.)

    Both are why this function checks the ACTUAL resulting state
    (_is_downstream_fact_active) rather than trusting execute_rules'
    return value as a proxy for it. When the fact is still not active,
    it checks ONE more thing before deciding what to do:
    _has_manual_rejection — is there a real :Rejection audit node for
    THIS specific fact, meaning an investigator independently rejected
    it themselves? If so, this function stops there and leaves it
    rejected: an upstream revert must never silently override a human's
    own decision on a different fact. Only when NO manual rejection
    exists — meaning the coalesce trap (bug #2) is the only remaining
    explanation — does it fall back to a direct SET (_direct_reinstate).
    That fallback is exactly as safe as the rule's own write would have
    been in that case: this module's own auto-invalidation never went
    through a :Rejection node or the rule's write path either, so there
    is nothing for the fallback to respect that re-running the rule
    would have respected instead.
    """
    if not _was_auto_invalidated(session, case_id, downstream_rule_id, subject_id):
        return False

    scope = scope_resolver.resolve_scope(case_id=case_id, subject_id=subject_id)
    registry = rule_registry.load_registry()
    # Its own session — rule_engine.execute_rules manages one internally,
    # and this module has no reason to fight that; Neo4j sessions from
    # the same driver are cheap and independent, so nesting one inside
    # the caller's own `with get_session()` block is safe.
    results = rule_engine.execute_rules([downstream_rule_id], scope, registry)
    record = next((r for r in results if r["rule_id"] == downstream_rule_id), None)
    writes = int(record["writes"]) if record and record.get("writes") else 0

    if _is_downstream_fact_active(session, case_id, downstream_rule_id, subject_id):
        _mark_reinstated(
            session, case_id, downstream_rule_id, subject_id, upstream_rule_id, reason, timestamp, investigator_id
        )
        return True

    if _has_manual_rejection(session, case_id, downstream_rule_id, subject_id):
        logger.info(
            "cascade: %s still carries an INDEPENDENT manual rejection for "
            "case_id=%s subject_id=%s (writes=%d) — leaving it rejected. An "
            "investigator's own decision on this fact is never overridden by "
            "an upstream revert; an explicit /revert_rejection on %s itself "
            "is required to clear it.",
            downstream_rule_id,
            case_id,
            subject_id,
            writes,
            downstream_rule_id,
        )
        return False

    logger.info(
        "cascade: %s's own write query ran (writes=%d) but did not restore "
        "case_id=%s subject_id=%s to active, and no independent manual "
        "rejection explains it — falling back to a direct SET (coalesce "
        "trap, see _reinstate's docstring bug #2)",
        downstream_rule_id,
        writes,
        case_id,
        subject_id,
    )
    changed = _direct_reinstate(
        session, case_id, downstream_rule_id, subject_id, upstream_rule_id, reason, timestamp, investigator_id
    )
    if not changed:
        logger.warning(
            "cascade: reinstate FAILED for case_id=%s rule_id=%s subject_id=%s — "
            "neither the rule's own re-fire nor the direct fallback SET changed "
            "anything. Leaving this module's auto_invalidated bookkeeping "
            "untouched; the fact remains rejected.",
            case_id,
            downstream_rule_id,
            subject_id,
        )
    return changed


def _walk(
    session,
    case_id: str,
    upstream_rule_id: str,
    subject_ids: List[str],
    reason: str,
    timestamp: str,
    investigator_id: Optional[str],
    direction: str,
    changes: List[Dict[str, Any]],
    depth: int = 0,
) -> None:
    if depth >= _MAX_WALK_DEPTH:
        return

    dependents = rule_registry.DOWNSTREAM_DEPENDENTS.get(upstream_rule_id, [])
    for dependent in dependents:
        downstream_rule_id = dependent["rule_id"]
        relationship_type = dependent["relationship_type"]

        for subject_id in subject_ids:
            if not subject_id:
                continue
            # Asymmetric on purpose — see the two helpers' docstrings.
            # REJECT: a multi-contributor fact (Rule_08: Rule_07's
            # prior-guilty finding AND a fraud-network membership from
            # Rules 2/4/6/9) is auto-rejected only once EVERY
            # contributor is inactive — rejecting Rule_01 alone (network
            # leg only) must NOT auto-reject Rule_08 while Rule_07 is
            # still active.
            # REVERT: the same fact is auto-reinstated only once EVERY
            # contributor is active again — reverting Rule_07 alone must
            # NOT reinstate Rule_08 while Rule_01/02's network leg is
            # still rejected.
            # A rule with a single required relationship_type
            # (Rule_02/04/06, Rule_13) collapses both checks back to
            # that one condition, so it is unaffected either way.
            condition_holds = (
                _all_upstream_conditions_hold(session, subject_id, downstream_rule_id)
                if direction == "revert"
                else _any_upstream_condition_holds(session, subject_id, downstream_rule_id)
            )

            if direction == "reject" and not condition_holds:
                changed = _auto_invalidate(
                    session,
                    case_id,
                    downstream_rule_id,
                    subject_id,
                    upstream_rule_id,
                    reason,
                    timestamp,
                    investigator_id,
                )
                if changed:
                    changes.append(
                        {
                            "rule_id": downstream_rule_id,
                            "subject_id": subject_id,
                            "action": "auto_invalidated",
                            "invalidated_by_rule_id": upstream_rule_id,
                            "reason": reason,
                            # Full audit parity with a manual rejection's
                            # own rejected_by/rejected_at fields — the
                            # investigator who triggered the UPSTREAM
                            # reject that caused this hop, and exactly
                            # when this hop happened (never the upstream
                            # rejected_at repeated blindly down every hop
                            # of a multi-level chain, even though the
                            # upstream call passes one shared `timestamp`
                            # into every hop of a single cascade walk —
                            # see reject_inference/revert_rejection, which
                            # compute `timestamp` once and reuse it for
                            # the whole walk).
                            "investigator_id": investigator_id,
                            "changed_at": timestamp,
                        }
                    )
                    logger.info(
                        "cascade: AUTO-INVALIDATED case_id=%s rule_id=%s subject_id=%s "
                        "(condition broke: %s no longer active) invalidated_by=%s "
                        "investigator_id=%s reason=%r",
                        case_id,
                        downstream_rule_id,
                        subject_id,
                        relationship_type,
                        upstream_rule_id,
                        investigator_id,
                        reason,
                    )
                    # Same original reason carries through every hop —
                    # "why this whole chain is rejected" is always the
                    # top-level investigator's own reason text, not a
                    # system-generated one for each intermediate hop.
                    _walk(
                        session,
                        case_id,
                        downstream_rule_id,
                        [subject_id],
                        reason,
                        timestamp,
                        investigator_id,
                        direction,
                        changes,
                        depth + 1,
                    )
                else:
                    # The upstream condition genuinely broke (condition_holds
                    # is False — this branch is only reached then), but
                    # _auto_invalidate found nothing in the graph to
                    # invalidate: no active downstream_rule_id fact exists
                    # for this subject at all (it may simply never have
                    # fired — nothing wrong there), OR one exists but this
                    # module's own targeting missed it (a network_type
                    # mismatch, a case-flag subject_field mismatch, etc — a
                    # real bug worth investigating). Logged at WARNING,
                    # distinct from the DEBUG "condition still holds, no
                    # cascade needed" case below, specifically so this
                    # situation is distinguishable from server logs alone
                    # instead of both looking identical (empty
                    # cascade_changes) to the caller.
                    logger.warning(
                        "cascade: condition broke (%s no longer active for subject_id=%s) "
                        "but no active %s fact was found to auto-invalidate for case_id=%s — "
                        "either %s never fired for this subject (not a problem) or this "
                        "module's targeting missed an existing fact (worth investigating)",
                        relationship_type,
                        subject_id,
                        downstream_rule_id,
                        case_id,
                        downstream_rule_id,
                    )

            elif direction == "revert" and condition_holds:
                changed = _reinstate(
                    session,
                    case_id,
                    downstream_rule_id,
                    subject_id,
                    upstream_rule_id,
                    reason,
                    timestamp,
                    investigator_id,
                )
                if changed:
                    changes.append(
                        {
                            "rule_id": downstream_rule_id,
                            "subject_id": subject_id,
                            "action": "reinstated",
                            "invalidated_by_rule_id": None,
                            "reason": reason,
                            "investigator_id": investigator_id,
                            "changed_at": timestamp,
                        }
                    )
                    logger.info(
                        "cascade: REINSTATED case_id=%s rule_id=%s subject_id=%s "
                        "(condition restored: %s active again) investigator_id=%s reason=%r",
                        case_id,
                        downstream_rule_id,
                        subject_id,
                        relationship_type,
                        investigator_id,
                        reason,
                    )
                    _walk(
                        session,
                        case_id,
                        downstream_rule_id,
                        [subject_id],
                        reason,
                        timestamp,
                        investigator_id,
                        direction,
                        changes,
                        depth + 1,
                    )
                else:
                    # Mirror of the reject-direction warning above: the
                    # condition genuinely came back (condition_holds is
                    # True — this branch is only reached then), but
                    # _reinstate found nothing to bring back — either the
                    # fact was never auto-invalidated in the first place
                    # (an investigator manually rejected it instead, which
                    # _reinstate deliberately never touches — not a
                    # problem), or something about the coalesce-trap
                    # fallback / rule re-run failed silently (worth
                    # investigating).
                    logger.warning(
                        "cascade: condition restored (%s active again for subject_id=%s) "
                        "but no auto-invalidated %s fact was found to reinstate for "
                        "case_id=%s — either it was never auto-invalidated (e.g. a manual "
                        "rejection instead, not a problem) or reinstatement failed silently "
                        "(worth investigating)",
                        relationship_type,
                        subject_id,
                        downstream_rule_id,
                        case_id,
                    )

            else:
                # The common, entirely expected case: this subject's
                # upstream condition for downstream_rule_id didn't change
                # state in the direction that would warrant a cascade —
                # reject: it still holds (e.g. subject_id has ANOTHER
                # active edge of relationship_type, so downstream_rule_id's
                # condition is untouched by this one instance being
                # rejected); revert: it still doesn't hold. DEBUG, not
                # WARNING — this is the majority-case, silent-by-design
                # outcome, but now at least traceable if someone needs to
                # confirm "did cascade even check this subject" rather
                # than only ever seeing it for changes that did happen.
                logger.debug(
                    "cascade: no %s needed for case_id=%s rule_id=%s subject_id=%s — "
                    "%s condition_holds=%s (checked because %s changed)",
                    "auto-invalidation" if direction == "reject" else "reinstatement",
                    case_id,
                    downstream_rule_id,
                    subject_id,
                    relationship_type,
                    condition_holds,
                    upstream_rule_id,
                )


def cascade_reject(
    session,
    case_id: str,
    upstream_rule_id: str,
    affected_subject_ids: List[str],
    reason: str,
    timestamp: str,
    investigator_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Call once, immediately after reasoning_layer.rejection.reject_inference's
    own Neo4j write for ONE rejected instance, using the SAME open
    session (auto-commit per statement, matching every other write in
    that function). `affected_subject_ids` is every Subject the
    rejected instance itself touches — both endpoints for the
    symmetric-edge family (Rules 1/3/5), the single subject for every
    other upstream family (Rules 2/4/6/7/9). `reason` is the
    investigator's own reason text for the upstream rejection —
    threaded onto every downstream fact this walk auto-invalidates
    (invalidated_reason), so a caller looking at the DOWNSTREAM fact
    alone can see why it's rejected without having to separately look
    up the upstream rule's own rejection record. `investigator_id` is
    the same investigator who issued the upstream reject — stamped
    onto every downstream fact's own invalidated_by_investigator_id
    field, and echoed back on every entry in the returned list, so a
    caller never has to cross-reference the upstream rejection record
    just to learn who is responsible for a cascaded change.

    Returns a list of {rule_id, subject_id, action: "auto_invalidated",
    invalidated_by_rule_id, investigator_id, changed_at, reason}
    records — always non-silent, even when nothing needed to change (an
    empty list, never a suppressed side effect the caller can't see).
    """
    changes: List[Dict[str, Any]] = []
    _walk(
        session, case_id, upstream_rule_id, affected_subject_ids, reason, timestamp, investigator_id, "reject", changes
    )
    return changes


def cascade_revert(
    session,
    case_id: str,
    upstream_rule_id: str,
    affected_subject_ids: List[str],
    reason: str,
    timestamp: str,
    investigator_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    The exact mirror of cascade_reject — see the module docstring's
    "REVERT NEVER JUST FLIPS A STATUS BACK" for what "reinstated" means
    here. Called immediately after
    reasoning_layer.rejection.revert_rejection's own Neo4j write, same
    session, same affected_subject_ids convention. `reason` is the
    investigator's own reason text for the upstream revert — threaded
    onto every downstream fact this walk reinstates
    (reinstated_reason), the same way cascade_reject threads the
    rejection reason onto invalidated_reason. `investigator_id` mirrors
    cascade_reject's own — the investigator who issued the upstream
    revert, stamped onto every downstream fact's own
    reinstated_by_investigator_id field and echoed on every returned
    change entry.
    """
    changes: List[Dict[str, Any]] = []
    _walk(
        session, case_id, upstream_rule_id, affected_subject_ids, reason, timestamp, investigator_id, "revert", changes
    )
    return changes