"""
Owns: the 12 fixed, parameterised Cypher templates the Investigation
Copilot dispatches for graph-reasoning questions (AI-17 / Sections 6.3,
8.6, 9.3).

Section 6.3 is precise about the contract: "The LLM selects a template and
supplies parameters; it does not generate Cypher." Every query in this
file is therefore a fixed string with $parameters — no interpolation of
caller input into Cypher text anywhere, so a question cannot become an
injection.

WHY THESE ARE MANIFEST TOOLS (and the other reasoning_layer functions are
not): every earlier Phase 2 graph function is DETERMINISTIC — the route
knows it must run check_network_match, or find_structural_matches, before
or after the loop, so no model decision is involved and no manifest entry
is warranted. These 12 are the opposite: the investigator asks free text,
and only the LLM can decide which template answers it. Section 9.3 shows
that decision resolving through the dispatcher's three gates against
manifest.yaml, exactly like an AppWorks tool. That is the unified catalog
of Section 8.6 — AppWorks tools and Cypher templates governed by one
dispatcher.

READ-ONLY, ALWAYS. Copilot never re-triggers the Reasoning Pipeline under
any condition (Section 6.3, Principle 10). Nothing here calls
pipeline.run_pipeline, and every statement below is a MATCH/RETURN with no
CREATE, MERGE, SET or DELETE. Copilot reads the already-reasoned graph.

REJECTED FACTS ARE SURFACED, NEVER PRESENTED AS CURRENT (Section 6.3,
Principle 14). Investigator review writes status="rejected" onto an
inferred relationship, or a :Rejection node. Templates that return
inferred facts filter to status="active" for the live answer AND report
the rejected count alongside, so an answer can say "two connections, and
one further connection was reviewed and rejected" instead of silently
dropping it or wrongly asserting it.

Does NOT own: the pipeline, rule content, the deterministic route-level
graph calls (graph_queries / context_enrichment / similar_cases /
risk_signals / investigation_tasks), or any write path.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

_COMPUTED_BY = "reasoning_layer.copilot_templates"


def _envelope(result: Dict[str, Any]) -> Dict[str, Any]:
    """Standard {result, provenance} envelope — identical in shape to what
    an AppWorks tool returns, so the dispatcher and agent_runner cannot
    tell a template result from an AppWorks one."""
    return {
        "result": result,
        "provenance": graph_provenance(_COMPUTED_BY),
    }


def _require(**params: Any) -> Dict[str, str]:
    """Validate required template parameters. The dispatcher's GATE 2
    already checks presence, but a template is also callable directly and
    must not send an empty string into a MATCH."""
    cleaned: Dict[str, str] = {}
    for name, value in params.items():
        text = str(value).strip() if value is not None else ""
        if not text:
            raise ValueError(f"{name} is required and must be non-empty")
        cleaned[name] = text
    return cleaned


def _rows(query: str, **params: Any) -> List[Dict[str, Any]]:
    with get_session() as session:
        return session.run(query, **params).data()


def _single(query: str, **params: Any) -> Optional[Dict[str, Any]]:
    with get_session() as session:
        record = session.run(query, **params).single()
    return dict(record) if record is not None else None


# ---------------------------------------------------------------------------
# 1. get_subject_connections — who is this subject connected to?
# ---------------------------------------------------------------------------
_SUBJECT_CONNECTIONS = """
MATCH (s:Subject {subject_id: $subject_id})
OPTIONAL MATCH (s)-[r:SHARES_EMPLOYER_WITH|SHARES_ADDRESS_WITH|SHARES_ALIAS_PATTERN_WITH|IS_CO_SUBJECT_WITH]-(other:Subject)
WITH s,
     collect(DISTINCT CASE WHEN coalesce(r.status, "active") = "active" THEN {
        subject_id:      other.subject_id,
        connection_type: type(r),
        confidence:      r.confidence,
        corroborated:    coalesce(r.corroborated, false),
        source_rule:     r.source_rule
     } END) AS active_raw,
     collect(DISTINCT CASE WHEN r.status = "rejected" THEN {
        subject_id:      other.subject_id,
        connection_type: type(r),
        rejected_reason: r.rejection_reason
     } END) AS rejected_raw
