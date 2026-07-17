"""
Owns: the four Neo4j-sourced risk signals that are layered on top of the
AppWorks base risk score (AI-15 / Section 8.4).

Section 8.4 is explicit that the AppWorks base scoring logic is UNCHANGED;
these signals are added as additional inputs AFTER the base score is
computed. This module therefore takes the finished AppWorks result and
returns an augmented copy — it never recomputes the base score.

The four signals and their effects (Section 8.4):
  1. Rule 8 — recidivist in active network: HAS_PRIOR_GUILTY_CASE AND
     MEMBER_OF_FRAUD_NETWORK on the subject. Boolean additive signal that
     raises the score.
  2. Network size multiplier: largest FraudNetwork the subject belongs to.
     4-6 members => 1.2x, 7+ => 1.5x, applied to the GRAPH score component
     only (never to the AppWorks base).
  3. Prior guilt recency: most recent date_closed on HAS_PRIOR_GUILTY_CASE.
     <2yr => full weight, 2-5yr => 0.7x, >5yr => 0.4x — weights the
     prior-guilt part of the graph component.
  4. Rule 13 FastTrack override: is_fasttrack on the :Case. Forces
     risk_tier to a minimum of HIGH regardless of the computed score.
  Plus the compound rule penalty: 3+ of Rules 7, 8, 9, 11 firing together
  in rules_fired => compound_escalation flag.

Provenance (Section 8.4 requirement): the source of each risk component is
independently attributable — delivered as TWO provenance blocks in the
trail: the AppWorks base scorer's block and this module's block.

GOVERNANCE: this is a Neo4j read, not an AppWorks call, so per the rule
that manifest.yaml holds a tool only if it is LLM-called AND makes an
AppWorks call, it is invoked DIRECTLY by the /risk_assessment route (the
same pattern as check_network_match / enrich_graph_context /
find_structural_matches), never as an LLM tool.

Does NOT own: the AppWorks base scoring (appworks/risk_scoring.py), the
pipeline, or any write.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

# --- tunable graph-signal weights (0.0–1.0 scale, matching the base score) ---
# Section 8.4 fixes the MULTIPLIERS (network size, recency) and the
# STRUCTURE, but not the base point value of each signal. These are the
# graph component's base weights, kept as named constants so they are
# auditable and tunable rather than magic numbers inside the arithmetic.
_RULE_8_SIGNAL_WEIGHT = 0.15      # recidivist in an active network is a strong signal
_PRIOR_GUILT_BASE_WEIGHT = 0.10   # weighted down by recency below

# Section 8.4 tier ladder — identical thresholds to appworks/risk_scoring.py,
# re-declared here only to re-tier the FINAL (base + graph) score. The base
# module's own tiering is left untouched.
_TIER_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def _tier_for_score(score: float) -> str:
    if score >= 0.75:
        return "CRITICAL"
    if score >= 0.50:
        return "HIGH"
    if score >= 0.25:
        return "MEDIUM"
    return "LOW"


def _at_least(tier: str, floor: str) -> str:
    """Return whichever of `tier` / `floor` is more severe — used by the
    FastTrack override to raise the tier to a minimum of HIGH."""
    ti = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else 0
    fi = _TIER_ORDER.index(floor) if floor in _TIER_ORDER else 0
    return _TIER_ORDER[max(ti, fi)]


def _network_size_multiplier(network_size: int) -> float:
    """4-6 members => 1.2x, 7+ => 1.5x, otherwise 1.0x (Section 8.4)."""
    if network_size >= 7:
        return 1.5
    if network_size >= 4:
        return 1.2
    return 1.0


def _recency_weight(years: Optional[float]) -> float:
    """<2yr => 1.0, 2-5yr => 0.7, >5yr => 0.4 (Section 8.4). No prior guilt
    (years is None) contributes nothing."""
    if years is None:
        return 0.0
    if years < 2:
        return 1.0
    if years <= 5:
        return 0.7
    return 0.4


# --- Neo4j reads. Plain, scalar-returning, single statements each. ---
_FASTTRACK_QUERY = """
MATCH (c:Case {case_id: $case_id})
RETURN coalesce(c.is_fasttrack, false) AS is_fasttrack
"""

# Rule 8 inputs + prior-guilt recency inputs.
_RULE8_RECENCY_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})
OPTIONAL MATCH (s)-[pg:HAS_PRIOR_GUILTY_CASE]->(:Case)
    WHERE pg.status = "active"
WITH s,
     count(DISTINCT pg) AS prior_guilty_count,
     [d IN collect(pg.date_closed) WHERE d IS NOT NULL] AS closed_dates
OPTIONAL MATCH (s)-[mem:MEMBER_OF_FRAUD_NETWORK]->(:FraudNetwork)
    WHERE mem.status = "active"
RETURN prior_guilty_count,
       closed_dates,
       count(DISTINCT mem) AS network_membership_count
"""

