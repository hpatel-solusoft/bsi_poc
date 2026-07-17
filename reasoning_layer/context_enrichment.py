"""
Owns: the Phase 2 Context Enrichment graph processing (AI-13 / Section
8.2, 9.1) — the deterministic step that makes Context Enrichment the
gateway to the Reasoning Pipeline.

Section 9.1 places this AFTER fetch_subject_history returns, as Context
Enrichment's "own processing", NOT as an LLM-decided tool call. It is
invoked directly by the /intake route (same non-blocking, direct-call
pattern the route already uses for check_network_match), never selected
by the model, and it is not registered in manifest.yaml.

For one (case, subject) it performs, in order:

  Step 2  trigger reasoning_layer.pipeline.run_pipeline and BLOCK until it
          returns (Section 5.1). Step 1 — the AppWorks REST subject
          history — is fetch_subject_history and stays unchanged in the
          AppWorks layer; it is not this file's concern.
  Step 3  read graph_context from Neo4j: fraud-network detail, prior
          guilty cases, shared connections, and the co-subject hub flag.
  Step 4  compute graph-derived signals: temporal acceleration, role
          distribution, and corroboration ratio.

and returns { graph_context, graph_signals, rules_fired } wrapped in the
standard {result, provenance} envelope — the same envelope a
dispatcher-routed tool would return, so merge_direct_result places it in
CS-4 sections indistinguishably from an LLM-selected result.

LAYERING: Layer 4 only. It drives the pipeline (reasoning_layer) and
reads Neo4j (reasoning_layer). It makes ZERO AppWorks calls — an AppWorks
outage must not stop graph enrichment, and vice-versa.

Does NOT own: the pipeline itself (pipeline.py), rule content, the
read-only intake network check (graph_queries.check_network_match), or any
AppWorks call.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from reasoning_layer import pipeline
from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

# Days that define "recent" for the temporal-acceleration signal.
_RECENT_WINDOW_DAYS = 365
# avg interval / most-recent interval. >1.5 => cases arriving markedly
# faster than the historical average; <0.67 => markedly slower. Named
# constants so the interpretation is auditable, not a magic comparison.
_ACCEL_FAST = 1.5
_ACCEL_SLOW = 0.67


# --- Step 3: graph_context (one read-only statement) ------------------------
# Rows are built as maps of SCALAR properties (never node/relationship
# objects with chained access like n.x.y), which behaves consistently across
# Neo4j 5 builds. Each section is collected and null-filtered independently.
_GRAPH_CONTEXT_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})
OPTIONAL MATCH (s)-[m:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
    WHERE m.status = "active"
WITH s, collect(DISTINCT {
        network_type:   n.network_type,
        network_key:    n.network_key,
        confidence:     m.confidence,
        formed_by_rule: n.formed_by_rule
     }) AS nets_raw
OPTIONAL MATCH (s)-[pg:HAS_PRIOR_GUILTY_CASE]->(pgc:Case)
    WHERE pg.status = "active"
WITH s, nets_raw, collect(DISTINCT {
        case_id:     pgc.case_id,
        outcome:     pg.outcome,
        date_closed: pg.date_closed,
        confidence:  pg.confidence
     }) AS guilty_raw
OPTIONAL MATCH (s)-[sc:SHARES_EMPLOYER_WITH|SHARES_ADDRESS_WITH|SHARES_ALIAS_PATTERN_WITH]-(conn:Subject)
    WHERE sc.status = "active"
WITH s, nets_raw, guilty_raw, collect(DISTINCT {
        subject_id:      conn.subject_id,
        connection_type: type(sc),
        confidence:      sc.confidence,
        corroborated:    coalesce(sc.corroborated, false)
     }) AS conns_raw
RETURN
    coalesce(s.is_cross_case, false) AS is_cross_case_hub,
    coalesce(s.hub_case_ids, [])     AS hub_case_ids,
    [x IN nets_raw   WHERE x.network_type IS NOT NULL] AS fraud_networks,
    [x IN guilty_raw WHERE x.case_id IS NOT NULL]      AS prior_guilty_cases,
    [x IN conns_raw  WHERE x.subject_id IS NOT NULL]   AS shared_connections
"""

