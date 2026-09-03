"""
Owns: deterministic structural similar-case matching (AI-14 / Sections
8.3, 9.2) — a single read-only Cypher query that replaces Phase 1's
non-deterministic two-step LLM type-selection (get_allegation_types then
search_similar_cases).

Section 8.3 defines the change precisely:
  * matching is a Cypher property match on allegation_type in Neo4j,
    deterministic — the LLM no longer decides what matches;
  * similarity is COMPUTED, not the hardcoded 1.0 of Phase 1:
        allegation type exact match  -> 0.5 base (the entry requirement)
    giving a score of exactly 0.5 for every match (see DEVIATION below);
  * new output fields: match_reasons (which dimensions matched) and
    source: "structural_graph".

DEVIATION FROM SECTION 8.3 — SUBJECT-BASED DIMENSIONS REMOVED: Section
8.3 originally specified THREE structural dimensions (allegation_type,
+0.25 for a shared-Subject Employer FEIN, +0.25 for shared-Subject
FraudNetwork membership), giving a score in [0.5, 1.0]. Both of the
dropped dimensions traversed through :Subject nodes on each case
(`(c1)<-[:APPEARS_IN_CASE]-(:Subject)-[...]->(:Subject)-[:APPEARS_IN_CASE]->(c2)`).
Per direct instruction, only the allegation_type dimension remains —
matching is now purely on shared allegation_type, so similarity_score is
always exactly 0.5 and match_reasons always exactly ["allegation_type"].
The two OPTIONAL MATCH blocks for Employer FEIN and FraudNetwork are
removed entirely rather than left dead in the query. This does NOT touch
the prior-guilty exclusion below — that filter also traverses through
:Subject, but it is a candidate-set exclusion (a disqualifier), not a
scoring dimension, and was explicitly kept as-is.

EXCLUSION — A PRIOR-GUILTY CASE IS NOT A "SIMILAR" CASE: a case c2 is
dropped from the candidate set (never scored, never returned) when any
subject on the active case c1 has an active HAS_PRIOR_GUILTY_CASE edge to
c2. That edge (Rule_07_Prior_Guilty; see rules/wave2/rule_07_prior_guilty
.cypher) already means c2 has been adjudicated and attributed to one of
c1's subjects — it belongs in the Prior Guilty / recidivism surfaces
(risk_signals.py, investigation_tasks.py), not in a list meant to suggest
*new* candidate cases an investigator hasn't already resolved a verdict
on. Filtered the same way risk_signals.py reads this edge (`pg.status =
"active"`) so an investigator-rejected prior-guilty link does not
suppress a case here either — rejection restores it as an ordinary
similar-case candidate.

The LLM's role becomes EXPLAINING what the graph found, never selecting it
(Section 8.3, 9.2 Turn 2). This module makes no LLM call and no AppWorks
call — it is a pure Neo4j read.

WHY THIS IS A DIRECT CALL, NOT A MANIFEST TOOL:
Section 9.2 sketches find_structural_similar_cases as a dispatcher-routed
tool, but it resolves to Neo4j, not AppWorks. Per the governance rule that
manifest.yaml holds a tool ONLY IF it is LLM-called AND makes an AppWorks
call, this is invoked directly by the /similar_cases route (the same
pattern as check_network_match and enrich_graph_context), and its result
is injected into the LLM's context so the LLM can explain it.

DETERMINISM (AI-14 todo — "same input must return the same results on
repeated runs"): the query aggregates with collect(DISTINCT ...) and
ORDERs BY score DESC, case_id ASC (score is now a constant 0.5, so this
is effectively an order on case_id alone). There is no LLM, no randomness,
and a total order on ties (case_id), so repeated runs on an unchanged
graph return byte-identical output.

Does NOT own: the AppWorks search_similar_cases path (now unused for this
flow), the pipeline, or any write.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from config.settings import SIMILAR_CASES_MAX_TOTAL
from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# One read-only statement, one matching dimension (see DEVIATION FROM
# SECTION 8.3 above — the Employer-FEIN and FraudNetwork subject-based
# dimensions were removed by direct instruction).
#
# Dimension 1 (base, required, only dimension): the candidate case shares
#   at least one allegation_type with the active case. Cases with no shared
#   type are not similar and never appear — this is the +0.5 entry
#   requirement, so every returned case carries "allegation_type" in its
#   reasons and scores exactly 0.5.
#
# Matches use lower-cased allegation_type so "PCA" and "pca" unify, mirroring
# the case-insensitive CONTAINS the rule library already uses.
_SIMILAR_CASES_QUERY = """
MATCH (c1:Case {case_id: $case_id})-[:HAS_ALLEGATION]->(a1:Allegation)
WITH c1, collect(DISTINCT toLower(a1.allegation_type)) AS c1_types
WHERE size(c1_types) > 0

