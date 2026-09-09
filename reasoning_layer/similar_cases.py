"""
Owns: deterministic structural similar-case matching (AI-14 / Sections
8.3, 9.2) — a single read-only Cypher query that replaces Phase 1's
non-deterministic two-step LLM type-selection (get_allegation_types then
search_similar_cases).

FOUR STRUCTURAL DIMENSIONS:

  Dimension 1 — Allegation type (base, required):
      The candidate case shares at least one allegation_type with the
      active case.  Cases with no shared type are excluded entirely.
      Weight: SIMILAR_CASES_SCORE_BASE.

  Dimension 2 — Allegation description keyword overlap:
      At least one significant word (longer than SIMILAR_CASES_DESCRIPTION_
      MIN_WORD_LENGTH chars) from the active case's allegation comment_text
      appears in the candidate's allegation comment_text (both lowercased).
      Weight: SIMILAR_CASES_SCORE_DESCRIPTION.

  Dimension 3 — Shared employer (via SHARES_EMPLOYER_WITH):
      Uses Rule_01's already-computed SHARES_EMPLOYER_WITH edges rather
      than re-traversing through :Employer/:EMPLOYED_BY nodes.  The
      traversal is:
          (c1)<-[:APPEARS_IN_CASE]-(s1:Subject)
               -[:SHARES_EMPLOYER_WITH]-
               (s2:Subject)-[:APPEARS_IN_CASE]->(c2)
      This is more reliable than the :EMPLOYED_BY path because:
        * SHARES_EMPLOYER_WITH connects subjects across cases directly,
          regardless of whether the other case's employer data was ETL-
          synced into the graph.
        * Rule_01 already ran the FEIN-matching logic; the similar-cases
          query does not need to duplicate it.
        * The edges include both active and previously-rejected connections
          — structural similarity is independent of whether an investigator
          chose to exclude a connection for the current case.
      Weight: SIMILAR_CASES_SCORE_EMPLOYER_FEIN.

  Dimension 4 — Shared fraud network membership:
      Uses Rule_02/04/06's MEMBER_OF_FRAUD_NETWORK edges:
          (c1)<-[:APPEARS_IN_CASE]-(:Subject)
               -[:MEMBER_OF_FRAUD_NETWORK]->(fn:FraudNetwork)
               <-[:MEMBER_OF_FRAUD_NETWORK]-(:Subject)
               -[:APPEARS_IN_CASE]->(c2)
      Weight: SIMILAR_CASES_SCORE_FRAUD_NETWORK.

ALL WEIGHTS live in config/settings.py and must sum to 1.0.
DESCRIPTION WORD LENGTH THRESHOLD also lives in config/settings.py.
Nothing in this module is hardcoded — change settings only to retune.

EXCLUSION — OWN-HISTORY IS NOT "SIMILAR":
A case c2 is dropped from the candidate set when c1's PRIMARY subject
(APPEARS_IN_CASE.is_primary = true) is also the primary subject of c2.
Scoped to c1's primary subject only; co-subjects are not who c1 is about.

SCORE RANGE (with default 0.25 weights):
  0.25  — allegation type only
  0.50  — + any one bonus
  0.75  — + any two bonuses
  1.00  — all four dimensions matched

DETERMINISM (AI-14): aggregates use collect/count(DISTINCT), score is
derived from boolean flags, ORDER BY score DESC, case_id ASC gives a
total order on ties.  No LLM, no randomness.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from config.settings import (
    SIMILAR_CASES_DESCRIPTION_MIN_WORD_LENGTH,
    SIMILAR_CASES_MAX_TOTAL,
    SIMILAR_CASES_SCORE_BASE,
    SIMILAR_CASES_SCORE_DESCRIPTION,
    SIMILAR_CASES_SCORE_EMPLOYER_FEIN,
    SIMILAR_CASES_SCORE_FRAUD_NETWORK,
)
from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# All scoring weights and the description word-length threshold are passed
# as Cypher parameters from config/settings.py — zero numeric literals in
# the query itself.
#
# KEY DESIGN DECISION — Dimension 3 uses SHARES_EMPLOYER_WITH not EMPLOYED_BY:
#
# The naive approach (traverse Subject -EMPLOYED_BY-> Employer <-EMPLOYED_BY-
# Subject) requires both cases to have had their employer data ETL-synced into
# the graph.  In practice, candidate cases often have allegation nodes but no
# EMPLOYED_BY edges because they were synced before the employer ETL ran or
# were synced from a different pipeline path.
#
# Rule_01_Shared_Employer already solved this: it runs across the full
# reasoning scope of the active case (which the pipeline expands to include
# co-subjects, employer contacts, etc. from other cases) and writes
# SHARES_EMPLOYER_WITH edges directly between Subject nodes.  Those edges
# connect subjects from the active case to subjects from other cases — the
# exact cross-case link the similar-cases dimension needs.  Using them here
# means Dimension 3 fires whenever Rule_01 ran for the active case and found
# a match, without needing the candidate case to carry any ETL employer data.
# ---------------------------------------------------------------------------
_SIMILAR_CASES_QUERY = """
MATCH (c1:Case {case_id: $case_id})-[:HAS_ALLEGATION]->(a1:Allegation)
WITH c1,
     collect(DISTINCT toLower(a1.allegation_type)) AS c1_types,
     [d IN collect(DISTINCT toLower(coalesce(trim(a1.comment_text), '')))
      WHERE size(d) > 0] AS c1_descs