# --- Step 4 inputs ----------------------------------------------------------
_APPEARANCES_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})-[ap:APPEARS_IN_CASE]->(c:Case)
RETURN collect(DISTINCT {
    case_id:     c.case_id,
    opened_date: c.opened_date,
    role:        ap.subject_role,
    is_primary:  coalesce(ap.is_primary, false)
}) AS appearances
"""

_CORROBORATION_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})
      -[r:SHARES_EMPLOYER_WITH|SHARES_ADDRESS_WITH|SHARES_ALIAS_PATTERN_WITH|MEMBER_OF_FRAUD_NETWORK|HAS_PRIOR_GUILTY_CASE]-()
WHERE r.status = "active"
RETURN count(DISTINCT r) AS total_inferred,
       count(DISTINCT CASE WHEN r.corroborated = true THEN r END) AS corroborated_count
"""


def _parse_date(value: Any) -> Optional[datetime]:
    """opened_date is an ISO date string (or null). Degrade to None on
    anything unparseable rather than raising — a signal from partial dates
    is still useful; a crashed enrichment is not."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _temporal_acceleration(appearances: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Is this subject's case involvement speeding up? Compares the most
    recent gap between case openings against the historical average gap.
    Degrades explicitly with fewer than two dated cases."""
    dates = sorted(d for d in (_parse_date(a.get("opened_date")) for a in appearances) if d)
    total = len(appearances)

    if len(dates) < 2:
        return {
            "interpretation": "insufficient_data",
            "case_count": total,
            "dated_case_count": len(dates),
            "acceleration_ratio": None,
        }

    intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    avg_interval = statistics.mean(intervals)
    recent_interval = intervals[-1]
    ratio = (avg_interval / recent_interval) if recent_interval > 0 else None
    cases_recent = sum(1 for d in dates if (dates[-1] - d).days <= _RECENT_WINDOW_DAYS)

    if ratio is None:
        interpretation = "insufficient_data"
    elif ratio >= _ACCEL_FAST:
        interpretation = "accelerating"
    elif ratio <= _ACCEL_SLOW:
        interpretation = "decelerating"
    else:
        interpretation = "steady"

    return {
        "interpretation": interpretation,
        "case_count": total,
        "dated_case_count": len(dates),
        "cases_in_last_12_months": cases_recent,
        "average_interval_days": round(avg_interval, 1),
        "most_recent_interval_days": recent_interval,
        "acceleration_ratio": round(ratio, 3) if ratio is not None else None,
    }


