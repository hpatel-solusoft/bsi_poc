"""
Owns: reading, out of Neo4j, everything the Extraction Stage needs to
reason over for one subject — the narrative text, and the structural
relationships that narrative might independently confirm.

Does not call an LLM (that's extraction_stage.py) and writes nothing
(that's graph_load.py). This is the input-gathering half of Step 3
(Section 5.3), kept separate so the Cypher read and the LLM call can each
be tested on their own.

Zero AppWorks calls, by design: Section 5.3 Step 3 says the Extraction
Stage reads ":Commentary nodes already loaded by ETL rather than a fresh
AppWorks fetch", and Section 5.2 gives the whole pipeline an AppWorks
dependency of "None".

TWO THINGS ARE RETURNED, NOT ONE:

  1. allegations  — every allegation across the subject's full case
     history, each with the narrative text that might attribute it.
     Narrative comes from all THREE sources Section 5.3 names —
     Commentary, Subject_Comment, and the Allegation comment field —
     not just case commentary, which was the previous round's gap.

  2. structural_relationships — the SHARES_EMPLOYER_WITH /
     SHARES_ADDRESS_WITH / SHARES_ALIAS_PATTERN_WITH edges Wave 1 has
     just written, each with a stable reference. Rule 14 elevates a
     structural relationship that the narrative independently confirms,
     and it can only do that if the Extraction Stage was actually SHOWN
     the relationships it might confirm. Without this, Rule 14 has an
     input that nothing ever populates, and quietly never fires.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

# One row per (case, allegation) across every case the subject appears in.
# Case-level commentary is not allegation-scoped in the schema, so all of a
# case's commentary is offered as context for each allegation on that case
# and the LLM decides relevance — rather than this query inventing a
# narrower join the schema does not support.
_NARRATIVE_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})-[:APPEARS_IN_CASE]->(c:Case)
MATCH (c)-[:HAS_ALLEGATION]->(al:Allegation)
OPTIONAL MATCH (c)-[:HAS_COMMENTARY]->(case_comm:Commentary)
OPTIONAL MATCH (al)-[:HAS_COMMENTARY]->(alleg_comm:Commentary)
OPTIONAL MATCH (subj:Subject)-[:APPEARS_IN_CASE]->(c)
OPTIONAL MATCH (subj)-[:HAS_COMMENTARY]->(subj_comm:Commentary)
WITH c, al,
     collect(DISTINCT case_comm)  AS case_comments,
     collect(DISTINCT alleg_comm) AS alleg_comments,
     collect(DISTINCT subj_comm)  AS subject_comments,
     collect(DISTINCT {subject_id: subj.subject_id,
                       name: coalesce(subj.company_name,
                                      trim(coalesce(subj.first_name, "") + " " +
                                           coalesce(subj.last_name, "")))}) AS case_subjects
WITH c, al, case_subjects,
     [x IN case_comments + alleg_comments + subject_comments
      WHERE x IS NOT NULL AND x.comment_text IS NOT NULL] AS comments
RETURN
    c.case_id          AS case_id,
    c.status           AS case_status,
    al.allegation_id   AS allegation_id,
    al.allegation_type AS allegation_type,
    al.status          AS allegation_status,
    // Every subject on the case, so the LLM can attribute an allegation to a
    // co-subject rather than being implicitly forced onto the subject whose
    // pipeline run this is. Section 6.1's Rule 9 depends on exactly that
    // (an allegation attributed to the PCA, not the consumer, or vice versa).
    [x IN case_subjects WHERE x.subject_id IS NOT NULL] AS case_subjects,
    [x IN comments | {
        comment_ref:  x.comment_id,
        comment_text: x.comment_text,
        comment_type: x.comment_type,
        created_date: x.created_date
    }] AS commentary
ORDER BY c.case_id, al.allegation_id
"""

# The structural relationships Wave 1 just asserted, offered to the
# Extraction Stage as confirmable candidates. elementId(r) is the
# reference — the same mechanism Section 6.2's Rule 14 worked example uses
# (comm.confirms_relationship_id = elementId(r)).
_STRUCTURAL_QUERY = """
MATCH (a:Subject {subject_id: $subject_id})
      -[r:SHARES_EMPLOYER_WITH|SHARES_ADDRESS_WITH|SHARES_ALIAS_PATTERN_WITH]-(b:Subject)
WHERE r.status = "active"
RETURN elementId(r)     AS relationship_ref,
       type(r)          AS relationship_type,
       a.subject_id     AS subject_id_a,
       b.subject_id     AS subject_id_b,
       coalesce(b.company_name,
                trim(coalesce(b.first_name, "") + " " + coalesce(b.last_name, ""))) AS other_subject_name,
       r.confidence     AS current_confidence
ORDER BY relationship_type, subject_id_b
"""


def get_narrative_records(subject_id: str) -> Dict[str, Any]:
    """
    Returns:
        {
          "subject_id": ...,
          "allegations": [
            {case_id, case_status, allegation_id, allegation_type,
             allegation_status, case_subjects: [{subject_id, name}],
             commentary: [{comment_ref, comment_text, comment_type, created_date}]}
          ],
          "structural_relationships": [
            {relationship_ref, relationship_type, subject_id_a, subject_id_b,
             other_subject_name, current_confidence}
          ],
        }

    Raises GraphUnavailableError / Neo4jError on a database problem;
    pipeline.py applies Principle 15's failure handling to both.
    """
    with get_session() as session:
        allegations = session.run(_NARRATIVE_QUERY, subject_id=subject_id).data()
        structural = session.run(_STRUCTURAL_QUERY, subject_id=subject_id).data()

    comment_count = sum(len(row.get("commentary") or []) for row in allegations)
    logger.info(
        "commentary_reader: subject_id=%s allegations=%d comments=%d structural_relationships=%d",
        subject_id, len(allegations), comment_count, len(structural),
    )
    return {
        "subject_id": subject_id,
        "allegations": allegations,
        "structural_relationships": structural,
    }