RETURN [x IN active_raw   WHERE x IS NOT NULL AND x.subject_id IS NOT NULL] AS connections,
       [x IN rejected_raw WHERE x IS NOT NULL AND x.subject_id IS NOT NULL] AS rejected_connections
"""


def get_subject_connections(subject_id: str, **kwargs) -> dict:
    """Who is this subject connected to in the graph? (Section 6.3)"""
    p = _require(subject_id=subject_id)
    row = _single(_SUBJECT_CONNECTIONS, subject_id=p["subject_id"]) or {}
    connections = list(row.get("connections") or [])
    rejected = list(row.get("rejected_connections") or [])
    logger.info(
        "copilot_template get_subject_connections: subject_id=%s active=%d rejected=%d",
        p["subject_id"], len(connections), len(rejected),
    )
    return _envelope({
        "subject_id": p["subject_id"],
        "connections": connections,
        "connection_count": len(connections),
        # Surfaced, never merged into `connections` — a rejected connection
        # is review history, not a current fact.
        "rejected_connections": rejected,
        "rejected_count": len(rejected),
    })


# ---------------------------------------------------------------------------
# 2. get_rules_fired — what rules fired on this case and why?
# ---------------------------------------------------------------------------
# Read from the graph itself (the inferred edges' source_rule), not from a
# cached rules_fired block, so the answer reflects the graph as it stands.
_RULES_FIRED = """
MATCH (c:Case {case_id: $case_id})<-[:APPEARS_IN_CASE]-(s:Subject)
OPTIONAL MATCH (s)-[r]-()
    WHERE r.source_rule IS NOT NULL
WITH r.source_rule                       AS rule_id,
     coalesce(r.status, "active")        AS status,
     r.confidence                        AS confidence,
     coalesce(r.corroborated, false)     AS corroborated
WHERE rule_id IS NOT NULL
RETURN rule_id,
       count(*)                                                   AS edge_count,
       count(CASE WHEN status = "active"   THEN 1 END)            AS active_count,
       count(CASE WHEN status = "rejected" THEN 1 END)            AS rejected_count,
       count(CASE WHEN corroborated       THEN 1 END)             AS corroborated_count,
       collect(DISTINCT confidence)                               AS confidences
ORDER BY rule_id ASC
"""


def get_rules_fired(case_id: str, **kwargs) -> dict:
    """What rules fired on this case and why? (Section 6.3)"""
    p = _require(case_id=case_id)
    rows = _rows(_RULES_FIRED, case_id=p["case_id"])
    rules = [{
        "rule_id": r["rule_id"],
        "fired": (r["active_count"] or 0) > 0,
        "active_edge_count": r["active_count"],
        "rejected_edge_count": r["rejected_count"],
        "corroborated_edge_count": r["corroborated_count"],
        "confidences": [c for c in (r["confidences"] or []) if c],
    } for r in rows]
    logger.info(
        "copilot_template get_rules_fired: case_id=%s rules=%d",
        p["case_id"], len(rules),
    )
    return _envelope({
        "case_id": p["case_id"],
        "rules": rules,
        "fired_rule_count": sum(1 for r in rules if r["fired"]),
    })


# ---------------------------------------------------------------------------
# 3. get_risk_signals — why is this case flagged high risk?
# ---------------------------------------------------------------------------
_RISK_SIGNALS = """
MATCH (c:Case {case_id: $case_id})
OPTIONAL MATCH (s:Subject {subject_id: $subject_id})
OPTIONAL MATCH (s)-[pg:HAS_PRIOR_GUILTY_CASE]->(pgc:Case)
    WHERE coalesce(pg.status, "active") = "active"
WITH c, s,
     count(DISTINCT pgc) AS prior_guilty_count,
     [d IN collect(pg.date_closed) WHERE d IS NOT NULL] AS prior_closed_dates