def _role_distribution(appearances: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How this subject appears across their case history — primary vs
    co-subject, and the spread of investigative roles."""
    by_role = Counter((a.get("role") or "Unknown") for a in appearances)
    primary = sum(1 for a in appearances if a.get("is_primary"))
    total = len(appearances)
    return {
        "total_appearances": total,
        "primary_count": primary,
        "co_subject_count": total - primary,
        "by_role": dict(by_role),
    }


def _corroboration_ratio(total_inferred: int, corroborated_count: int) -> Dict[str, Any]:
    """Of the graph's inferred relationships for this subject, what
    fraction did the narrative independently confirm (Rule 14)?"""
    ratio = (corroborated_count / total_inferred) if total_inferred else None
    return {
        "inferred_relationship_count": total_inferred,
        "corroborated_count": corroborated_count,
        "ratio": round(ratio, 3) if ratio is not None else None,
    }


def _read_graph_context(session, subject_id: str) -> Dict[str, Any]:
    record = session.run(_GRAPH_CONTEXT_QUERY, subject_id=subject_id).single()
    if record is None:
        return {
            "is_cross_case_hub": False,
            "hub_case_ids": [],
            "fraud_networks": [],
            "prior_guilty_cases": [],
            "shared_connections": [],
        }
    return {
        "is_cross_case_hub": bool(record["is_cross_case_hub"]),
        "hub_case_ids": list(record["hub_case_ids"] or []),
        "fraud_networks": list(record["fraud_networks"] or []),
        "prior_guilty_cases": list(record["prior_guilty_cases"] or []),
        "shared_connections": list(record["shared_connections"] or []),
    }


def _compute_signals(session, subject_id: str) -> Dict[str, Any]:
    appearances_rec = session.run(_APPEARANCES_QUERY, subject_id=subject_id).single()
    appearances = list(appearances_rec["appearances"]) if appearances_rec else []

    corr_rec = session.run(_CORROBORATION_QUERY, subject_id=subject_id).single()
    total_inferred = int(corr_rec["total_inferred"]) if corr_rec else 0
    corroborated_count = int(corr_rec["corroborated_count"]) if corr_rec else 0

    return {
        "temporal_acceleration": _temporal_acceleration(appearances),
        "role_distribution": _role_distribution(appearances),
        "corroboration_ratio": _corroboration_ratio(total_inferred, corroborated_count),
    }


def enrich_graph_context(case_id: str, subject_id: str, force: bool = False, reason: str = "api_reload_ai_summary") -> dict:
    """
    Context Enrichment processing (AI-13 / Section 9.1). For the given
    (case, subject):

      Step 2  run the reasoning pipeline, blocking until it completes.
      Step 3  read graph_context from Neo4j.
      Step 4  compute graph-derived signals.

    Returns (inside the standard {result, provenance} envelope):
        { "case_id", "subject_id",
          "graph_context": {is_cross_case_hub, hub_case_ids, fraud_networks,
                            prior_guilty_cases, shared_connections},
          "graph_signals": {temporal_acceleration, role_distribution,
                            corroboration_ratio},
          "rules_fired":   [ ...the entries the pipeline reported... ] }

    Step 2 defaults to force=False: enrichment is a READ of the
    investigator's world, so Principle 10 applies — an already-completed
    (case, subject) is not re-inferred; its existing rules_fired is
    returned. The ETL path always forces a re-run; the /intake and
    /copilot routes additionally pass force=True through here when the
    caller explicitly asked for reload_ai_summary=True (Section 9.5's
    "reload banner" path, reached from an API caller instead of ETL) —
    that is the only way a route can make this re-infer rather than
    return the cached rules_fired.

    Raises:
        ValueError: on a missing case_id or subject_id.
        GraphUnavailableError / Neo4jError / ValueError from the pipeline:
            propagated. The /intake route catches these and degrades to an
            empty, clearly-unavailable graph_context (non-blocking, Section
            8.1) — a fabricated empty context here would misinform every
            downstream agent, so this function does not paper over it.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("enrich_graph_context requires a non-empty case_id")
    if not subject_id or not str(subject_id).strip():
        raise ValueError("enrich_graph_context requires a non-empty subject_id")
    case_id, subject_id = str(case_id).strip(), str(subject_id).strip()

    # --- Step 2: trigger the pipeline, block until complete ---
    pipeline_envelope = pipeline.run_pipeline(case_id, subject_id, force=force, reason=reason)
    rules_fired = pipeline_envelope["result"].get("rules_fired", [])

    # --- Steps 3 & 4: read graph_context and compute signals ---
    with get_session() as session:
        graph_context = _read_graph_context(session, subject_id)
        graph_signals = _compute_signals(session, subject_id)

    fired_count = sum(1 for r in rules_fired if r.get("fired"))
    logger.info(
        "enrich_graph_context: case_id=%s subject_id=%s networks=%d prior_guilty=%d "
        "shared=%d hub=%s rules_fired=%d/%d",
        case_id, subject_id,
        len(graph_context["fraud_networks"]),
        len(graph_context["prior_guilty_cases"]),
        len(graph_context["shared_connections"]),
        graph_context["is_cross_case_hub"],
        fired_count, len(rules_fired),
    )

    return {
        "result": {
            "case_id": case_id,
            "subject_id": subject_id,
            "graph_context": graph_context,
            "graph_signals": graph_signals,
            "rules_fired": rules_fired,
        },
        "provenance": {
            "sources": ["reasoning pipeline", "Neo4j graph query"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "reasoning_layer.context_enrichment.enrich_graph_context",
        },
    }