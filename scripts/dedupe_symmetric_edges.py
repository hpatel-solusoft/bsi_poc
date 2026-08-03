"""
One-time maintenance: collapse duplicate physical relationships that a
concurrent pipeline run may have created for the same logical fact,
before core.pipeline_state_repository.pipeline_run_lock closed the
race that causes this (see that function's docstring for the full
explanation).

ROOT CAUSE, established across two rounds of investigation: this was
first diagnosed as MERGE-on-an-undirected-relationship-pattern being
unreliable (true, and fixed — see reasoning_layer/rules/wave1/
rule_01_shared_employer.cypher's "DIRECTED MERGE, UNDIRECTED READS"
comment) but that theory didn't explain a duplicate later found on
Rule_07_Prior_Guilty, whose write query MERGEs a DIRECTED relationship
with no undirected pattern anywhere in it. The actual, general-purpose
cause: Neo4j's MERGE find-or-create check is only atomic WITHIN one
transaction — two pipeline runs for the same (case_id, subject_id)
executing concurrently can each see "doesn't exist yet" before either
commits, and each create one. This affects every relationship a rule
writes, not just the three symmetric-edge ones, which is why this
script (unlike its predecessor) covers all of them.

This is a data-repair script, not application code — nothing in the
running app imports it. reasoning_layer/rules_fired.py's own
_dedupe_rows() already hides any existing duplicate from every API
response, so running this script is NOT required for the app to behave
correctly, and core.pipeline_state_repository.pipeline_run_lock now
prevents new ones from forming. Run this when convenient to remove the
duplicates from the graph itself — e.g. so a raw Cypher query against
the database (outside this app) doesn't see them either.

SAFE BY DESIGN:
  * Read-only dry run by default — prints what it WOULD delete and does
    nothing else. Pass --apply to actually delete.
  * Only ever deletes a relationship — never a node, and never a
    :Rejection node.
  * The relationship KEPT is deterministic: the one Neo4j created
    first (ordered by first_asserted_at, falling back to elementId as
    a stable tiebreaker for the rare case two duplicates share a
    timestamp) — i.e. whichever one is more likely to be the one an
    investigator has already seen and possibly acted on (rejected,
    corroborated, etc.), so a manual audit trail on the "real" instance
    is never the one silently dropped.
  * Idempotent: running it again after a clean graph finds nothing to
    do.

USAGE:
    python scripts/dedupe_duplicate_relationships.py                  # dry run, whole graph
    python scripts/dedupe_duplicate_relationships.py --apply           # whole graph
    python scripts/dedupe_duplicate_relationships.py --apply --case-id 658407433
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from reasoning_layer.neo4j_client import get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dedupe_duplicate_relationships")


@dataclass(frozen=True)
class _RelSpec:
    rel_type: str
    source_rule: str
    # "symmetric": written via an undirected MERGE pattern historically
    # (see rule_01/03/05's fix) — read as undirected, deduped on the
    # UNORDERED endpoint pair.
    # "directed": written via a directed MERGE — read as directed,
    # deduped on the ORDERED (source, target) pair.
    family: str


# Every relationship type a rule's write .cypher actually MERGEs.
# Deliberately excludes the case-flag/subject-flag/allegation-flag
# families (Rules 08, 11, 12, 13) — those SET properties on an existing
# node rather than creating a relationship, so there is no relationship
# for this script to deduplicate; a concurrent double-write there is a
# lost-update risk, not a duplicate-instance display bug, and is out of
# this script's scope.
_REL_SPECS = (
    _RelSpec("SHARES_EMPLOYER_WITH", "Rule_01_Shared_Employer", "symmetric"),
    _RelSpec("SHARES_ADDRESS_WITH", "Rule_03_Shared_Address", "symmetric"),
    _RelSpec("SHARES_ALIAS_PATTERN_WITH", "Rule_05_Alias_Identity", "symmetric"),
    _RelSpec("HAS_PRIOR_GUILTY_CASE", "Rule_07_Prior_Guilty", "directed"),
    # APPEARS_IN_CASE is mixed-provenance (Section 3.2) — ETL-asserted
    # edges have no source_rule and are never touched here; only the
    # rule-derived ones (source_rule = "Rule_10_...") are in scope.
    _RelSpec("APPEARS_IN_CASE", "Rule_10_Merged_Case_Propagation", "directed"),
    _RelSpec("MEMBER_OF_FRAUD_NETWORK", "Rule_02_Employer_Fraud_Network", "directed"),
    _RelSpec("MEMBER_OF_FRAUD_NETWORK", "Rule_04_Address_Fraud_Network", "directed"),
    _RelSpec("MEMBER_OF_FRAUD_NETWORK", "Rule_06_Identity_Fraud_Network", "directed"),
    _RelSpec("MEMBER_OF_FRAUD_NETWORK", "Rule_09_PCA_CheckSplit", "directed"),
)

_FIND_DUPLICATES_SYMMETRIC = """
MATCH (a)-[r:{rel_type}]-(b)
WHERE r.source_rule = $rule_id
  AND elementId(a) < elementId(b)
  {case_filter}