OPTIONAL MATCH (s)-[m:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
    WHERE coalesce(m.status, "active") = "active"
OPTIONAL MATCH (member:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
    WHERE coalesce(mm.status, "active") = "active"
WITH c, s, prior_guilty_count, prior_closed_dates,
     collect(DISTINCT {network_key: n.network_key,
                       network_type: n.network_type,
                       formed_by_rule: n.formed_by_rule}) AS nets_raw,
     count(DISTINCT member) AS network_member_count
RETURN coalesce(c.is_fasttrack, false) AS is_fasttrack,
       c.fraud_amount                  AS fraud_amount,
       coalesce(s.is_cross_case, false) AS is_cross_case_hub,
       prior_guilty_count,
       prior_closed_dates,
       network_member_count,
       [x IN nets_raw WHERE x.network_key IS NOT NULL] AS fraud_networks
"""


def get_risk_signals(case_id: str, subject_id: str, **kwargs) -> dict:
    """Why is this case flagged high risk? (Section 6.3)

    Returns the graph inputs behind the risk tier. It deliberately does NOT
    recompute the score: /risk_assessment owns that arithmetic (Section
    8.4), and a second implementation here could disagree with the number
    the investigator already has on screen.
    """
    p = _require(case_id=case_id, subject_id=subject_id)
    row = _single(_RISK_SIGNALS, case_id=p["case_id"], subject_id=p["subject_id"]) or {}
    networks = list(row.get("fraud_networks") or [])
    prior_guilty_count = int(row.get("prior_guilty_count") or 0)
    result = {
        "case_id": p["case_id"],
        "subject_id": p["subject_id"],
        "fasttrack_eligible": bool(row.get("is_fasttrack", False)),
        "fraud_amount": row.get("fraud_amount"),
        "is_cross_case_hub": bool(row.get("is_cross_case_hub", False)),
        "prior_guilty_case_count": prior_guilty_count,
        "prior_guilty_dates_closed": list(row.get("prior_closed_dates") or []),
        "fraud_networks": networks,
        "network_member_count": int(row.get("network_member_count") or 0),
        # The Rule 8 condition in plain terms, so the answer can explain the
        # escalation rather than restate a boolean.
        "recidivist_in_active_network": prior_guilty_count > 0 and len(networks) > 0,
    }
    logger.info(
        "copilot_template get_risk_signals: case_id=%s subject_id=%s networks=%d prior_guilty=%d",
        p["case_id"], p["subject_id"], len(networks), prior_guilty_count,
    )
    return _envelope(result)


# ---------------------------------------------------------------------------
# 4. get_employer_case_history — has this employer appeared in other cases?
# ---------------------------------------------------------------------------
_EMPLOYER_CASE_HISTORY = """
MATCH (e:Employer {fein: $fein})<-[emp:EMPLOYED_BY]-(s:Subject)-[:APPEARS_IN_CASE]->(c:Case)
WITH e, c, collect(DISTINCT s.subject_id) AS subject_ids
RETURN e.fein            AS fein,
       e.name            AS employer_name,
       c.case_id         AS case_id,
       c.complaint_number AS complaint_no,
       c.status          AS status,
       c.fraud_amount    AS fraud_amount,
       c.opened_date     AS opened_date,
       subject_ids
ORDER BY c.opened_date DESC, case_id ASC
"""


def get_employer_case_history(fein: str, **kwargs) -> dict:
    """Has this employer appeared in other cases? (Section 6.3)"""
    p = _require(fein=fein)
    rows = _rows(_EMPLOYER_CASE_HISTORY, fein=p["fein"])
    cases = [{
        "case_id": r["case_id"],
        "complaint_no": r.get("complaint_no"),
        "status": r.get("status"),
        "fraud_amount": r.get("fraud_amount"),
        "opened_date": r.get("opened_date"),
        "subject_ids": list(r.get("subject_ids") or []),
    } for r in rows]
    logger.info(
        "copilot_template get_employer_case_history: fein=%s cases=%d", p["fein"], len(cases),
    )
    return _envelope({
        "fein": p["fein"],
        "employer_name": rows[0].get("employer_name") if rows else None,
        "cases": cases,
        "case_count": len(cases),
    })


# ---------------------------------------------------------------------------
# 5. get_full_network — who is in the fraud network and what are their cases?
# ---------------------------------------------------------------------------
_FULL_NETWORK = """
MATCH (c:Case {case_id: $case_id})<-[:APPEARS_IN_CASE]-(:Subject)
      -[m:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE coalesce(m.status, "active") = "active"
WITH DISTINCT n
MATCH (member:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
WHERE coalesce(mm.status, "active") = "active"
OPTIONAL MATCH (member)-[:APPEARS_IN_CASE]->(mc:Case)
WITH n, member, mm, collect(DISTINCT mc.case_id) AS member_case_ids
RETURN n.network_key    AS network_key,
       n.network_type   AS network_type,
       n.formed_by_rule AS formed_by_rule,
       collect(DISTINCT {
           subject_id: member.subject_id,
           confidence: mm.confidence,
           case_ids:   member_case_ids
       }) AS members
ORDER BY network_key ASC
"""


def get_full_network(case_id: str, **kwargs) -> dict:
    """Who is in the fraud network and what are their cases? (Section 6.3)"""
    p = _require(case_id=case_id)
    rows = _rows(_FULL_NETWORK, case_id=p["case_id"])
    networks = [{
        "network_key": r["network_key"],
        "network_type": r.get("network_type"),
        "formed_by_rule": r.get("formed_by_rule"),
        "members": list(r.get("members") or []),
        "member_count": len(r.get("members") or []),
    } for r in rows]
    logger.info(
        "copilot_template get_full_network: case_id=%s networks=%d", p["case_id"], len(networks),
    )
    return _envelope({
        "case_id": p["case_id"],
        "networks": networks,
        "network_count": len(networks),
    })


# ---------------------------------------------------------------------------
# 6. get_structural_similar_cases — cases sharing structural graph properties
# ---------------------------------------------------------------------------
def get_structural_similar_cases(case_id: str, limit: int = 25, **kwargs) -> dict:
    """What cases share structural graph properties with this one?
    (Section 6.3)

    Delegates to the AI-14 matcher rather than re-implementing the scoring.
    Two independent similarity implementations would eventually disagree,
    and then the Similar Cases tab and the Copilot would tell the
    investigator different things about the same pair of cases.
    """
    from reasoning_layer.similar_cases import find_structural_matches
    envelope = find_structural_matches(case_id, limit=limit)
    logger.info(
        "copilot_template get_structural_similar_cases: case_id=%s matches=%d",
        case_id, len(envelope["result"].get("matches", [])),
    )
    return envelope


# ---------------------------------------------------------------------------
# 7. get_rejection_history — which inferred facts were reviewed or rejected?
# ---------------------------------------------------------------------------
# Two sources of rejection state: :Rejection nodes written by the review
# flow, and status="rejected" stamped directly on an inferred edge.
_REJECTION_HISTORY = """
MATCH (c:Case {case_id: $case_id})<-[:APPEARS_IN_CASE]-(s:Subject)
OPTIONAL MATCH (s)-[r]-()
    WHERE r.status = "rejected"
WITH c, collect(DISTINCT {
        relationship_type: type(r),
        source_rule:       r.source_rule,
        rejected_by:       r.rejected_by,
        rejected_at:       r.rejected_at,
        reason:            coalesce(r.rejection_reason, r.reason)
     }) AS edge_raw
OPTIONAL MATCH (rej:Rejection)
    WHERE rej.case_id = $case_id
WITH edge_raw, collect(DISTINCT {
        relationship_type: rej.relationship_type,
        status:            rej.status,
        rejected_by:       rej.rejected_by,
        rejected_at:       rej.rejected_at,
        reason:            rej.reason
     }) AS node_raw
RETURN [x IN edge_raw WHERE x.relationship_type IS NOT NULL] AS rejected_relationships,
       [x IN node_raw WHERE x.relationship_type IS NOT NULL] AS rejection_records
"""


def get_rejection_history(case_id: str, **kwargs) -> dict:
    """Which inferred facts have been reviewed or rejected on this case?
    (Section 6.3)

    This is the template that exists so a rejected fact is answerable as
    review history. Section 6.3: a question touching a status="rejected"
    fact must surface the rejection context, never present it as current.
    """
    p = _require(case_id=case_id)
    row = _single(_REJECTION_HISTORY, case_id=p["case_id"]) or {}
    rels = list(row.get("rejected_relationships") or [])
    records = list(row.get("rejection_records") or [])
    logger.info(
        "copilot_template get_rejection_history: case_id=%s rejected_edges=%d records=%d",
        p["case_id"], len(rels), len(records),
    )
    return _envelope({
        "case_id": p["case_id"],
        "rejected_relationships": rels,
        "rejection_records": records,
        "rejected_count": len(rels) + len(records),
        # Read by the prompt layer; states the handling rule as data so the
        # answer cannot quietly restate a rejected inference as live.
        "status_note": (
            "These inferences were reviewed and rejected. They are historical "
            "review context and must not be presented as current findings."
        ),
    })


# ---------------------------------------------------------------------------
# 8. get_case_merge_history — was history inherited from a merged case?
# ---------------------------------------------------------------------------
_CASE_MERGE_HISTORY = """
MATCH (c:Case {case_id: $case_id})
OPTIONAL MATCH (source:Case)-[:MERGED_INTO_CASE]->(c)
WITH c, collect(DISTINCT {
        case_id:          source.case_id,
        complaint_number: source.complaint_number,
        status:           source.status,
        direction:        "merged_into_this_case"
     }) AS incoming_raw
OPTIONAL MATCH (c)-[:MERGED_INTO_CASE]->(target:Case)
WITH c, incoming_raw, collect(DISTINCT {
        case_id:          target.case_id,
        complaint_number: target.complaint_number,
        status:           target.status,
        direction:        "this_case_merged_into"
     }) AS outgoing_raw
OPTIONAL MATCH (s:Subject)-[ap:APPEARS_IN_CASE]->(c)
    WHERE ap.merge_derived = true
WITH incoming_raw, outgoing_raw, collect(DISTINCT {
        subject_id:  s.subject_id,
        source_rule: ap.source_rule
     }) AS derived_raw
RETURN [x IN incoming_raw WHERE x.case_id IS NOT NULL] AS merged_in_cases,
       [x IN outgoing_raw WHERE x.case_id IS NOT NULL] AS merged_out_cases,
       [x IN derived_raw  WHERE x.subject_id IS NOT NULL] AS merge_derived_subjects
"""


def get_case_merge_history(case_id: str, **kwargs) -> dict:
    """Was any of this subject's history inherited from a merged case?
    (Section 6.3)

    merge_derived_subjects is the point of the template: those subjects are
    on this case because Rule 10 propagated them from a merged case, not
    because AppWorks asserted them here. An investigator reading the case
    should know which is which.
    """
    p = _require(case_id=case_id)
    row = _single(_CASE_MERGE_HISTORY, case_id=p["case_id"]) or {}
    merged_in = list(row.get("merged_in_cases") or [])
    merged_out = list(row.get("merged_out_cases") or [])
    derived = list(row.get("merge_derived_subjects") or [])
    logger.info(
        "copilot_template get_case_merge_history: case_id=%s in=%d out=%d derived_subjects=%d",
        p["case_id"], len(merged_in), len(merged_out), len(derived),
    )
    return _envelope({
        "case_id": p["case_id"],
        "merged_in_cases": merged_in,
        "merged_out_cases": merged_out,
        "merge_derived_subjects": derived,
        "has_merge_history": bool(merged_in or merged_out or derived),
    })


# ---------------------------------------------------------------------------
# 9. get_cross_case_hub_summary — which cases is this subject a hub across?
# ---------------------------------------------------------------------------
_CROSS_CASE_HUB = """
MATCH (s:Subject {subject_id: $subject_id})
OPTIONAL MATCH (s)-[ap:APPEARS_IN_CASE]->(c:Case)
WITH s, collect(DISTINCT {
        case_id:       c.case_id,
        complaint_no:  c.complaint_number,
        status:        c.status,
        opened_date:   c.opened_date,
        subject_role:  ap.subject_role,
        is_primary:    coalesce(ap.is_primary, false),
        merge_derived: coalesce(ap.merge_derived, false)
     }) AS cases_raw
RETURN coalesce(s.is_cross_case, false) AS is_cross_case_hub,
       coalesce(s.hub_case_ids, [])     AS hub_case_ids,
       [x IN cases_raw WHERE x.case_id IS NOT NULL] AS cases
"""


def get_cross_case_hub_summary(subject_id: str, **kwargs) -> dict:
    """Which cases is this subject a cross-case hub across? (Section 6.3)"""
    p = _require(subject_id=subject_id)
    row = _single(_CROSS_CASE_HUB, subject_id=p["subject_id"]) or {}
    cases = list(row.get("cases") or [])
    logger.info(
        "copilot_template get_cross_case_hub_summary: subject_id=%s hub=%s cases=%d",
        p["subject_id"], row.get("is_cross_case_hub"), len(cases),
    )
    return _envelope({
        "subject_id": p["subject_id"],
        "is_cross_case_hub": bool(row.get("is_cross_case_hub", False)),
        "hub_case_ids": list(row.get("hub_case_ids") or []),
        "cases": cases,
        "case_count": len(cases),
        "primary_case_count": sum(1 for c in cases if c.get("is_primary")),
    })


# ---------------------------------------------------------------------------
# 10. get_wage_corroboration_detail — what wage evidence corroborates SLAM?
# ---------------------------------------------------------------------------
_WAGE_CORROBORATION = """
MATCH (c:Case {case_id: $case_id})-[:HAS_ALLEGATION]->(al:Allegation)
OPTIONAL MATCH (al)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s:Subject {subject_id: $subject_id})
OPTIONAL MATCH (s)-[w:HAS_WAGE_RECORD_WITH]->(e:Employer)
WITH c, al, s,
     collect(DISTINCT {
        employer_name: e.name,
        fein:          e.fein,
        quarter:       w.quarter,
        year:          w.year,
        amount:        w.amount
     }) AS wages_raw
RETURN al.allegation_type                  AS allegation_type,
       coalesce(al.wage_corroborated, false) AS wage_corroborated,
       al.wage_corroboration_confidence    AS corroboration_confidence,
       al.wage_corroboration_verified      AS date_overlap_verified,
       al.wage_corroboration_rule          AS corroboration_rule,
       c.fraud_start_date                  AS fraud_start_date,
       c.fraud_end_date                    AS fraud_end_date,
       [x IN wages_raw WHERE x.fein IS NOT NULL OR x.employer_name IS NOT NULL] AS wage_records
ORDER BY allegation_type ASC
"""


def get_wage_corroboration_detail(case_id: str, subject_id: str, **kwargs) -> dict:
    """What wage evidence corroborates this SLAM allegation? (Section 6.3)"""
    p = _require(case_id=case_id, subject_id=subject_id)
    rows = _rows(_WAGE_CORROBORATION, case_id=p["case_id"], subject_id=p["subject_id"])
    allegations = [{
        "allegation_type": r.get("allegation_type"),
        "wage_corroborated": bool(r.get("wage_corroborated", False)),
        "corroboration_confidence": r.get("corroboration_confidence"),
        # Rule 12 distinguishes a verified date overlap (High) from wages
        # present but dates absent (Medium). Carried through so the answer
        # can state which of the two it is.
        "date_overlap_verified": r.get("date_overlap_verified"),
        "corroboration_rule": r.get("corroboration_rule"),
        "wage_records": list(r.get("wage_records") or []),
    } for r in rows]
    logger.info(
        "copilot_template get_wage_corroboration_detail: case_id=%s subject_id=%s allegations=%d",
        p["case_id"], p["subject_id"], len(allegations),
    )
    return _envelope({
        "case_id": p["case_id"],
        "subject_id": p["subject_id"],
        "fraud_start_date": rows[0].get("fraud_start_date") if rows else None,
        "fraud_end_date": rows[0].get("fraud_end_date") if rows else None,
        "allegations": allegations,
        "corroborated_count": sum(1 for a in allegations if a["wage_corroborated"]),
    })


# ---------------------------------------------------------------------------
# 11. get_connection_path — how exactly is A connected to B?
# ---------------------------------------------------------------------------
# shortestPath with a bounded 4-hop pattern (Section 9.3). The bound is not
# cosmetic: unbounded variable-length matching over a dense fraud graph can
# run away, and a path longer than four hops is not an explanation an
# investigator can act on anyway.
_CONNECTION_PATH = """
MATCH (a:Subject {subject_id: $subject_id_a}), (b:Subject {subject_id: $subject_id_b})
OPTIONAL MATCH path = shortestPath((a)-[*..4]-(b))
RETURN [rel IN relationships(path) | type(rel)]                     AS connection_path,
       [n IN nodes(path) | coalesce(n.subject_id, n.network_key, n.case_id, n.fein)] AS path_nodes,
       length(path)                                                 AS hop_count
"""


def get_connection_path(subject_id_a: str, subject_id_b: str, **kwargs) -> dict:
    """How exactly is A connected to B, even across subjects not in the
    same detected network? (Sections 6.3, 9.3)"""
    p = _require(subject_id_a=subject_id_a, subject_id_b=subject_id_b)
    row = _single(
        _CONNECTION_PATH,
        subject_id_a=p["subject_id_a"], subject_id_b=p["subject_id_b"],
    ) or {}
    path = list(row.get("connection_path") or [])
    logger.info(
        "copilot_template get_connection_path: %s -> %s hops=%s",
        p["subject_id_a"], p["subject_id_b"], row.get("hop_count"),
    )
    return _envelope({
        "subject_id_a": p["subject_id_a"],
        "subject_id_b": p["subject_id_b"],
        "connection_path": path,
        "path_nodes": list(row.get("path_nodes") or []),
        "hop_count": row.get("hop_count"),
        # Explicit, so an answer says "no connection within four hops"
        # rather than treating an empty list as "not connected at all".
        "connected": bool(path),
        "search_depth_limit": 4,
    })


# ---------------------------------------------------------------------------
# 12. get_network_financial_exposure — total exposure across a network
# ---------------------------------------------------------------------------
_NETWORK_EXPOSURE = """
MATCH (c:Case {case_id: $case_id})<-[:APPEARS_IN_CASE]-(:Subject)
      -[m:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE coalesce(m.status, "active") = "active"
WITH DISTINCT n
MATCH (member:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
WHERE coalesce(mm.status, "active") = "active"
MATCH (member)-[:APPEARS_IN_CASE]->(nc:Case)
WITH n, collect(DISTINCT {case_id: nc.case_id, fraud_amount: nc.fraud_amount}) AS case_rows,
     count(DISTINCT member) AS member_count
RETURN n.network_key  AS network_key,
       n.network_type AS network_type,
       member_count,
       size(case_rows) AS case_count,
       reduce(total = 0.0, x IN case_rows | total + coalesce(x.fraud_amount, 0.0)) AS total_exposure,
       case_rows
ORDER BY total_exposure DESC, network_key ASC
"""


def get_network_financial_exposure(case_id: str, **kwargs) -> dict:
    """Total amount exposure across a detected network (Section 6.3)."""
    p = _require(case_id=case_id)
    rows = _rows(_NETWORK_EXPOSURE, case_id=p["case_id"])
    networks = []
    for r in rows:
        cases = list(r.get("case_rows") or [])
        networks.append({
            "network_key": r["network_key"],
            "network_type": r.get("network_type"),
            "member_count": r.get("member_count"),
            "case_count": r.get("case_count"),
            "total_exposure": round(float(r.get("total_exposure") or 0.0), 2),
            "cases": cases,
            # Stated rather than hidden: a case with a null fraud_amount
            # contributes 0, so the total is a floor, not a certainty.
            "cases_missing_amount": sum(1 for c in cases if c.get("fraud_amount") is None),
        })
    total = round(sum(n["total_exposure"] for n in networks), 2)
    logger.info(
        "copilot_template get_network_financial_exposure: case_id=%s networks=%d total=%.2f",
        p["case_id"], len(networks), total,
    )
    return _envelope({
        "case_id": p["case_id"],
        "networks": networks,
        "network_count": len(networks),
        "total_exposure_all_networks": total,
    })