WHERE size(c1_types) > 0

MATCH (c2:Case)-[:HAS_ALLEGATION]->(a2:Allegation)
WHERE toString(c2.case_id) <> toString($case_id)
  AND toLower(a2.allegation_type) IN c1_types
  // Own-history exclusion: drop c2 when c1's primary subject is also
  // the primary subject of c2.
  AND NOT EXISTS {
        MATCH (c1)<-[ap1:APPEARS_IN_CASE]-(s:Subject)-[ap2:APPEARS_IN_CASE]->(c2)
        WHERE ap1.is_primary = true AND ap2.is_primary = true
      }

WITH c1, c2, c1_descs,
     collect(DISTINCT a2.allegation_type) AS shared_types,
     [d IN collect(DISTINCT toLower(coalesce(trim(a2.comment_text), '')))
      WHERE size(d) > 0] AS c2_descs

// Dimension 2 — Allegation description keyword overlap.
// Tokenise each c1 description on spaces; keep tokens longer than
// $desc_min_word_length characters (drops stop-words like "the", "with").
// No hardcoded threshold — $desc_min_word_length is a settings parameter.
WITH c1, c2, shared_types,
     any(c1d IN c1_descs WHERE
         any(word IN [w IN split(c1d, ' ') WHERE size(trim(w)) > $desc_min_word_length]
             WHERE any(c2d IN c2_descs WHERE c2d CONTAINS word))
     ) AS has_description

// Dimension 3 — Shared employer via SHARES_EMPLOYER_WITH.
// Uses Rule_01's already-computed edges: a subject from c1 is directly
// connected by SHARES_EMPLOYER_WITH to a subject from c2.  This fires
// regardless of whether the candidate case's employer data was ETL-synced,
// because Rule_01 writes these edges across the full reasoning scope of
// the active case (which the pipeline expands to include subjects from
// other cases).  The relationship is undirected (-) since Rule_01 creates
// it with a.subject_id < b.subject_id ordering but the match must find
// it from either direction.
OPTIONAL MATCH (c1)<-[:APPEARS_IN_CASE]-(s1:Subject)
               -[:SHARES_EMPLOYER_WITH]-
               (s2:Subject)-[:APPEARS_IN_CASE]->(c2)
WITH c1, c2, shared_types, has_description,
     count(DISTINCT s1) AS shared_employer_count

// Dimension 4 — Shared fraud network membership via MEMBER_OF_FRAUD_NETWORK.
// Uses Rule_02/04/06's written edges: a subject from c1 and a subject from c2
// both point to the same :FraudNetwork node.
OPTIONAL MATCH (c1)<-[:APPEARS_IN_CASE]-(:Subject)-[:MEMBER_OF_FRAUD_NETWORK]->(fn:FraudNetwork)
               <-[:MEMBER_OF_FRAUD_NETWORK]-(:Subject)-[:APPEARS_IN_CASE]->(c2)
WITH c2, shared_types, has_description,
     shared_employer_count,
     count(DISTINCT fn) AS shared_network_count

