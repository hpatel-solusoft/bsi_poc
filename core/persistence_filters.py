"""
Owns: the single choke point deciding what is allowed to persist to
PostgreSQL (case_ai_summary_store) versus what must always be re-read
live from Neo4j on every request.

BUG THIS FIXES: previously, rules fired, risk-score graph add-ons, and
network-match data were snapshotted into Postgres (and the CS-4 warm
CASE_STORE rehydrated from it) at the moment a tab was first opened. An
investigator rejecting an inferred finding updated Neo4j correctly, but
the case screen kept showing the stale snapshot until the whole case
was reloaded from scratch. Postgres must only ever hold the AI-written
narrative text (the agent_summary_cache markdown for intake,
similar_cases, risk_assessment, and plan) plus plain AppWorks-sourced
data — never rule results, risk-score add-ons, or network-match data.
Those are cheap, idempotent Neo4j reads
(reasoning_layer.context_enrichment.enrich_graph_context,
reasoning_layer.risk_signals.apply_graph_risk_signals,
reasoning_layer.similar_cases.find_structural_matches,
reasoning_layer.investigation_tasks.build_rule_aware_tasks) and must be
fetched fresh on every request instead — including when a route serves
a cached agent_summary and skips the LLM entirely (see
api.pipeline_execution.fetch_live_graph_findings /
fetch_live_similar_cases / fetch_live_risk_signals /
fetch_live_rule_aware_tasks, called from every GET-like path in
api/server.py for /intake, /similar_cases, /risk_assessment, and /plan).

core.case_store.persist_case_session is the one and only function that
writes to case_ai_summary_store, so running every ai_summary through
strip_graph_derived_fields there is what makes this apply everywhere
(intake, similar_cases, risk_assessment, plan, copilot, and the
reject/revert sync path) without every caller having to remember to
call it.
"""

from typing import Any, Dict

from config.settings import (
    GRAPH_DERIVED_INVESTIGATION_KEYS,
    GRAPH_DERIVED_PLAN_KEYS,
    GRAPH_DERIVED_RISK_ASSESSMENT_KEYS,
    GRAPH_DERIVED_TOP_LEVEL_SECTIONS,
)


def strip_graph_derived_fields(ai_summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a COPY of `ai_summary` with every Neo4j-derived field removed,
    ready to persist to case_ai_summary_store. Never mutates the input —
    the same dict is also handed to the in-memory CS-4 CASE_STORE and to
    this request's own response, and both of those are allowed to keep
    the fresh graph data this request just computed; only the PERSISTED
    copy must have it stripped.

    Removes:
      * investigation.network_match_flag / graph_context / graph_signals
        / rules_fired  (Case Summary tab's graph findings)
      * top-level similar_cases                (Similar Cases tab's
        structural graph matches — the whole section is graph-derived)
      * risk_assessment.neo4j_signals           (Risk Assessment tab's
        graph signal detail)
      * investigation_plan.rule_aware_tasks     (Investigation Plan
        tab's rule-derived task recommendations)

    risk_assessment.risk_score / risk_tier are graph-augmented IN PLACE
    by reasoning_layer.risk_signals.apply_graph_risk_signals (the final
    value is itself a "risk score add-on"), so persisting them verbatim
    would still leak graph state into Postgres. Instead, the untouched
    AppWorks base_risk_score / base_risk_tier are written back under the
    plain risk_score / risk_tier keys, so a future cache hit re-augments
    from the same untouched starting point apply_graph_risk_signals
    always starts from, instead of double-applying graph signals on top
    of an already-augmented score.
    """
    cleaned: Dict[str, Any] = dict(ai_summary)

    investigation = dict(cleaned.get("investigation") or {})
    for key in GRAPH_DERIVED_INVESTIGATION_KEYS:
        investigation.pop(key, None)
    cleaned["investigation"] = investigation

    for key in GRAPH_DERIVED_TOP_LEVEL_SECTIONS:
        cleaned.pop(key, None)

    if isinstance(cleaned.get("risk_assessment"), dict):
        risk_assessment = dict(cleaned["risk_assessment"])
        for key in GRAPH_DERIVED_RISK_ASSESSMENT_KEYS:
            risk_assessment.pop(key, None)
        if "base_risk_score" in risk_assessment:
            risk_assessment["risk_score"] = risk_assessment.pop("base_risk_score")
        if "base_risk_tier" in risk_assessment:
            risk_assessment["risk_tier"] = risk_assessment.pop("base_risk_tier")
        cleaned["risk_assessment"] = risk_assessment

    if isinstance(cleaned.get("investigation_plan"), dict):
        investigation_plan = dict(cleaned["investigation_plan"])
        for key in GRAPH_DERIVED_PLAN_KEYS:
            investigation_plan.pop(key, None)
        cleaned["investigation_plan"] = investigation_plan

    return cleaned