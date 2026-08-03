"""
One-time maintenance: collapse duplicate physical relationships that
MERGE on an undirected pattern (`MERGE (a)-[r:TYPE]-(b)`) may have
created for the same logical fact, before
reasoning_layer/rules/wave1/rule_0{1,3,5}_*.cypher were fixed to MERGE
in a fixed direction instead.

This is a data-repair script, not application code — nothing in the
running app imports it. reasoning_layer/rules_fired.py's own
_dedupe_rows() already hides any existing duplicate from every API
response, so running this script is NOT required for the app to behave
correctly. Run it when convenient to remove the duplicate relationships
from the graph itself — e.g. so a raw Cypher query against the database
(outside this app) doesn't see them, and so
reasoning_layer.rejection.reject_inference/revert_rejection's
rejected_count/reverted_count reports 1 instead of 2 when both
duplicates happen to satisfy the same target filter.

SAFE BY DESIGN:
  * Read-only dry run by default — prints what it WOULD delete and does
    nothing else. Pass --apply to actually delete.
  * Only ever deletes a relationship — never a :Subject, :Employer,
    :Address, or :Alias node, and never a :Rejection node (only the
    keep-one's audit trail matters; a lost duplicate's fields, if they
    ever differed, are not the ones the UI already reads).
  * The relationship KEPT is deterministic: whichever one
    reasoning_layer/rules_fired.py's own read queries would return
    first (ORDER BY subject_id, related_subject_id — ties broken by
    Neo4j's internal relationship id, oldest first), so this always
    keeps the same one the API has already been showing.
  * Idempotent: running it again after a clean graph finds nothing to
    do.

USAGE:
    python scripts/dedupe_symmetric_edges.py                 # dry run
    python scripts/dedupe_symmetric_edges.py --apply          # all cases
    python scripts/dedupe_symmetric_edges.py --apply --case-id 658407433
"""

from __future__ import annotations

import argparse
import logging
import sys

from reasoning_layer.neo4j_client import get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dedupe_symmetric_edges")

# The three symmetric-edge relationship types affected by the undirected
# MERGE anti-pattern (see reasoning_layer/rules/wave1/rule_01_shared_employer
# .cypher's "DIRECTED MERGE, UNDIRECTED READS" comment for the full
# explanation). MEMBER_OF_FRAUD_NETWORK, HAS_PRIOR_GUILTY_CASE, etc. are
# directed relationships (Subject->Case, Subject->FraudNetwork) and were
# never at risk of this — only these three ever used an undirected MERGE.
_RELATIONSHIP_TYPES = (
    "SHARES_EMPLOYER_WITH",
    "SHARES_ADDRESS_WITH",
    "SHARES_ALIAS_PATTERN_WITH",
)

_FIND_DUPLICATES_QUERY = """
MATCH (a:Subject)-[r:{rel_type}]-(b:Subject)
WHERE a.subject_id < b.subject_id
  AND r.source_rule = $rule_id
  {case_filter}
WITH a.subject_id AS subject_id_a, b.subject_id AS subject_id_b,
     collect(r) AS rels
WHERE size(rels) > 1
RETURN subject_id_a, subject_id_b,
       [rel IN rels | elementId(rel)] AS rel_ids
ORDER BY subject_id_a, subject_id_b
"""

_DELETE_RELATIONSHIP_QUERY = """
MATCH ()-[r]-()
WHERE elementId(r) = $rel_id
DELETE r
"""

_RULE_ID_BY_TYPE = {
    "SHARES_EMPLOYER_WITH": "Rule_01_Shared_Employer",
    "SHARES_ADDRESS_WITH": "Rule_03_Shared_Address",
    "SHARES_ALIAS_PATTERN_WITH": "Rule_05_Alias_Identity",
}


def find_and_dedupe(apply: bool, case_id: str | None) -> int:
    """
    Returns the number of duplicate relationships removed (or that WOULD
    be removed, in dry-run mode).
    """
    case_filter = (
        "AND (EXISTS { MATCH (a)-[:APPEARS_IN_CASE]->(:Case {case_id: $case_id}) } "
        "OR EXISTS { MATCH (b)-[:APPEARS_IN_CASE]->(:Case {case_id: $case_id}) })"
        if case_id
        else ""
    )
    total_removed = 0

    with get_session() as session:
        for rel_type in _RELATIONSHIP_TYPES:
            rule_id = _RULE_ID_BY_TYPE[rel_type]
            query = _FIND_DUPLICATES_QUERY.format(rel_type=rel_type, case_filter=case_filter)
            params = {"rule_id": rule_id}
            if case_id:
                params["case_id"] = case_id

            groups = session.run(query, **params).data()
            if not groups:
                logger.info("%s: no duplicates found", rel_type)
                continue

            for group in groups:
                subject_id_a = group["subject_id_a"]
                subject_id_b = group["subject_id_b"]
                rel_ids = group["rel_ids"]
                keep, *extras = rel_ids  # relationship ids in creation order
                logger.info(
                    "%s: %s <-> %s has %d duplicate relationships — keeping %s, %s %d",
                    rel_type,
                    subject_id_a,
                    subject_id_b,
                    len(rel_ids),
                    keep,
                    "would remove" if not apply else "removing",
                    len(extras),
                )
                for rel_id in extras:
                    if apply:
                        session.run(_DELETE_RELATIONSHIP_QUERY, rel_id=rel_id)
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
        help="Limit to one case's primary subject's relationships instead of the whole graph.",
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
