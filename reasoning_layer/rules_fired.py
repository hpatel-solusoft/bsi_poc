"""
Owns: assembling the `rules_fired` block — the shared output contract of
the Reasoning Pipeline (Functional Specification A.4).

This block is consumed by Context Enrichment, Investigation Plan,
Copilot, Report Generation and Rule Audit. A.4 is blunt about the stakes:
"If it is absent or incorrectly structured, those Phase 2 improvements
fail silently." So it is assembled in exactly one place, from Neo4j,
after the rules have run — never reconstructed by a caller, and never
cached in Postgres (Data Persistence C.2: Neo4j is the system of record
for inferred relationships; Postgres holds no inferred-relationship
state).

Contract, per entry (A.4):
    rule_id      — Rule_01_... through Rule_14_...
    fired        — did this rule match a pattern for this subject
    confidence   — High / Medium / Unresolved
    corroborated — was the inferred fact also confirmed by narrative
                   evidence (Rule 14; Wave 2 and structural rules only)

Everything beyond those four fields (evidence_count, instances, wave,
skipped_reason) is additive and safe for existing consumers to ignore —
but it is what makes /rule_audit and the investigator-facing "why did
this fire" panel possible without a second round of queries. Each entry
in `instances` additionally carries asserted_at, subject_id_a,
subject_id_b, relationship_type and match_id (frontend follow-up to
AI-28/AI-33) — the same instance-targeting fields rule_audit.py already
puts on every row it returns — so a caller can POST /reject_inference
or /revert_rejection straight off an /intake response without a second
call to GET /rule_audit first.

Does NOT own: rule execution (rule_engine.py) or rule content.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from reasoning_layer import rejection, rule_inference, rule_registry
from reasoning_layer.neo4j_client import get_session
from reasoning_layer.rejection import build_match_id

logger = logging.getLogger(__name__)

_CONFIDENCE_ORDER = {"Unresolved": 0, "Medium": 1, "High": 2}

from reasoning_layer.rules_fired_queries import PROP_RULES, REL_RULES

# Rule 12's `corroborated` is deliberately wage_corroboration_verified, not
# a Rule 14 flag: for this rule, "corroborated" means the wage period was
# actually checked against the case's fraud date range and overlapped —
# rather than the rule firing on an existing wage record with no dates
# available to verify against. See the rule file.


# Instance keys, in the order they are emitted. Only the ones a given rule
# actually produces appear on its instances — a subject-to-subject rule has
# no related_case_id, and inventing a null one would suggest the rule looked
# for a case and found none.
_INSTANCE_KEYS = (
    "subject_id",
    "related_subject_id",
    "related_case_id",
    "related_network_key",
    "allegation_type",
    "allegation_id",
)


def _stamp_member_match_ids(rule_id: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    """
    AI-28 completeness gap: the network family (Rule_02/04/06/09)
    collapses every member into ONE instance row for one readable
    narrative line (see the REL_RULES network queries' own "Collapse
    to ONE row per network" comment) — but that meant the row's single
    top-level match_id could only ever target the ANCHOR member
    (row["subject_id"]), unlike reasoning_layer/rule_audit.py and
    reasoning_layer/fraud_network.py, which return one row PER member
    and so already let an investigator reject/revert ANY specific
    member directly. This stamps a member-specific match_id onto every
    entry in detail["members"], decodable back to (rule_id, that
    member's own subject_id, the network composite key) — byte-
    identical to what rule_audit.py would build for that same member.

    Degrades to leaving a member's match_id as None (never raises)
    when network_type or network_key is missing or a member has no
    subject_id — a display concern must never crash the whole
    rules_fired build over one incomplete row. A no-op, returning
    `detail` completely unchanged, when there is no "members" list at
    all (every non-network-family rule's detail, and a malformed
    network-family row).

    Returns a NEW detail dict with a NEW members list of NEW member
    dicts — the row/detail this module was handed, and its own
    "members" list and dicts, are never mutated in place.
    """
    members = detail.get("members")
    if not members:
        return detail

    network_type = detail.get("network_type")
    network_key = detail.get("network_key")
    network_composite = f"{network_type}:{network_key}" if network_type and network_key else None

    stamped_members = []
    for member in members:
        member_copy = dict(member)
        member_subject_id = member_copy.get("subject_id")
        member_copy["match_id"] = (
            build_match_id(rule_id, member_subject_id, network_composite)
            if member_subject_id and network_composite
            else None
        )
        stamped_members.append(member_copy)

    new_detail = dict(detail)
    new_detail["members"] = stamped_members
    return new_detail


def _instance(rule_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """
    One concrete match: WHICH subjects/records this rule fired on, with the
    entity and field detail behind it and a readable inference line.

    `detail` carries the fields the rule actually matched on — the address,
    the employer FEIN, the network members. Without it "Rule 3 fired" tells
    an investigator that something matched but not what, which is not
    enough to accept or reject the inference.

    AI-30 / frontend follow-up: also carries `asserted_at`, `subject_id_a`,
    `subject_id_b`, `relationship_type` and `match_id` — exactly the fields
    reasoning_layer/rule_audit.py already stamps onto every row it returns
    — so the frontend can POST /reject_inference or /revert_rejection
    straight off an /intake response's rules_fired.instances without a
    second round trip to GET /rule_audit first just to obtain a match_id.
    subject_id_a/subject_id_b/match_id are computed via
    reasoning_layer.rejection.instance_endpoints/build_match_id, the same
    per-rule-family encoding rule_audit.py and fraud_network.py already
    use — see that function's docstring for the family-by-family mapping.
    Omitted entirely for rule_ids rejection.py does not track as
    rejectable (Rule 14, a confidence modifier with no independent
    instance of its own), rather than emitting a match_id that would
    always 404 if a caller tried to use it.

    For the case-flag family (Rule 8/13), subject_id_a comes straight
    off row["subject_id"] — Rule 8's query reads its own
    risk_escalation_subject_id property; Rule 13's query returns the
    $subject_id query parameter build_rules_fired binds from
    scope["primary_subject_id"] (Rule 13 stamps no escalating-subject
    id onto :Case itself — see that query's own comment). Either way,
    no separate parameter is needed here: row["subject_id"] is already
    whatever the right value is, or None if a primary subject was never
    resolved, in which case match_id degrades to None rather than
    building a token nobody could ever act on.

    For the network family (Rule 2/4/6/9), every member in
    detail["members"] additionally gets its OWN match_id via
    _stamp_member_match_ids — see that function's docstring.
    """
    instance = {key: row[key] for key in _INSTANCE_KEYS if row.get(key) is not None}
    detail = {k: v for k, v in (row.get("detail") or {}).items() if v is not None and v != []}
    if detail:
        detail = _stamp_member_match_ids(rule_id, detail)
        instance["detail"] = detail
    instance["confidence"] = row.get("confidence") or "Unresolved"
    instance["corroborated"] = bool(row.get("corroborated", False))
    if row.get("asserted_at"):
        instance["asserted_at"] = row["asserted_at"]

    # --- rejection state (Human-in-the-Loop, Section 5.2) ---
    # A rejected instance STAYS in the block. It used to be filtered out of
    # the query entirely, which meant the investigator who rejected it had
    # nothing left on screen to revert from — the only way back was
    # /rule_audit, a different endpoint with a different shape. Keeping the
    # row and flipping a status is what makes reject and revert two
    # directions of one control rather than a one-way door.
    status = row.get("status") or "active"
    instance["status"] = status
    instance["revertable"] = status == "rejected"
    audit = {k: v for k, v in (row.get("rejection") or {}).items() if v is not None and v != ""}
    if audit:
        # Who rejected it, when, and why — and the same for a previous
        # revert. An investigator deciding whether to revert someone else's
        # rejection needs the reason, not just the fact of it.
        instance["rejection"] = audit
    # Names are a presentation concern, owned by rule_inference so
    # reformatting them never touches this query module.
    for name_key in ("first_name", "last_name", "related_first_name", "related_last_name"):
        if row.get(name_key) is not None:
            instance[name_key] = row[name_key]

    # --- reject/revert targeting fields (v3 contract, AI-28/AI-33) ---
    subject_id_a, subject_id_b = rejection.instance_endpoints(
        rule_id,
        case_id=row.get("related_case_id"),
        subject_id=row.get("subject_id"),
        related_subject_id=row.get("related_subject_id"),
        related_case_id=row.get("related_case_id"),
        network_type=detail.get("network_type"),
        network_key=detail.get("network_key"),
        allegation_id=row.get("allegation_id"),
    )
    if subject_id_a:
        instance["subject_id_a"] = subject_id_a
        instance["subject_id_b"] = subject_id_b
        instance["relationship_type"] = rule_inference.rule_label(rule_id)
        instance["match_id"] = build_match_id(rule_id, subject_id_a, subject_id_b)

    return rule_inference.enrich_instance(rule_id, instance)


def _summarise(rule_id: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Roll instance rows up into the rule-level summary.

    The rule-level `confidence` is the HIGHEST across instances and
    `corroborated` is true if ANY instance was corroborated. Both are
    deliberately optimistic: the rule-level flags answer "is there anything
    here worth an investigator's attention", and per-instance detail — the
    Medium, uncorroborated match sitting behind a High one — is preserved
    in `instances` rather than averaged away.
    """
    instances = [_instance(rule_id, row) for row in rows]
    active = [i for i in instances if i["status"] == "active"]
    rejected = [i for i in instances if i["status"] == "rejected"]
    count = len(active)

    # EVERY rolled-up figure below is computed from ACTIVE instances only,
    # and that is the whole safety property of this change. `instances` now
    # carries rejected findings so the UI can show and revert them — but
    # rules_fired also feeds the Copilot's context, Investigation Plan and
    # Report Generation, and a fact an investigator has explicitly rejected
    # must never be handed to any of them as live evidence. Visible in the
    # payload, absent from the counts.
    confidences = [i["confidence"] for i in active if i["confidence"]]
    confidence = max(confidences, key=lambda c: _CONFIDENCE_ORDER.get(c, 0)) if confidences else "Unresolved"

    if count and rejected:
        rule_status = "partially_rejected"
    elif rejected:
        rule_status = "rejected"
    elif count:
        rule_status = "active"
    else:
        rule_status = "not_fired"

    return {
        # Unchanged meaning: is there a LIVE finding here. A rule whose only
        # findings were rejected reports fired=false, exactly as it did when
        # those rows were dropped from the query — downstream consumers see
        # no behaviour change from this work.
        "fired": count > 0,
        # A rule that did not fire has no confidence to report. "Unresolved"
        # is the correct value here (A.4's own enum) — not None, and not a
        # cheerful "High" inherited from a previous run.
        "confidence": confidence if count > 0 else "Unresolved",
        "corroborated": any(i["corroborated"] for i in active),
        "evidence_count": count,
        # `matched` is the flag a UI renders the row on: this rule produced
        # something, whether or not it is currently accepted. `fired` alone
        # cannot serve that purpose without either hiding rejected rows or
        # misreporting rejected facts as live to the LLM consumers.
        "matched": len(instances) > 0,
        "status": rule_status,
        "rejected_count": len(rejected),
        "revertable": len(rejected) > 0,
        "instances": instances,
    }


def instance_identity_key(instance: Dict[str, Any]) -> tuple:
    """
    THE stable, run-independent identity of one logical instance —
    the single definition every caller that needs to tell "is this the
    same fact" must reuse, rather than each re-deriving its own notion
    of identity (which is exactly how the case-level merge bug below
    was introduced).

    Built ONLY from _INSTANCE_KEYS (subject_id / related_subject_id /
    related_case_id / related_network_key / allegation_type /
    allegation_id) — the fields that describe WHAT the instance is.
    Deliberately excludes every field that varies by WHEN or BY WHICH
    RUN the row was produced rather than by what it describes:
    asserted_at (re-stamped by Neo4j on every re-assert of an otherwise
    identical fact), status/rejection/revertable (case-level workflow
    state, not identity), confidence/corroborated (rolled up
    separately), detail/first_name/last_name/etc. (presentation), and
    match_id/subject_id_a/subject_id_b/relationship_type (derived FROM
    this same identity, not part of it).

    Used by:
      * _dedupe_rows (below) — collapses duplicate PHYSICAL
        relationships the graph already has for one rule's query
        within a single build_rules_fired() call.
      * reasoning_layer.pipeline._merge_rules_fired — collapses the
        same logical instance appearing in more than one subject's
        per-subject rules_fired block when run_pipeline_for_case folds
        them into one case-level block.

    A caller with a network-family instance (Rule 2/4/6/9) must NOT use
    this alone: `subject_id` there is an arbitrary per-run anchor, not
    part of the network's identity — see _merge_rules_fired's own
    related_network_key branch, which takes priority over this key for
    that family.
    """
    return tuple(instance.get(k) for k in _INSTANCE_KEYS)


def _dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Collapse rows describing the SAME logical instance down to one,
    keyed on instance_identity_key (subject_id / related_subject_id /
    related_case_id / related_network_key / allegation_type).

    Defense in depth against duplicate PHYSICAL relationships in the
    graph for the same logical fact. The known way this happens: MERGE
    on an undirected relationship pattern — `MERGE (a)-[r:TYPE]-(b)` —
    is not guaranteed idempotent (Cypher's own docs flag undirected
    MERGE as unreliable); under some execution-plan orderings across
    repeated pipeline runs it can create a second parallel relationship
    for the same pair instead of matching the one already there. The
    three symmetric-edge write rules (reasoning_layer/rules/wave1/
    rule_01_shared_employer.cypher, rule_03_shared_address.cypher,
    rule_05_alias_identity.cypher) now MERGE in a fixed, deterministic
    direction (already enforced by their own `a.subject_id < b.subject_id`
    guard) to stop NEW duplicates from forming — but that does nothing
    for a duplicate a graph already has, so every REL_RULES/PROP_RULES
    query is deduped here regardless of family, rather than trusting
    each Cypher file to never produce one.

    Prefers a row that CARRIES rejection/revert/cascade audit data over
    one that doesn't, rather than blindly keeping whichever row a given
    query's ORDER BY happened to place first. This matters specifically
    for a pre-existing duplicate: if a case already has two parallel
    relationships for the same pair from before the deterministic-MERGE
    fix above landed, an investigator's reject/revert only ever touches
    ONE of them (whichever the locate query's WHERE clause matches), so
    the OTHER keeps sailing through with no audit trail at all — a
    plain "keep the first" dedupe could then discard the very row
    documenting what an investigator just did, in favor of the "clean"
    duplicate that never saw the action, making a real reject or revert
    look like it silently vanished. Every query's own ORDER BY still
    decides ties between two rows that are equally (un)informative — a
    row with audit data always wins regardless of where it falls in
    that order.
    """
    seen: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for row in rows:
        key = instance_identity_key(row)
        if key not in seen:
            seen[key] = row
            order.append(key)
            continue
        if not _row_has_audit_trail(seen[key]) and _row_has_audit_trail(row):
            seen[key] = row
    return [seen[key] for key in order]


def _row_has_audit_trail(row: Dict[str, Any]) -> bool:
    """Whether this row's own `rejection` map (as returned straight off
    the Cypher — see _dedupe_rows's docstring for why this, not just
    `row.get("status") == "rejected"`, is the right test) carries any
    reject/revert/cascade field at all, current status notwithstanding."""
    rejection = row.get("rejection") or {}
    return any(v is not None and v != "" for v in rejection.values())


def build_rules_fired(scope: Dict[str, Any], execution_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Build the full 14-entry rules_fired block for one pipeline run.

    Always returns 14 entries, in rule-number order, whether or not each
    rule fired — the block is a fixed-shape contract, not a list of hits.
    A consumer iterating it can rely on every rule_id being present.

    `execution_records` (from rule_engine) contributes the skipped_reason,
    so a rule disabled in the registry reads as fired=false +
    skipped_reason="disabled_in_registry" rather than as an ordinary miss.
    """
    executed_by_id = {rec["rule_id"]: rec for rec in execution_records}
    params = {
        "scope_subject_ids": scope["scope_subject_ids"],
        "scope_case_ids": scope["scope_case_ids"],
        "case_id": scope["case_id"],
        # Rule 13 stamps no escalating-subject id onto :Case itself (see
        # that query's own comment) — its RETURN reads this bound
        # $subject_id parameter instead, so row["subject_id"] is
        # populated for it exactly like every other rule, and
        # _instance() needs no separate case_id/primary_subject_id
        # threading to build its match_id.
        "subject_id": scope.get("primary_subject_id"),
    }

    block: List[Dict[str, Any]] = []
    with get_session() as session:
        for rule_id in rule_registry.ALL_RULE_IDS:
            query = REL_RULES.get(rule_id) or PROP_RULES.get(rule_id)
            rows = session.run(query, **params).data()
            rows = _dedupe_rows(rows)
            summary = _summarise(rule_id, rows)
            execution = executed_by_id.get(rule_id, {})
            block.append(
                {
                    "rule_id": rule_id,
                    "fired": summary["fired"],
                    "confidence": summary["confidence"],
                    "corroborated": summary["corroborated"],
                    # --- additive, beyond A.4's four required fields ---
                    # What this rule looks for, from config/rule.yaml — so the
                    # Inference panel can explain the rule itself, not only the match.
                    "rule_description": rule_inference.rule_description(rule_id),
                    "relationship_type": rule_inference.rule_label(rule_id),
                    "evidence_count": summary["evidence_count"],
                    # --- rejection / revert state (Human-in-the-Loop) ---
                    # `status` is the rule-level roll-up: active, rejected,
                    # partially_rejected, or not_fired. `revertable` tells the UI
                    # whether POST /revert_rejection has anything to act on for
                    # this case_id + rule_id, so it can enable the control without
                    # a second call to /rule_audit.
                    "matched": summary["matched"],
                    "status": summary["status"],
                    "rejected_count": summary["rejected_count"],
                    "revertable": summary["revertable"],
                    # Which concrete subjects/records this rule fired on. Without
                    # it, "Rule 3 fired, evidence_count 2" tells an investigator
                    # something happened but not to whom — and the co-subject
                    # pipeline runs below make multi-instance results the norm.
                    "instances": summary["instances"],
                    "wave": (
                        1
                        if rule_id in rule_registry.WAVE_1_RULE_IDS
                        else 2 if rule_id in rule_registry.WAVE_2_RULE_IDS else 0
                    ),
                    "writes_this_run": execution.get("writes", 0),
                    "skipped_reason": execution.get("skipped_reason"),
                }
            )

    # Stamp the rule-level display fields (number, display name, heading)
    # onto every entry. In-memory over rows already fetched: no extra
    # queries, no change to any .cypher file.
    rule_inference.render_block(block)

    fired_count = sum(1 for entry in block if entry["fired"])
    rejected_count = sum(entry["rejected_count"] for entry in block)
    # DEBUG, not INFO — same reasoning as reasoning_layer/scope.py's
    # identical downgrade: fires once per subject in the reasoning
    # population, and reasoning_layer.pipeline's case-level
    # "run_pipeline_for_case: ... rules_fired=X/14" is the right INFO
    # granularity for routine operation.
    logger.debug(
        "rules_fired: case_id=%s subject_id=%s %d/%d rules fired, "
        "%d rejected instance(s) retained for revert",
        scope["case_id"],
        scope["primary_subject_id"],
        fired_count,
        len(block),
        rejected_count,
    )
    return block