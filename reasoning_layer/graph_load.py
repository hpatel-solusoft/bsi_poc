"""
Owns: Step 4 (Graph Load, Section 5.3) — writing the Extraction Stage's
validated candidate facts into Neo4j.

Two kinds of candidate fact, both produced by the same LLM call:

  1. Attributions   -> (:Allegation)-[:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(:Subject)
     Every Wave 2 rule is gated on these existing. Written with the
     Section 3.3 provenance quartet (confidence / source_rule /
     asserted_at / status), exactly like any other inferred relationship.

  2. Corroborations -> (:Commentary).confirms_relationship_ids
     The narrative independently confirming a structural relationship
     Wave 1 asserted. This is the input Rule 14 reads to elevate Medium
     to High. Section 6.2's Rule 14 example calls it
     comm.confirms_relationship_id, "set by the Extraction Stage" —
     widened to a list here, because one comment routinely confirms more
     than one relationship and a scalar would silently drop all but the
     last.

Also enforces Section 5.5 at write time: "every future pipeline run
checks for an existing rejection before re-asserting the same fact." This
module READS :Rejection nodes defensively; it never creates them
(POST /reject_inference, Phase 9, owns that). A suppressed fact returns
a visible note rather than vanishing — the system stays quiet about the
fact, never about the rejection.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# ON CREATE / ON MATCH mirrors Rule 1's worked example (Section 6.2):
# idempotent by design (Principle 15), so a re-run refreshes an existing
# edge rather than duplicating it. The rejection guard is a WHERE before
# the MERGE, so a rejected attribution is never re-asserted and its
# status:"rejected" is never quietly flipped back.
_WRITE_ATTRIBUTION = """
MATCH (al:Allegation {allegation_id: $allegation_id})
MATCH (s:Subject {subject_id: $subject_id})
WHERE NOT EXISTS {
    MATCH (rej:Rejection {
        relationship_type: "ALLEGATION_LIKELY_AGAINST_SUBJECT",
        from_key: $allegation_id,
        to_key:   $subject_id,
        status:   "active"
    })
}
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
ON CREATE SET r.first_asserted_at = $asserted_at,
              r.status            = "active"
SET r.confidence         = $confidence,
    r.source_rule        = "Extraction_Stage",
    r.asserted_at        = $asserted_at,
    r.rationale          = $rationale,
    r.source_comment_ids = $source_comment_ids
RETURN elementId(r) AS rel_id
"""

_CHECK_REJECTED = """
MATCH (rej:Rejection {
    relationship_type: "ALLEGATION_LIKELY_AGAINST_SUBJECT",
    from_key: $allegation_id,
    to_key:   $subject_id,
    status:   "active"
})
RETURN rej.rejected_by AS rejected_by, rej.rejected_at AS rejected_at, rej.reason AS reason
LIMIT 1
"""

# The relationship_ref must resolve to a relationship that actually exists
# (elementId is checked against the live graph) before it is recorded as
# confirmed. An LLM that invents a plausible-looking element id would
# otherwise cause Rule 14 to elevate nothing while reporting success.
_WRITE_CORROBORATION = """
MATCH (comm:Commentary {comment_id: $comment_id})
MATCH ()-[r]-()
WHERE elementId(r) = $relationship_ref
SET comm.confirms_relationship_ids =
        CASE WHEN comm.confirms_relationship_ids IS NULL
             THEN [$relationship_ref]
             WHEN $relationship_ref IN comm.confirms_relationship_ids
             THEN comm.confirms_relationship_ids
             ELSE comm.confirms_relationship_ids + $relationship_ref END,
    comm.confirmed_by  = "Extraction_Stage",
    comm.confirmed_at  = $asserted_at
