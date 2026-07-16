"""
Owns: read-only, agent-facing Neo4j lookups that return the standard
{result, provenance} envelope — graph reads an agent needs WITHOUT
running the reasoning pipeline.

Currently exposes check_network_match (AI-12 / Section 8.1): the
proactive "is this subject already in a known fraud network?" check the
Complaint Intake agent runs the moment a complaint is entered.

Why this is a separate file from pipeline.py: the pipeline WRITES
(Principle 12 — rules are write operations) and runs a six-step,
side-effecting, once-per-subject sequence. The lookups here only READ.
AI-12 is explicit that this check is "decoupled from the Reasoning
Pipeline itself" — it must never trigger inference. Keeping it in its own
module, with no import of pipeline.py, makes that decoupling structural
rather than a matter of discipline: there is no code path from here that
could start a pipeline run.

Why it is not in the AppWorks layer: it queries Neo4j (Bolt), not
AppWorks (REST). Unlike pipeline.run_pipeline, this module's function
used to be registered in manifest.yaml as an LLM-selectable tool; it has
since been converted to a direct, unconditional Python call made by
api/server.py's /intake route right after complaint intake resolves a
subject_primary_id — the same "invoked directly, never LLM-callable"
pattern Section 9.1 describes for run_pipeline. It is not registered in
manifest.yaml.

Does NOT own: the pipeline (pipeline.py), rule content (rules/*.cypher),
the Copilot's parameterised template catalog (AI-17's own concern, gets
its own home when built), or any write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)


# A SINGLE read-only Cypher statement (AI-12: "single read-only Cypher
# call"). It returns exactly one row carrying two things:
#   - networks: a list of the subject's ACTIVE fraud-network memberships
#   - rejected_membership_count: how many memberships an investigator
#     rejected (surfaced, never presented as a live match — Principle 14)
#
# Design notes that make this production-safe rather than merely correct:
#   * Result rows are built from SCALAR properties packed into maps
#     ({network_type: n.network_type, ...}), never from node/relationship
#     objects with chained property access (n.x.y). The latter parses in
#     theory but behaves inconsistently across Neo4j 5 builds; scalar maps
#     do not.
#   * No CALL subquery, so no dependency on the 5.23+ scoped-CALL syntax
#     and no deprecation warning on older 5.x.
#   * One aggregation over ALL memberships (active + rejected) yields both
#     outputs in one pass — the active ones collected into `networks`, the
#     rejected ones summed — so this stays a single statement.
#   * other_member_count is computed per active network via a scoped
#     OPTIONAL MATCH; a rejected membership contributes 0 and is not
#     collected into `networks`.
#   * A subject absent from the graph yields zero rows (MATCH (s) fails),
#     which the caller reads as "nothing known" — the honest answer, not
#     a fabricated "not in any network".
_NETWORK_MATCH_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})
OPTIONAL MATCH (s)-[m:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
OPTIONAL MATCH (other:Subject)-[om:MEMBER_OF_FRAUD_NETWORK {status: "active"}]->(n)
    WHERE m.status = "active" AND other.subject_id <> s.subject_id
WITH s, n, m, count(DISTINCT other) AS other_member_count
WITH
    collect(CASE WHEN m.status = "active" THEN {
        network_type:       n.network_type,
        network_key:        n.network_key,
        formed_by_rule:     n.formed_by_rule,
        confidence:         m.confidence,
        source_rule:        m.source_rule,
        other_member_count: other_member_count
    } END) AS active_raw,
    sum(CASE WHEN m.status = "rejected" THEN 1 ELSE 0 END) AS rejected_membership_count
RETURN
    [x IN active_raw WHERE x IS NOT NULL] AS networks,
    rejected_membership_count
"""


def _envelope(result: Dict[str, Any]) -> dict:
    """The {result, provenance} envelope pattern Principle 8 uses everywhere
    in this codebase — identical in shape to appworks_services.py's and to
    pipeline.run_pipeline's, whether the caller reaches this function
    through the dispatcher or, as with check_network_match, via a direct
    Python call. Keeping the shape identical means the /intake route can
    merge this result into `sections` the same way it merges a
    dispatcher-routed tool result."""
    return {
        "result": result,
        "provenance": {
            "sources": ["Neo4j graph query"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "reasoning_layer.graph_queries.check_network_match",
        },
    }


def check_network_match(subject_id: str) -> dict:
    """
    Proactive, read-only check (AI-12 / Section 8.1): is this subject
    already a member of one or more known fraud networks, at the moment a
    complaint is entered?

    Runs ZERO writes and does NOT trigger the reasoning pipeline — the
    decoupling AI-12 requires. It reads only what the pipeline has
    already written on prior runs.

    Args:
        subject_id: the subject to check. Required and non-empty.

    Returns (inside the standard {result, provenance} envelope):
        {
          "subject_id":     ...,
          "in_network":     bool,       # true iff any ACTIVE membership exists
          "network_count":  int,        # number of active networks
          "networks": [                  # one entry per active network
            {network_type, network_key, formed_by_rule,
             confidence, source_rule, other_member_count}
          ],
          "rejected_membership_count": int,  # memberships an investigator
                                             # has rejected — surfaced, never
                                             # presented as a live match
        }

    A subject not present in the graph (their case has not been loaded, or
    they are genuinely new) is not an error: in_network is false and
    networks is empty — the honest answer to "is this a known network
    member?" when nothing is known about them.

    Raises:
        ValueError: if subject_id is missing or blank — a caller bug worth
            surfacing, not a silent empty result.
        GraphUnavailableError / Neo4jError: propagated unchanged. This
            lookup has no fallback data source, and returning
            in_network=false when Neo4j was simply unreachable would be a
            fabricated negative. The route/agent layer decides how a graph
            outage degrades for display (AppWorks tab shows its 'couldn't
            load' state); this function does not paper over it.
    """
    if not subject_id or not str(subject_id).strip():
        raise ValueError("check_network_match requires a non-empty subject_id")
    subject_id = str(subject_id).strip()

    with get_session() as session:
        record = session.run(_NETWORK_MATCH_QUERY, subject_id=subject_id).single()

    # record is None only when the subject node does not exist at all.
    raw_networks: List[Dict[str, Any]] = list(record["networks"]) if record else []
    rejected_count: int = int(record["rejected_membership_count"]) if record else 0

    networks: List[Dict[str, Any]] = [
        {
            "network_type": row.get("network_type"),
            "network_key": row.get("network_key"),
            "formed_by_rule": row.get("formed_by_rule"),
            "confidence": row.get("confidence"),
            "source_rule": row.get("source_rule"),
            "other_member_count": int(row.get("other_member_count") or 0),
        }
        for row in raw_networks
    ]

    result = {
        "subject_id": subject_id,
        "in_network": bool(networks),
        "network_count": len(networks),
        "networks": networks,
        "rejected_membership_count": rejected_count,
    }
    logger.info(
        "check_network_match: subject_id=%s in_network=%s networks=%d rejected=%d",
        subject_id, result["in_network"], len(networks), rejected_count,
    )
    return _envelope(result)