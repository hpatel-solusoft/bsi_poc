"""
Owns: resolving the reasoning scope for one pipeline run — the set of
subject_ids a rule is allowed to match on.

Section 5.2 defines the scope precisely: "Primary Subject of the current
case. Co-Subjects are included where inference rules cross-reference
them: one hop out via IS_CO_SUBJECT_WITH and anyone matched on
Employer/Address/Alias, not the whole database."

Every Wave 1 and Wave 2 rule in the previous round ignored that and
matched across the ENTIRE graph. At POC seed-data scale that is merely
wasteful. At production scale it is a full graph scan on every case
open, and worse, it writes inferred relationships between subjects who
have nothing to do with the case being opened — attributing rule output
to a run that never looked at those subjects. This module is what makes
the documented scope actually enforced rather than aspirational.

Does NOT own: rule execution (reasoning_layer/rule_engine.py), rule
content (rules/*.cypher), or the pipeline sequence (pipeline.py).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

# One hop out from the primary subject, along exactly the four edges
# Section 5.2 names: co-subject membership, shared employer, shared
# address, shared alias. The primary subject is always in scope even
# when they are connected to nobody.
_SCOPE_QUERY = """
MATCH (primary:Subject {subject_id: $subject_id})
OPTIONAL MATCH (primary)-[:IS_CO_SUBJECT_WITH]-(co:Subject)
OPTIONAL MATCH (primary)-[:EMPLOYED_BY|HAS_WAGE_RECORD_WITH]->(:Employer)
                <-[:EMPLOYED_BY|HAS_WAGE_RECORD_WITH]-(emp:Subject)
OPTIONAL MATCH (primary)-[:HAS_ADDRESS]->(:Address)<-[:HAS_ADDRESS]-(addr:Subject)
OPTIONAL MATCH (primary)-[:HAS_ALIAS]->(:Alias)<-[:HAS_ALIAS]-(alias:Subject)
WITH primary,
     collect(DISTINCT co)    AS co_subjects,
     collect(DISTINCT emp)   AS employer_linked,
     collect(DISTINCT addr)  AS address_linked,
     collect(DISTINCT alias) AS alias_linked
WITH [primary] + co_subjects + employer_linked + address_linked + alias_linked AS all_subjects,
     size(co_subjects)    AS co_count,
     size(employer_linked) AS employer_count,
     size(address_linked)  AS address_count,
     size(alias_linked)    AS alias_count
UNWIND all_subjects AS s
WITH DISTINCT s, co_count, employer_count, address_count, alias_count
RETURN collect(s.subject_id) AS scope_subject_ids,
       co_count, employer_count, address_count, alias_count
"""

# Every case any in-scope subject appears in. Rules 7, 8, 10 and 13
# reason about cases, not only subjects, and a prior guilty case is by
# definition not the case currently being opened — so the case scope is
# derived from the subject scope rather than being just [case_id].
_CASE_SCOPE_QUERY = """
UNWIND $scope_subject_ids AS sid
MATCH (s:Subject {subject_id: sid})-[:APPEARS_IN_CASE]->(c:Case)
RETURN collect(DISTINCT c.case_id) AS scope_case_ids
"""


def resolve_scope(case_id: str, subject_id: str) -> Dict[str, Any]:
    """
    Returns:
        {
          "case_id": ..., "primary_subject_id": ...,
          "scope_subject_ids": [...],   # primary + one hop
          "scope_case_ids":    [...],   # every case those subjects touch
          "expansion": {co_subject: n, employer: n, address: n, alias: n},
        }

    Raises GraphUnavailableError / Neo4jError on a database problem —
    the caller (pipeline.py) treats that as a Principle 15 failure, the
    same as a rule failing, because a run with an unresolved scope would
    otherwise silently fall back to "no scope" = "whole graph", which is
    exactly the failure mode this module exists to prevent.
    """
    with get_session() as session:
        record = session.run(_SCOPE_QUERY, subject_id=subject_id).single()
        if record is None or not record["scope_subject_ids"]:
            # The subject is not in the graph at all. That is not an error —
            # it means ETL has not loaded this case yet (or loaded it without
            # this subject). Return a scope of exactly the primary subject so
            # every rule matches nothing, rather than matching everything.
            logger.warning(
                "scope: subject_id=%s not found in graph — rules will match nothing "
                "for case_id=%s. Has ETL run for this case?", subject_id, case_id,
            )
            return {
                "case_id": case_id, "primary_subject_id": subject_id,
                "scope_subject_ids": [subject_id], "scope_case_ids": [case_id],
                "expansion": {"co_subject": 0, "employer": 0, "address": 0, "alias": 0},
                "subject_in_graph": False,
            }

        scope_subject_ids: List[str] = [sid for sid in record["scope_subject_ids"] if sid]
        case_record = session.run(_CASE_SCOPE_QUERY, scope_subject_ids=scope_subject_ids).single()
        scope_case_ids = list(case_record["scope_case_ids"]) if case_record else [case_id]
        if case_id not in scope_case_ids:
            scope_case_ids.append(case_id)

    scope = {
        "case_id": case_id,
        "primary_subject_id": subject_id,
        "scope_subject_ids": scope_subject_ids,
        "scope_case_ids": scope_case_ids,
        "expansion": {
            "co_subject": record["co_count"],
            "employer": record["employer_count"],
            "address": record["address_count"],
            "alias": record["alias_count"],
        },
        "subject_in_graph": True,
    }
    logger.info(
        "scope: case_id=%s subject_id=%s subjects_in_scope=%d cases_in_scope=%d expansion=%s",
        case_id, subject_id, len(scope_subject_ids), len(scope_case_ids), scope["expansion"],
    )
    return scope
