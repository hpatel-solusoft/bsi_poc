"""
Reasoning Pipeline orchestrator (Layer 4). Not an agent — an internal
service invoked by the Context Enrichment agent through the dispatcher's
"run_reasoning_pipeline" manifest.yaml entry (Principle 16), and by the
ETL ingest service after a case is loaded (etl/ingest_service.py).

Implements the full six-step sequence (Python Implementation Reference,
Section 5.3). All six steps are now built:

  1. Scope         — primary subject + one hop (reasoning_layer/scope.py)
  2. Wave 1        — Rules 1, 3, 5, 10, 11. Structural. No LLM.
  3. Extraction    — LLM reads :Commentary already loaded by ETL
  4. Graph Load    — attributions + narrative corroborations written to Neo4j
  5. Wave 2        — Rules 2, 4, 6, 7, 8, 9, 12, 13, then the Rule 14 modifier
  6. Serve         — rules_fired block returned to Context Enrichment

Guarantees this module is responsible for:
  * Principle 10 — runs once per subject per case, unless explicitly cleared
  * Principle 12 — rules write; nothing here read-and-returns for an agent
  * Principle 15 — failure is all-or-nothing; the next trigger re-runs from
                   the start. Safe precisely because every rule write is an
                   idempotent MERGE/SET
  * Principle 14 — a rejected fact is never re-asserted (the guard lives in
                   each rule's own Cypher, checked before its MERGE)

AppWorks dependency: none. Section 5.2 is explicit — the pipeline makes
zero AppWorks REST calls. Structured data arrives pre-loaded via ETL
(etl/graph_sync.py). This module only ever touches Neo4j and the LLM.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from neo4j.exceptions import Neo4jError

from core import pipeline_state_repository
from reasoning_layer import (
    commentary_reader,
    extraction_stage,
    graph_load,
    rule_engine,
    rule_registry,
    rules_fired,
    scope as scope_resolver,
)
from reasoning_layer.neo4j_client import GraphUnavailableError

logger = logging.getLogger(__name__)

# Re-exported for callers (tests, the ETL service) that need to know wave
# membership without importing the registry directly. Membership is by
# explicit LIST, never a numeric range — Rules 7, 8, 10 and 11 all fall
# outside a clean split, which is exactly what Section 5.4 warns about.
WAVE_1_RULE_IDS: List[str] = rule_registry.WAVE_1_RULE_IDS
WAVE_2_RULE_IDS: List[str] = rule_registry.WAVE_2_RULE_IDS


def _envelope(result: Dict[str, Any], sources: List[str], computed_by: str) -> dict:
    """The {result, provenance} envelope every dispatcher-routed function
    returns (Principle 8) — identical in shape to appworks_services.py's,
    so a Neo4j-sourced tool result and an AppWorks one are structurally
    indistinguishable to the agent layer (CS-2/CS-7)."""
    return {
        "result": result,
        "provenance": {
            "sources": sources,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": computed_by,
        },
    }


def _run_extraction_stage(case_id: str, subject_id: str) -> Dict[str, Any]:
    """
    Steps 3-4: Narrative Extraction + Graph Load.

    Reads narrative already in Neo4j (zero AppWorks calls), runs the
    Extraction Stage LLM call, then writes back two kinds of candidate
    fact:
      * attributions   — ALLEGATION_LIKELY_AGAINST_SUBJECT edges. Every
                         Wave 2 rule is gated on these existing.
      * corroborations — confirms_relationship_ids on the :Commentary
                         node, which is what Rule 14 later reads to
                         elevate a structurally-inferred relationship the
                         narrative independently confirms.
    """
    narrative_records = commentary_reader.get_narrative_records(subject_id)

    extraction_result = extraction_stage.run_extraction(subject_id, narrative_records)["result"]
    load_result = graph_load.load_extraction_output(case_id, subject_id, extraction_result)["result"]

    logger.info(
        "pipeline: extraction case_id=%s subject_id=%s attributions=%d unresolved=%d "
        "written=%d suppressed=%d corroborations_linked=%d",
        case_id, subject_id,
        len(extraction_result.get("attributions", [])),
        len(extraction_result.get("unresolved_allegation_ids", [])),
        len(load_result.get("written", [])),
        len(load_result.get("suppressed", [])),
        load_result.get("corroborations_linked", 0),
    )

    return {
        "attributions_extracted": len(extraction_result.get("attributions", [])),
        "unresolved_allegation_ids": extraction_result.get("unresolved_allegation_ids", []),
        "attributions_written": load_result.get("written", []),
        "attributions_suppressed": load_result.get("suppressed", []),
        "corroborations_linked": load_result.get("corroborations_linked", 0),
    }


def run_reasoning_pipeline(case_id: str, subject_id: str, force: bool = False) -> dict:
    """
    Entry point for the six-step sequence. Dispatched from the
    'run_reasoning_pipeline' manifest.yaml tool, and called directly by
    etl/ingest_service.py after a case is ingested.

    Idempotent per Principle 10: a case+subject that has already completed
    and has not been explicitly cleared returns immediately without
    touching the graph again.

    `force` exists for the ETL path only, and is not exposed as a tool
    parameter: a fresh AppWorks ingest brings new structural facts and new
    commentary into the graph, so the previous run's conclusions are stale
    by definition and re-running is the correct behaviour — this is the
    same "explicitly cleared" path Section 9.5 describes for the reload
    banner, reached from ETL instead of from a human clicking reload. An
    agent, by contrast, must never be able to force a re-run: that is
    exactly the loop Principle 10 exists to prevent.
    """
    existing = pipeline_state_repository.get_run_state(case_id, subject_id)
    already_done = (
        existing
        and existing.get("status") == "completed"
        and existing.get("cleared_at") is None
    )
    if already_done and not force:
        logger.info(
            "run_reasoning_pipeline SKIPPED case_id=%s subject_id=%s — already completed at %s "
            "(Principle 10)", case_id, subject_id, existing.get("completed_at"),
        )
        scope = scope_resolver.resolve_scope(case_id, subject_id)
        return _envelope(
            result={
                "pipeline_status": "already_completed",
                "wave1_completed_at": str(existing.get("wave1_completed_at")),
                "wave2_completed_at": str(existing.get("wave2_completed_at")),
                # rules_fired is re-read from Neo4j rather than cached in
                # Postgres (Data Persistence C.2 — Postgres never holds
                # inferred-relationship state). A skipped run still returns
                # the full block, so a caller never has to special-case it.
                "rules_fired": rules_fired.build_rules_fired(scope, []),
            },
            sources=["pipeline_execution_state", "Neo4j graph query"],
            computed_by="reasoning_layer.pipeline.run_reasoning_pipeline",
        )

    if already_done and force:
        pipeline_state_repository.clear_run(case_id, subject_id, reason="etl_resync")

    pipeline_state_repository.start_run(case_id, subject_id)

    try:
        # --- Step 1: scope ---
        rule_registry.ensure_registry()
        registry = rule_registry.load_registry()
        scope = scope_resolver.resolve_scope(case_id, subject_id)

        # --- Step 2: Wave 1 (structural) ---
        wave1_results = rule_engine.run_wave1(scope, registry)
        pipeline_state_repository.mark_wave1_complete(case_id, subject_id)
    except (GraphUnavailableError, Neo4jError):
        pipeline_state_repository.mark_failed(case_id, subject_id)
        logger.exception("Reasoning pipeline WAVE 1 FAILED case_id=%s subject_id=%s", case_id, subject_id)
        raise

    try:
        # --- Steps 3-4: Narrative Extraction + Graph Load ---
        extraction_results = _run_extraction_stage(case_id, subject_id)
        pipeline_state_repository.mark_extraction_complete(case_id, subject_id)
    except (GraphUnavailableError, Neo4jError, ValueError):
        # ValueError = an Extraction Stage LLM failure (unparseable JSON after
        # one retry, or output that fails ExtractionResult validation). Treated
        # identically to a Neo4j outage: a genuine failure of a stage that IS
        # built, so Principle 15 applies. Wave 1's writes stay durable in Neo4j
        # regardless — the next run re-MERGEs them harmlessly.
        pipeline_state_repository.mark_failed(case_id, subject_id)
        logger.exception("Reasoning pipeline EXTRACTION FAILED case_id=%s subject_id=%s", case_id, subject_id)
        raise

    try:
        # --- Step 5: Wave 2 (attribution-dependent) + the Rule 14 modifier ---
        # Wave 2 executes in dependency order (Rule 8 reads Rule 7's and the
        # network rules' output; Rule 13 reads Rule 7's). Rule 14 runs last,
        # once there is something for it to elevate.
        wave2_results = rule_engine.run_wave2(scope, registry)
        modifier_results = rule_engine.run_modifier(scope, registry)
        pipeline_state_repository.mark_wave2_complete(case_id, subject_id)
    except (GraphUnavailableError, Neo4jError):
        pipeline_state_repository.mark_failed(case_id, subject_id)
        logger.exception("Reasoning pipeline WAVE 2 FAILED case_id=%s subject_id=%s", case_id, subject_id)
        raise

    # --- Step 6: serve — the rules_fired contract (Functional Spec A.4) ---
    execution_records = wave1_results + wave2_results + modifier_results
    fired_block = rules_fired.build_rules_fired(scope, execution_records)

    return _envelope(
        result={
            "pipeline_status": "completed",
            "case_id": case_id,
            "subject_id": subject_id,
            "scope": {
                "subjects_in_scope": len(scope["scope_subject_ids"]),
                "cases_in_scope": len(scope["scope_case_ids"]),
                "expansion": scope["expansion"],
            },
            "wave1_results": wave1_results,
            "extraction_results": extraction_results,
            "wave2_results": wave2_results,
            "modifier_results": modifier_results,
            "rules_fired": fired_block,
        },
        sources=["Neo4j graph query"],
        computed_by="reasoning_layer.pipeline.run_reasoning_pipeline",
    )