MATCH (c2:Case)-[:HAS_ALLEGATION]->(a2:Allegation)
// toString on both sides: a bare `<>` between $case_id (always a str here —
// find_structural_matches does str(case_id).strip()) and a c2.case_id
// property stored as a different type (e.g. int) evaluates to null in
// Cypher, which WHERE treats as false — so the row is NOT filtered and the
// current case can leak into its own "similar cases" results. Comparing
// as strings on both sides keeps the exclusion correct regardless of how
// case_id is typed in the graph.
WHERE toString(c2.case_id) <> toString($case_id)
  AND toLower(a2.allegation_type) IN c1_types
  // Prior-guilty exclusion: c2 is not a "similar" case if it is already
  // a resolved prior-guilty case for one of c1's subjects — that's a
  // recidivism finding (Rule_07_Prior_Guilty), a different surface, not
  // a new candidate to investigate. Scoped to c1's subjects (not c2's),
  // since the question is "does someone on the ACTIVE case already have
  // a guilty verdict on c2", and to pg.status = "active" so a rejected
  // prior-guilty link doesn't wrongly suppress a genuine similar case.
  //
  // PRIMARY-ON-c2 REQUIRED: the same subject `s` must ALSO be the primary
  // subject (APPEARS_IN_CASE.is_primary = true) on c2 itself, not merely
  // a co-subject there. HAS_PRIOR_GUILTY_CASE already tells us the guilty
  // verdict on c2 is attributable to s (Rule_07 uses
  // ALLEGATION_LIKELY_AGAINST_SUBJECT for that) — but that's a fact about
  // whose conduct was found guilty, not about whose case c2 IS. A subject
  // who was a co-subject (e.g. PCA, witness) on c2 while someone else was
  // primary there doesn't make c2 disqualified as a similar case; only
  // being the primary subject of c2 does. Per investigator review of live
  // data (case 658407434): 5 candidates share an active
  // HAS_PRIOR_GUILTY_CASE edge from the same subject, but that subject is
  // primary on the candidate case for only some of them — the rest have
  // him as a co-subject there and belong back in results. See the
  // verification query in this module's docstring before trusting this
  // blind; the query below encodes the rule, not a specific case's counts.
  AND NOT EXISTS {
        MATCH (c1)<-[:APPEARS_IN_CASE]-(s:Subject)-[pg:HAS_PRIOR_GUILTY_CASE]->(c2)
        MATCH (s)-[ap2:APPEARS_IN_CASE]->(c2)
        WHERE pg.status = "active" AND ap2.is_primary = true
      }
WITH c2, collect(DISTINCT a2.allegation_type) AS shared_types,
     0.5 AS similarity_score,
     ["allegation_type"] AS match_reasons
RETURN
    c2.case_id           AS case_id,
    c2.complaint_number  AS complaint_no,
    c2.status            AS status,
    c2.fraud_amount      AS fraud_amount,
    c2.opened_date       AS date_opened,
    shared_types         AS matched_allegation_types,
    similarity_score,
    match_reasons
ORDER BY similarity_score DESC, case_id ASC
"""


def find_structural_matches(case_id: str, limit: int = SIMILAR_CASES_MAX_TOTAL) -> dict:
    """
    Return structurally similar cases for `case_id`, each scored exactly
    0.5 (see the module docstring's DEVIATION FROM SECTION 8.3 note —
    the Employer-FEIN and FraudNetwork scoring dimensions were removed;
    only the allegation_type dimension remains).

    Args:
        case_id: the active case to find matches for. Required, non-empty.
        limit:   maximum matches to return (already ordered strongest-first).
            Defaults to config.settings.SIMILAR_CASES_MAX_TOTAL (5) — not a
            bare literal here, so the display cap for the /similar_cases
            tab (api/pipeline_execution.py's caller relies on this
            default) lives in exactly one place, alongside
            SIMILAR_CASES_MAX_PER_TYPE/REQUIRED_STATUS/LOOKBACK_YEARS —
            the rest of this feature's tunable constants. A caller that
            genuinely wants a different cap (e.g.
            reasoning_layer/copilot_templates.py's
            get_structural_similar_cases, deliberately a higher, separate
            default for the Copilot tool) still passes limit= explicitly;
            this default only governs callers that don't.

    Returns (inside the standard {result, provenance} envelope):
        {
          "matches": [
            { case_id, complaint_no, status, fraud_amount, date_opened,
              matched_allegation_types, similarity_score, match_reasons }
          ],
          "source": "structural_graph",
          "total_candidates_scored": int
        }

    An active case with no allegations, or one absent from the graph,
    yields an empty match list — not an error. That is the honest answer:
    nothing to match on.

    A candidate case already linked to one of case_id's subjects via an
    active HAS_PRIOR_GUILTY_CASE edge is excluded from matches (and from
    total_candidates_scored) — see the module docstring's EXCLUSION note.

    Raises:
        ValueError: on a missing/blank case_id.
        GraphUnavailableError / Neo4jError: propagated; the /similar_cases
            route degrades to an empty, clearly-unavailable section rather
            than failing.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("find_structural_matches requires a non-empty case_id")
    case_id = str(case_id).strip()

    with get_session() as session:
        rows = session.run(_SIMILAR_CASES_QUERY, case_id=case_id).data()

    matches: List[Dict[str, Any]] = [
        {
            "case_id": row["case_id"],
            "complaint_no": row.get("complaint_no"),
            "status": row.get("status"),
            "fraud_amount": row.get("fraud_amount"),
            "date_opened": row.get("date_opened"),
            "matched_allegation_types": list(row.get("matched_allegation_types") or []),
            "similarity_score": round(float(row["similarity_score"]), 2),
            "match_reasons": list(row.get("match_reasons") or []),
        }
        for row in rows
    ]
    total_scored = len(matches)
    if limit is not None and limit >= 0:
        matches = matches[:limit]

    logger.info(
        "find_structural_matches: case_id=%s candidates_scored=%d returned=%d",
        case_id,
        total_scored,
        len(matches),
    )

    return {
        "result": {
            "matches": matches,
            "source": "structural_graph",
            "total_candidates_scored": total_scored,
        },
        "provenance": graph_provenance("reasoning_layer.similar_cases"),
    }