# Largest active FraudNetwork the subject belongs to (member count includes
# the subject).
_NETWORK_SIZE_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})-[m:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE m.status = "active"
OPTIONAL MATCH (member:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
    WHERE mm.status = "active"
WITH n, count(DISTINCT member) AS members
RETURN coalesce(max(members), 0) AS max_network_size
"""

_COMPOUND_RULE_PREFIXES = ("Rule_07", "Rule_08", "Rule_09", "Rule_11")


def _years_since(date_str: str) -> Optional[float]:
    try:
        d = datetime.fromisoformat(str(date_str)[:10])
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc).replace(tzinfo=None) - d).days / 365.25


def _compound_escalation(rules_fired: List[Dict[str, Any]]) -> bool:
    """3+ of Rules 7, 8, 9, 11 firing simultaneously (Section 8.4)."""
    fired = {
        r.get("rule_id", "")[:7]
        for r in (rules_fired or [])
        if r.get("fired") and r.get("rule_id", "").startswith(_COMPOUND_RULE_PREFIXES)
    }
    return len(fired) >= 3


def apply_graph_risk_signals(
    case_id: str,
    subject_id: str,
    base_result: Dict[str, Any],
    rules_fired: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """
    Layer the four Neo4j graph signals on top of an AppWorks base risk
    result (Section 8.4). Does not recompute the base score.

    Args:
        case_id, subject_id: the case and its primary subject.
        base_result: the AppWorks risk result dict, containing at least
            risk_score (0.0–1.0) and risk_tier.
        rules_fired: the pipeline's rules_fired block (from CS-4 / Context
            Enrichment), used only for the compound-escalation flag.

    Returns (inside the standard {result, provenance} envelope) a COPY of
    base_result with:
        * base_risk_score / base_risk_tier  — the untouched AppWorks values
        * risk_score / risk_tier            — the final values after signals
        * neo4j_signals: {
              rule_8_signal, network_size, network_size_multiplier,
              prior_guilt_recency_years, fasttrack_override,
              compound_escalation, graph_score_component
          }
    and a provenance block for the Neo4j graph signals. Together with the
    AppWorks base scorer's own provenance block (already in the trail from
    calculate_risk_metrics), this gives the two independently-attributable
    computed_by entries Section 8.4 requires.

    Raises GraphUnavailableError / Neo4jError on a graph problem — the
    /risk_assessment route catches these and returns the base result
    unaugmented (non-blocking), rather than fabricating signals.
    """
    if not subject_id or not str(subject_id).strip():
        raise ValueError("apply_graph_risk_signals requires a non-empty subject_id")
    case_id = str(case_id).strip()
    subject_id = str(subject_id).strip()

    with get_session() as session:
        ft_rec = session.run(_FASTTRACK_QUERY, case_id=case_id).single()
        r8_rec = session.run(_RULE8_RECENCY_QUERY, subject_id=subject_id).single()
        ns_rec = session.run(_NETWORK_SIZE_QUERY, subject_id=subject_id).single()

    is_fasttrack = bool(ft_rec["is_fasttrack"]) if ft_rec else False
    prior_guilty_count = int(r8_rec["prior_guilty_count"]) if r8_rec else 0
    network_membership_count = int(r8_rec["network_membership_count"]) if r8_rec else 0
    closed_dates = list(r8_rec["closed_dates"]) if r8_rec else []
    network_size = int(ns_rec["max_network_size"]) if ns_rec else 0

    # --- signal 1: Rule 8 — recidivist in an active network ---
    rule_8_signal = prior_guilty_count > 0 and network_membership_count > 0

    # --- signal 3: prior-guilt recency (most recent prior guilty case) ---
    recency_candidates = [y for y in (_years_since(d) for d in closed_dates) if y is not None]
    prior_guilt_recency_years = round(min(recency_candidates), 2) if recency_candidates else None
    recency_weight = _recency_weight(prior_guilt_recency_years) if prior_guilty_count > 0 else 0.0

    # --- signal 2: network size multiplier (graph component only) ---
    network_size_multiplier = _network_size_multiplier(network_size)

    # --- compound escalation flag ---
    compound_escalation = _compound_escalation(rules_fired)

    # --- assemble the graph score component ---
    rule_8_component = _RULE_8_SIGNAL_WEIGHT if rule_8_signal else 0.0
    prior_guilt_component = (_PRIOR_GUILT_BASE_WEIGHT * recency_weight) if prior_guilty_count > 0 else 0.0
    graph_component = round((rule_8_component + prior_guilt_component) * network_size_multiplier, 4)

    base_score = float(base_result.get("risk_score", 0.0) or 0.0)
    base_tier = base_result.get("risk_tier", _tier_for_score(base_score))

    final_score = round(min(base_score + graph_component, 1.0), 4)
    final_tier = _tier_for_score(final_score)

    # --- signal 4: FastTrack override — floor the tier at HIGH ---
    if is_fasttrack:
        final_tier = _at_least(final_tier, "HIGH")

    # Build the augmented result (copy so the base result is never mutated).
    augmented = copy.deepcopy(base_result)
    augmented["base_risk_score"] = round(base_score, 4)
    augmented["base_risk_tier"] = base_tier
    augmented["risk_score"] = final_score
    augmented["risk_tier"] = final_tier
    augmented["neo4j_signals"] = {
        "rule_8_signal": rule_8_signal,
        "network_size": network_size,
        "network_size_multiplier": network_size_multiplier,
        "prior_guilt_recency_years": prior_guilt_recency_years,
        "fasttrack_override": is_fasttrack,
        "compound_escalation": compound_escalation,
        "graph_score_component": graph_component,
    }

    logger.info(
        "apply_graph_risk_signals: case_id=%s subject_id=%s base=%.4f/%s -> final=%.4f/%s "
        "rule8=%s net_size=%d mult=%.1f recency_yrs=%s fasttrack=%s compound=%s",
        case_id, subject_id, base_score, base_tier, final_score, final_tier,
        rule_8_signal, network_size, network_size_multiplier,
        prior_guilt_recency_years, is_fasttrack, compound_escalation,
    )

    return {
        "result": augmented,
        "provenance": {
            "sources": ["Neo4j graph query"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            # Section 8.4 requires the source of each risk component to be
            # independently attributable. This is delivered as TWO provenance
            # BLOCKS in the trail: the AppWorks base scorer's block (computed_by
            # "BSI deterministic rules engine", added by calculate_risk_metrics)
            # and THIS block for the Neo4j graph signals. computed_by is kept a
            # single string per block so merge_provenance can hash it for
            # de-duplication; the two-entry requirement is met by the two blocks,
            # not by a list inside one.
            "computed_by": "reasoning_layer.risk_signals (Neo4j graph signals)",
        },
    }