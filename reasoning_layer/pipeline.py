"""
Reasoning Pipeline orchestrator (Layer 4). Not an agent and NOT an LLM
tool. Per Section 9.1 it is invoked directly by Context Enrichment's own
processing (reasoning_layer/context_enrichment.py) once fetch_subject_history
has returned, and by the ETL ingest service after a case is loaded
(etl/ingest_service.py). The LLM never sees run_pipeline as a callable
tool — it is not registered in manifest.yaml.

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
from reasoning_layer.neo4j_client import GraphUnavailableError, get_session
from utils.provenance import graph_provenance

_CONFIDENCE_ORDER = {"Unresolved": 0, "Medium": 1, "High": 2}

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
        "provenance": graph_provenance(computed_by, sources),
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


def run_pipeline(case_id: str, subject_id: str, force: bool = False, reason: str = "etl_resync") -> dict:
    """
    Entry point for the six-step sequence. Per Section 9.1 this is called
    DIRECTLY — by Context Enrichment's own processing
    (reasoning_layer/context_enrichment.py) and by etl/ingest_service.py
    after a case is ingested. It is deliberately NOT a manifest.yaml tool:
    the LLM never selects it. Keeping the pipeline out of the tool
    catalogue is what makes "the pipeline is invoked by Context
    Enrichment's own processing, not called by the LLM as a tool" true in
    code rather than by convention.

    Idempotent per Principle 10: a case+subject that has already completed
    and has not been explicitly cleared returns immediately without
    touching the graph again.

    `force` is not exposed as a tool parameter and is never reachable by
    the LLM — that is exactly the loop Principle 10 exists to prevent.
    It is set by two callers: etl/ingest_service.py after a fresh AppWorks
    ingest (previous conclusions are stale by definition), and the API
    routes' reload_ai_summary=True path (Section 9.5's "reload banner",
    a human/caller-triggered equivalent of the ETL resync rather than one
    reached from ETL itself). `reason` records which one, written to
    pipeline_execution_state.cleared_reason so the audit trail shows why a
    given run was invalidated.
    """
    existing = pipeline_state_repository.get_run_state(case_id, subject_id)
    already_done = (
        existing
        and existing.get("status") == "completed"
        and existing.get("cleared_at") is None
    )
    if already_done and not force:
        logger.info(
            "run_pipeline SKIPPED case_id=%s subject_id=%s — already completed at %s "
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
            computed_by="reasoning_layer.pipeline.run_pipeline",
        )

    if already_done and force:
        pipeline_state_repository.clear_run(case_id, subject_id, reason=reason)

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
        computed_by="reasoning_layer.pipeline.run_pipeline",
    )

# ---------------------------------------------------------------------------
# Case-level orchestration: every subject, not just the primary
# ---------------------------------------------------------------------------
_CASE_SUBJECTS_QUERY = """
MATCH (s:Subject)-[r:APPEARS_IN_CASE]->(c:Case {case_id: $case_id})
RETURN s.subject_id AS subject_id, coalesce(r.is_primary, false) AS is_primary
ORDER BY is_primary DESC, subject_id
"""


def subjects_for_case(case_id: str) -> List[str]:
    """
    Every subject on the case, primary first.

    The pipeline is scoped per (case, subject) — Principle 10 keys its
    completion state that way — so each subject needs its own run. Reasoning
    only the primary subject leaves co-subjects without their
    ALLEGATION_LIKELY_AGAINST_SUBJECT attribution edges, which silently
    starves the Wave 2 network rules of their second endpoint: an address
    fraud network needs BOTH subjects reasoned before it can form. That is
    why there is no "primary only" mode.

    Primary-first ordering is deliberate rather than cosmetic: the primary
    subject's Wave 1 edges are in place before a co-subject's Wave 2 run
    looks for them.
    """
    with get_session() as session:
        rows = session.run(_CASE_SUBJECTS_QUERY, case_id=case_id).data()
    if not rows:
        logger.warning("run_pipeline_for_case: case_id=%s has no subjects in the graph", case_id)
        return []
    return [r["subject_id"] for r in rows if r.get("subject_id")]


def _reasoning_population_for_case(case_id: str, direct_subject_ids: List[str]) -> List[str]:
    """
    The full set of subjects that must each get their own pipeline run so
    Wave 2's cross-endpoint rules (2, 4, 6, 8, 9) have BOTH sides'
    ALLEGATION_LIKELY_AGAINST_SUBJECT attribution edges available — not
    just `direct_subject_ids` (who is literally on this Workfolder).

    subjects_for_case() answers "who APPEARS_IN_CASE here". That is NOT
    the same question Rules 2/4/6/8/9 ask. Those rules match pairs across
    SHARES_EMPLOYER_WITH / SHARES_ADDRESS_WITH / SHARES_ALIAS_PATTERN_WITH
    / IS_CO_SUBJECT_WITH, and scope.py's whole reason for existing is that
    such a pair is frequently NOT both on the same Workfolder — a
    co-subject can be pulled in one hop out via a shared employer even
    though their own allegations sit on a different case entirely.

    Wave 1/Wave 2 rule MATCHes already see that co-subject correctly,
    because rule_engine.py filters on scope_subject_ids, which scope.py
    expands one hop out. But the Extraction Stage — the step that writes
    ALLEGATION_LIKELY_AGAINST_SUBJECT — only ever ran for `direct_subject_ids`
    up to this point. The out-of-case party's own allegation never got an
    attribution edge, so any rule requiring BOTH endpoints attributed
    (Rule 2, 4, 6, 9) silently produced writes=0 forever, no matter how
    many times /intake ran — and Rule 8, which depends on Rule 2/4/6/9's
    output, starved right along with it.

    Concretely: an /intake of case 658407433 alone could never make
    Rule 2 fire for Smith + Nunes, because Nunes's own case (658423814)
    was never reasoned, so Nunes never got his own attribution edge.

    Fix: resolve one-hop scope for every direct subject (reusing
    scope.py's own query — no new graph traversal logic here) and union
    the result into the reasoning population. Deliberately NOT recursive:
    it takes exactly the one hop scope.py already documents as the line
    Section 5.2 draws, so this cannot turn into the full-graph scan
    scope.py itself warns against. A subject pulled in this way is
    reasoned under the CURRENT case_id (Principle 10's
    pipeline_execution_state key is (case_id, subject_id), so this does
    not collide with — or replace — that subject's own run under their
    real case; both can exist independently), and Rule 13's
    primary-subject scoping is unaffected since it keys off the subject
    actually passed to run_pipeline, not off is_primary in this case's
    Workfolder.
    """
    population: List[str] = list(direct_subject_ids)
    seen = set(direct_subject_ids)
    for subject_id in direct_subject_ids:
        # A scope failure must not silently shrink the population back to
        # "direct subjects only" and then report rules_fired as if it were
        # complete — surface it exactly like run_pipeline itself would.
        scope = scope_resolver.resolve_scope(case_id, subject_id)
        for sid in scope.get("scope_subject_ids", []):
            if sid and sid not in seen:
                seen.add(sid)
                population.append(sid)
    if len(population) > len(direct_subject_ids):
        logger.info(
            "run_pipeline_for_case: case_id=%s expanded reasoning population from "
            "%d direct subject(s) to %d via one-hop scope (co-subject/employer/"
            "address/alias) — extra: %s",
            case_id, len(direct_subject_ids), len(population),
            sorted(seen - set(direct_subject_ids)),
        )
    return population


def _merge_rules_fired(blocks: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Fold per-subject rules_fired blocks into ONE case-level block.

    Each block is the same fixed 14 entries, so they are merged rule by
    rule. Instances are concatenated and de-duplicated: overlapping scopes
    mean the same edge is legitimately seen from both endpoints, and
    counting it twice would inflate evidence_count.

    Rule-level confidence is the highest across all subjects and
    corroborated is true if any instance was corroborated — the same
    optimistic roll-up _summarise applies within a subject, for the same
    reason: the detail survives in `instances`.

    FRAUD-NETWORK INSTANCES (Rule 2/4/6/9) are keyed on `related_network_key`
    alone, not the full instance. rules_fired.py already collapses each of
    those rules to one row per network within a single run_pipeline call —
    but run_pipeline_for_case calls run_pipeline once per subject, and each
    call independently picks its own arbitrary anchor subject
    (subject_id/subject_name) for that same network. Two per-subject runs
    of the identical network therefore differ only in which subject got
    picked as anchor, which the generic full-instance key does not
    recognise as a duplicate — subject_id there is incidental, not part of
    the network's identity, so keying on it produced the same inference
    line twice. Non-network rules (e.g. Rule 1's directed subject-to-subject
    pairs) still use the full-instance key, since for those subject_id is
    the actual identity of a distinct, directional instance.
    """
    if not blocks:
        return []

    merged: List[Dict[str, Any]] = []
    for index, template in enumerate(blocks[0]):
        entry = dict(template)
        instances: List[Dict[str, Any]] = []
        seen = set()
        for block in blocks:
            for instance in block[index].get("instances", []):
                network_key = instance.get("related_network_key")
                if network_key is not None:
                    key = ("related_network_key", str(network_key))
                else:
                    key = tuple(sorted(
                        (k, str(v)) for k, v in instance.items()
                        if k not in ("confidence", "corroborated")
                    ))
                if key in seen:
                    continue
                seen.add(key)
                instances.append(instance)

        confidences = [i.get("confidence") for i in instances if i.get("confidence")]
        entry["instances"] = instances
        entry["evidence_count"] = len(instances)
        entry["fired"] = len(instances) > 0
        entry["confidence"] = (
            max(confidences, key=lambda c: _CONFIDENCE_ORDER.get(c, 0))
            if confidences else "Unresolved"
        )
        entry["corroborated"] = any(i.get("corroborated") for i in instances)
        entry["writes_this_run"] = sum(
            block[index].get("writes_this_run", 0) or 0 for block in blocks
        )
        # A rule is only genuinely "skipped" if it was skipped for every
        # subject; skipped for one and run for another is not a skip.
        reasons = {block[index].get("skipped_reason") for block in blocks}
        entry["skipped_reason"] = reasons.pop() if len(reasons) == 1 else None
        merged.append(entry)
    return merged


def run_pipeline_for_case(case_id: str, force: bool = False,
                          reason: str = "etl_resync") -> dict:
    """
    Run the pipeline for EVERY subject on the case and return one merged
    rules_fired block.

    This is the entry point Context Enrichment and the ETL should use.
    run_pipeline stays per-subject because that is the unit Principle 10
    tracks in pipeline_execution_state — this function orchestrates it, it
    does not replace it. A subject whose run fails does not abort the
    others: a co-subject with bad data must not cost the investigator the
    reasoning on everyone else.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("run_pipeline_for_case requires a non-empty case_id")
    case_id = str(case_id).strip()

    direct_subject_ids = subjects_for_case(case_id)
    if not direct_subject_ids:
        return _envelope(
            result={"pipeline_status": "no_subjects", "case_id": case_id,
                    "subjects_run": [], "subject_count": 0, "rules_fired": []},
            sources=["Neo4j graph query"],
            computed_by="reasoning_layer.pipeline.run_pipeline_for_case",
        )

    # Reason every subject one hop out (co-subject/employer/address/alias),
    # not just the subjects literally on this Workfolder — see
    # _reasoning_population_for_case's docstring. Without this, Rules
    # 2/4/6/9 (and Rule 8, which depends on them) can never fire for a
    # co-subject whose own allegations sit on a different case.
    subject_ids = _reasoning_population_for_case(case_id, direct_subject_ids)

    blocks: List[List[Dict[str, Any]]] = []
    ran: List[Dict[str, Any]] = []
    for subject_id in subject_ids:
        try:
            envelope = run_pipeline(case_id, subject_id, force=force, reason=reason)
            result = envelope["result"]
            block = result.get("rules_fired") or []
            if block:
                blocks.append(block)
            ran.append({"subject_id": subject_id,
                        "pipeline_status": result.get("pipeline_status")})
        except Exception as exc:  # noqa: BLE001 — one subject must not sink the case
            logger.error(
                "run_pipeline_for_case: case_id=%s subject_id=%s FAILED — %s",
                case_id, subject_id, exc,
            )
            ran.append({"subject_id": subject_id, "pipeline_status": "failed",
                        "error": str(exc)})

    merged = _merge_rules_fired(blocks)
    fired = sum(1 for e in merged if e.get("fired"))
    logger.info(
        "run_pipeline_for_case: case_id=%s direct_subjects=%d reasoned_subjects=%d "
        "rules_fired=%d/%d",
        case_id, len(direct_subject_ids), len(subject_ids), fired, len(merged),
    )
    return _envelope(
        result={
            "pipeline_status": "completed",
            "case_id": case_id,
            "direct_subject_count": len(direct_subject_ids),
            "subjects_run": ran,
            "subject_count": len(subject_ids),
            "rules_fired": merged,
        },
        sources=["Neo4j graph query"],
        computed_by="reasoning_layer.pipeline.run_pipeline_for_case",
    )