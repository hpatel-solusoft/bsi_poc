"""
Owns: orchestrating a full ingest — AppWorks fetch -> Neo4j load ->
reasoning pipeline — for one case (the lifecycle-event path) or for many
(the POC/demo backfill path).

This is the module the AppWorks lifecycle event will call through
POST /graph/ingest. Until AppWorks is wired up to fire that event, the
exact same function is reachable from the CLI (python -m etl.run_sync).
The endpoint and the CLI are two doors into one implementation — there is
no separate "manual" code path that could drift from the production one.

Does NOT own: the AppWorks fetch or the Neo4j write (etl/graph_sync.py),
the rules (reasoning_layer/), or HTTP concerns (api/server.py). This file
is sequencing and failure policy, nothing else.

--- LOAD-ALL-THEN-REASON: THE ONE NON-OBVIOUS DECISION HERE ---

For a multi-case ingest, every case is LOADED before ANY case is
REASONED. This is not an optimisation; it is a correctness requirement,
and doing it the obvious way (load case, reason case, next case) produces
quietly wrong output.

Every rule in the library is cross-case by nature. Rule 1 fires when two
subjects share an employer — and those two subjects are usually on
different cases. If case A is reasoned before case B is loaded, the
shared employer between them does not exist in the graph yet, so Rule 1
does not fire, so Rule 2 (which needs Rule 1's edge) never forms the
fraud network. Nothing errors. The pipeline reports success. The network
simply is not there.

Load-all-then-reason removes that ordering hazard entirely: by the time
any rule runs, every structural fact the backfill was asked to bring in
is present.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core import graph_ingest_repository
from etl import graph_sync
from reasoning_layer import pipeline
from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

# AppWorks is a live REST service behind SAML; a transient 502 or a
# timeout on one of the dozens of calls a single case fetch makes should
# not fail the ingest of that case, let alone the backfill of eighteen.
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (2, 5)  # after attempt 1, then after attempt 2


# The subjects a case's reasoning runs for. The pipeline is scoped per
# (case, subject) — Principle 10 — so a case with three subjects needs
# three runs, not one.
_CASE_SUBJECTS_QUERY = """
MATCH (s:Subject)-[r:APPEARS_IN_CASE]->(c:Case {case_id: $case_id})
RETURN s.subject_id AS subject_id, coalesce(r.is_primary, false) AS is_primary
ORDER BY is_primary DESC, subject_id
"""


def _subjects_for_case(case_id: str) -> List[str]:
    """
    Every subject on the case. The pipeline is scoped per (case, subject)
    (Principle 10), so each subject gets its own run and its own completed
    pipeline_execution_state row — which is what gives every subject the
    ALLEGATION_LIKELY_AGAINST_SUBJECT attribution edges the Wave 2 network
    rules depend on. Reasoning only the primary subject would leave
    co-subjects unattributed and silently starve rules like the address
    fraud-network rule of their second endpoint, so there is no "primary
    only" mode.

    The underlying query still orders is_primary first, so the primary
    subject is reasoned before the co-subjects — but all of them are run.
    """
    with get_session() as session:
        rows = session.run(_CASE_SUBJECTS_QUERY, case_id=case_id).data()

    if not rows:
        logger.warning("ingest_service: case_id=%s has no subjects in the graph", case_id)
        return []

    return [row["subject_id"] for row in rows]


def load_case(case_id: str) -> Dict[str, Any]:
    """
    Phase 1 of an ingest: fetch from AppWorks, write to Neo4j. Retried
    with backoff on any exception, because the failure modes here are
    overwhelmingly transient (auth token expiry, a slow AppWorks
    relationship traversal, a network blip mid-fetch).

    Returns {case_id, status, counts, attempts, error}. Never raises —
    a bad case in a backfill of eighteen must not take down the other
    seventeen. The caller decides what a failure means.
    """
    last_error: Optional[str] = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            graph_ingest_repository.mark_started(case_id)
            counts = graph_sync.sync_case(case_id)
            graph_ingest_repository.mark_loaded(case_id, counts)
            return {"case_id": case_id, "status": "loaded", "counts": counts,
                    "attempts": attempt, "error": None}
        except Exception as exc:  # noqa: BLE001 — see docstring: isolate per-case failure
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "ingest_service: load FAILED case_id=%s attempt=%d/%d — %s",
                case_id, attempt, _MAX_ATTEMPTS, last_error,
            )
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_BACKOFF_SECONDS[attempt - 1])

    logger.error("ingest_service: load GAVE UP case_id=%s after %d attempts", case_id, _MAX_ATTEMPTS)
    graph_ingest_repository.mark_failed(case_id, last_error or "unknown error")
    return {"case_id": case_id, "status": "failed", "counts": {},
            "attempts": _MAX_ATTEMPTS, "error": last_error}


def reason_case(case_id: str) -> Dict[str, Any]:
    """
    Phase 2 of an ingest: run the reasoning pipeline for this case's
    subjects.

    force=True is passed to the pipeline deliberately. Principle 10's
    "runs once per subject per case" is about not re-running on every
    READ — an investigator reopening a tab, a Copilot question. A fresh
    AppWorks ingest is not a read: it has just changed the underlying
    facts, which makes the previous run's conclusions stale by
    definition. Not re-running would leave the graph asserting yesterday's
    inferences over today's data. This is the same "explicitly cleared"
    path Section 9.5 defines for the reload banner, reached from ETL
    rather than from a human clicking reload.
    """
    subject_ids = _subjects_for_case(case_id)
    runs: List[Dict[str, Any]] = []

    for subject_id in subject_ids:
        try:
            envelope = pipeline.run_pipeline(case_id, subject_id, force=True)
            result = envelope["result"]
            fired = [r["rule_id"] for r in result.get("rules_fired", []) if r["fired"]]
            runs.append({
                "subject_id": subject_id,
                "pipeline_status": result["pipeline_status"],
                "rules_fired": fired,
                "rules_fired_count": len(fired),
                "error": None,
            })
            logger.info(
                "ingest_service: reasoned case_id=%s subject_id=%s rules_fired=%d %s",
                case_id, subject_id, len(fired), fired,
            )
        except Exception as exc:  # noqa: BLE001 — one subject's failure is not the case's
            logger.exception("ingest_service: reasoning FAILED case_id=%s subject_id=%s", case_id, subject_id)
            runs.append({
                "subject_id": subject_id, "pipeline_status": "failed",
                "rules_fired": [], "rules_fired_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            })

    failed = [r for r in runs if r["error"]]
    if runs and not failed:
        graph_ingest_repository.mark_reasoned(case_id)

    return {
        "case_id": case_id,
        "pipeline_reasoned": len(runs),
        "subjects_failed": len(failed),
        "runs": runs,
    }


def ingest(case_ids: List[str], run_reasoning: bool = True) -> Dict[str, Any]:
    """
    The single entry point behind both POST /graph/ingest and the CLI.

    Sequence, in this order and for the reason in the module docstring:
        1. LOAD every case (AppWorks -> Neo4j)
        2. THEN REASON every case that loaded

    A case that failed to load is not reasoned over — reasoning against a
    half-present case produces confidently wrong output, which is worse
    than an obvious failure. It is reported as failed and the rest of the
    batch proceeds.

    Returns a report the caller can log, return over HTTP, or print.
    """
    logger.info(
        "ingest_service: START cases=%d run_reasoning=%s",
        len(case_ids), run_reasoning,
    )

    # --- Phase 1: load everything ---
    load_results = [load_case(case_id) for case_id in case_ids]
    loaded = [r["case_id"] for r in load_results if r["status"] == "loaded"]
    load_failed = [r for r in load_results if r["status"] == "failed"]

    logger.info("ingest_service: LOAD PHASE complete — loaded=%d failed=%d",
                len(loaded), len(load_failed))

    # --- Phase 2: reason over the now-complete graph ---
    pipeline_results: List[Dict[str, Any]] = []
    if run_reasoning:
        pipeline_results = [reason_case(case_id) for case_id in loaded]

    report = {
        "cases_requested": len(case_ids),
        "cases_loaded": len(loaded),
        "cases_load_failed": len(load_failed),
        "load_results": load_results,
        "pipeline_executed": run_reasoning,
        "pipeline_results": pipeline_results,
        "pipeline_reasoned": sum(r["pipeline_reasoned"] for r in pipeline_results),
        "pipeline_reasoning_failed": sum(r["subjects_failed"] for r in pipeline_results),
    }
    logger.info(
        "ingest_service: DONE loaded=%d/%d pipeline_reasoned=%d reasoning_failures=%d",
        report["cases_loaded"], report["cases_requested"],
        report["pipeline_reasoned"], report["pipeline_reasoning_failed"],
    )
    return report