RETURN elementId(r) AS confirmed_ref
"""


def load_extraction_output(case_id: str, subject_id: str,
                           extraction_result: Dict[str, Any]) -> dict:
    """
    Write every attribution and every corroboration in `extraction_result`.

    `extraction_result` is a validated ExtractionResult.model_dump() —
    already schema-checked by extraction_stage.run_extraction, so this
    function trusts its shape rather than re-validating it. What it does
    NOT trust is the *content*: an id that passed schema validation
    (right shape) can still be hallucinated (wrong value), which is why
    every write matches against real graph nodes and a non-matching
    attribution is logged and dropped rather than counted as written.

    Returns, inside the standard {result, provenance} envelope:
        {
          "written":    [{allegation_id, subject_id, confidence, rel_id}],
          "suppressed": [{allegation_id, subject_id, note}],
          "dropped":    [{allegation_id, subject_id, reason}],
          "corroborations_linked": int,
        }
    """
    written: List[Dict[str, Any]] = []
    suppressed: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    corroborations_linked = 0
    asserted_at = datetime.now(timezone.utc).isoformat()

    with get_session() as session:
        # --- attributions ---
        for attribution in extraction_result.get("attributions", []):
            allegation_id = attribution["allegation_id"]
            attributed_subject_id = attribution["subject_id"]

            record = session.run(
                _WRITE_ATTRIBUTION,
                allegation_id=allegation_id,
                subject_id=attributed_subject_id,
                confidence=attribution["confidence"],
                rationale=attribution.get("rationale", ""),
                source_comment_ids=attribution.get("source_comment_ids", []),
                asserted_at=asserted_at,
            ).single()

            if record is not None:
                written.append({
                    "allegation_id": allegation_id,
                    "subject_id": attributed_subject_id,
                    "confidence": attribution["confidence"],
                    "rel_id": record["rel_id"],
                })
                continue

            # The MERGE produced no row. Two possible reasons, and they must
            # not be conflated: a live rejection suppressed it (expected,
            # reportable), or the ids do not exist in the graph (an LLM
            # hallucination that passed schema validation because its SHAPE
            # was valid even though its VALUES were not).
            rejection = session.run(
                _CHECK_REJECTED, allegation_id=allegation_id, subject_id=attributed_subject_id,
            ).single()

            if rejection is not None:
                note = (
                    f"previously flagged and rejected by {rejection['rejected_by']} "
                    f"on {rejection['rejected_at']}"
                )
                suppressed.append({
                    "allegation_id": allegation_id,
                    "subject_id": attributed_subject_id,
                    "note": note,
                })
                logger.info("graph_load: SUPPRESSED allegation_id=%s subject_id=%s — %s",
                            allegation_id, attributed_subject_id, note)
            else:
                dropped.append({
                    "allegation_id": allegation_id,
                    "subject_id": attributed_subject_id,
                    "reason": "allegation_id/subject_id not found in graph",
                })
                logger.warning(
                    "graph_load: DROPPED attribution allegation_id=%s subject_id=%s — matched "
                    "neither a real Allegation/Subject pair nor a Rejection; likely an "
                    "LLM-hallucinated id",
                    allegation_id, attributed_subject_id,
                )

        # --- corroborations (Rule 14's input) ---
        for corroboration in extraction_result.get("corroborations", []):
            record = session.run(
                _WRITE_CORROBORATION,
                comment_id=corroboration["comment_ref"],
                relationship_ref=corroboration["relationship_ref"],
                asserted_at=asserted_at,
            ).single()
            if record is not None:
                corroborations_linked += 1
            else:
                logger.warning(
                    "graph_load: DROPPED corroboration comment_ref=%s relationship_ref=%s — "
                    "one or both do not exist in the graph",
                    corroboration.get("comment_ref"), corroboration.get("relationship_ref"),
                )

    logger.info(
        "graph_load: case_id=%s subject_id=%s written=%d suppressed=%d dropped=%d corroborations=%d",
        case_id, subject_id, len(written), len(suppressed), len(dropped), corroborations_linked,
    )

    return {
        "result": {
            "written": written,
            "suppressed": suppressed,
            "dropped": dropped,
            "corroborations_linked": corroborations_linked,
        },
        "provenance": graph_provenance(
            "reasoning_layer.graph_load.load_extraction_output",
            ["Neo4j write — ALLEGATION_LIKELY_AGAINST_SUBJECT, Commentary corroboration"],
            # A write cites when it was ASSERTED, not when this envelope
            # was assembled.
            retrieved_at=asserted_at,
        ),
    }