WITH elementId(a) AS a_id, elementId(b) AS b_id,
     coalesce(a.subject_id, a.case_id) AS a_key,
     coalesce(b.subject_id, b.case_id) AS b_key,
     collect(r) AS rels
WHERE size(rels) > 1
RETURN a_key, b_key,
       [rel IN rels | {{id: elementId(rel), first_asserted_at: rel.first_asserted_at}}] AS rel_infos
ORDER BY a_key, b_key
"""

_FIND_DUPLICATES_DIRECTED = """
MATCH (a)-[r:{rel_type}]->(b)
WHERE r.source_rule = $rule_id
  {case_filter}
WITH elementId(a) AS a_id, elementId(b) AS b_id,
     coalesce(a.subject_id, a.case_id) AS a_key,
     coalesce(b.subject_id, b.case_id, b.network_key) AS b_key,
     collect(r) AS rels
WHERE size(rels) > 1
RETURN a_key, b_key,
       [rel IN rels | {{id: elementId(rel), first_asserted_at: rel.first_asserted_at}}] AS rel_infos
ORDER BY a_key, b_key
"""

_DELETE_RELATIONSHIP_QUERY = """
MATCH ()-[r]-()
WHERE elementId(r) = $rel_id
DELETE r
"""

_CASE_FILTER = (
    "AND (EXISTS {{ MATCH (a)-[:APPEARS_IN_CASE]->(:Case {{case_id: $case_id}}) }} "
    "OR EXISTS {{ MATCH (b)-[:APPEARS_IN_CASE]->(:Case {{case_id: $case_id}}) }})"
)


def _sort_key(rel_info: dict) -> tuple:
    # first_asserted_at is an ISO 8601 string (or None) — sorts correctly
    # as text; a missing timestamp sorts last (never preferred as "keep").
    return (rel_info.get("first_asserted_at") is None, rel_info.get("first_asserted_at") or "", rel_info["id"])


def find_and_dedupe(apply: bool, case_id: str | None) -> int:
    """Returns the number of duplicate relationships removed (or that
    WOULD be removed, in dry-run mode)."""
    case_filter = _CASE_FILTER if case_id else ""
    total_removed = 0

    with get_session() as session:
        for spec in _REL_SPECS:
            template = _FIND_DUPLICATES_SYMMETRIC if spec.family == "symmetric" else _FIND_DUPLICATES_DIRECTED
            query = template.format(rel_type=spec.rel_type, case_filter=case_filter)
            params = {"rule_id": spec.source_rule}
            if case_id:
                params["case_id"] = case_id

            groups = session.run(query, **params).data()
            if not groups:
                logger.info("%s (%s): no duplicates found", spec.rel_type, spec.source_rule)
                continue

            for group in groups:
                rel_infos = sorted(group["rel_infos"], key=_sort_key)
                keep, *extras = rel_infos
                logger.info(
                    "%s (%s): %s <-> %s has %d duplicate relationships — keeping %s "
                    "(first_asserted_at=%s), %s %d",
                    spec.rel_type,
                    spec.source_rule,
                    group["a_key"],
                    group["b_key"],
                    len(rel_infos),
                    keep["id"],
                    keep.get("first_asserted_at"),
                    "would remove" if not apply else "removing",
                    len(extras),
                )
                for rel_info in extras:
                    if apply:
                        session.run(_DELETE_RELATIONSHIP_QUERY, rel_id=rel_info["id"])
                    total_removed += 1

    return total_removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete duplicate relationships. Without this flag, only reports what would happen.",
    )
    parser.add_argument(
        "--case-id",
        default=None,
        help="Limit to relationships touching this case instead of scanning the whole graph.",
    )
    args = parser.parse_args()

    if not args.apply:
        logger.info("DRY RUN — pass --apply to actually delete anything")

    removed = find_and_dedupe(apply=args.apply, case_id=args.case_id)

    if args.apply:
        logger.info("Done — removed %d duplicate relationship(s)", removed)
    else:
        logger.info("Done — would remove %d duplicate relationship(s) (dry run, nothing changed)", removed)

    return 0


if __name__ == "__main__":
    sys.exit(main())