WITH c2, shared_types,
     has_description,
     (shared_employer_count > 0) AS has_employer,
     (shared_network_count  > 0) AS has_network

// Score: base (always present) + conditional bonuses.
// All coefficients are Cypher parameters — no numeric literals.
WITH c2, shared_types,
     $score_base
       + CASE WHEN has_description THEN $score_description  ELSE 0.0 END
       + CASE WHEN has_employer    THEN $score_employer_fein ELSE 0.0 END
       + CASE WHEN has_network     THEN $score_fraud_network ELSE 0.0 END AS similarity_score,
     [reason IN [
        "allegation_type",
        CASE WHEN has_description THEN "allegation_description" ELSE null END,
        CASE WHEN has_employer    THEN "shared_employer_fein"   ELSE null END,
        CASE WHEN has_network     THEN "shared_fraud_network"   ELSE null END
     ] WHERE reason IS NOT NULL] AS match_reasons

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
    Return structurally similar cases for `case_id`, scored 0.25–1.0
    across four dimensions (allegation type, description, employer via
    SHARES_EMPLOYER_WITH, fraud network via MEMBER_OF_FRAUD_NETWORK).

    All scoring weights and the description word-length threshold come
    from config/settings.py — nothing is hardcoded in the query.

    Args:
        case_id: the active case to find matches for.  Required, non-empty.
        limit:   maximum matches to return (ordered strongest-first).
            Defaults to config.settings.SIMILAR_CASES_MAX_TOTAL.

    Returns (inside the standard {result, provenance} envelope):
        {
          "matches": [
            { case_id, complaint_no, status, fraud_amount, date_opened,
              matched_allegation_types, similarity_score, match_reasons }
          ],
          "source": "structural_graph",
          "total_candidates_scored": int
        }

    Scoring (default weights, all from settings):
        0.25  — allegation type only  (SCORE_BASE)
        +0.25 — description keyword overlap (SCORE_DESCRIPTION)
        +0.25 — shared employer via SHARES_EMPLOYER_WITH (SCORE_EMPLOYER_FEIN)
        +0.25 — shared fraud network (SCORE_FRAUD_NETWORK)
        1.00  — all four match

    match_reasons vocabulary:
        "allegation_type"        — always present (entry requirement)
        "allegation_description" — description keyword overlap fired
        "shared_employer_fein"   — SHARES_EMPLOYER_WITH dimension fired
        "shared_fraud_network"   — MEMBER_OF_FRAUD_NETWORK dimension fired

    Raises:
        ValueError: on a missing/blank case_id.
        GraphUnavailableError / Neo4jError: propagated upstream.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("find_structural_matches requires a non-empty case_id")
    case_id = str(case_id).strip()

    with get_session() as session:
        rows = session.run(
            _SIMILAR_CASES_QUERY,
            case_id=case_id,
            # All numeric values come from settings — no literals in the query.
            score_base=float(SIMILAR_CASES_SCORE_BASE),
            score_description=float(SIMILAR_CASES_SCORE_DESCRIPTION),
            score_employer_fein=float(SIMILAR_CASES_SCORE_EMPLOYER_FEIN),
            score_fraud_network=float(SIMILAR_CASES_SCORE_FRAUD_NETWORK),
            desc_min_word_length=int(SIMILAR_CASES_DESCRIPTION_MIN_WORD_LENGTH),
        ).data()

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
        "find_structural_matches: case_id=%s candidates_scored=%d returned=%d "
        "weights=[base=%.2f desc=%.2f fein=%.2f network=%.2f] "
        "desc_min_word_length=%d",
        case_id,
        total_scored,
        len(matches),
        SIMILAR_CASES_SCORE_BASE,
        SIMILAR_CASES_SCORE_DESCRIPTION,
        SIMILAR_CASES_SCORE_EMPLOYER_FEIN,
        SIMILAR_CASES_SCORE_FRAUD_NETWORK,
        SIMILAR_CASES_DESCRIPTION_MIN_WORD_LENGTH,
    )

    return {
        "result": {
            "matches": matches,
            "source": "structural_graph",
            "total_candidates_scored": total_scored,
        },
        "provenance": graph_provenance("reasoning_layer.similar_cases"),
    }
