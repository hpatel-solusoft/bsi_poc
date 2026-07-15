// Rule 11: Cross-Case Co-Subject Network Detection — Wave 1.
//
// Trigger : A IS_CO_SUBJECT_WITH B in case C1, and A IS_CO_SUBJECT_WITH
//           C in a different case C2 (B != C, C1 != C2)
//           -> flag A as a cross-case network hub.
// Confidence: Medium.
// Writes  : properties on the :Subject node itself (is_cross_case,
//           hub_case_ids — both already declared Subject key properties
//           in Section 3.1), not a new relationship type. "Flag A as a
//           hub" is a property assertion about A, not a connection
//           between two other things.
//
// THIS IS NOW THE LITERAL TRIGGER, not the previous round's
// approximation. The earlier version asked "does A's case membership
// span more than one case", which fires for any subject on two cases
// even with no co-subject at all — a materially different (and much
// looser) question than the one the rule specifies. This version matches
// two distinct co-subjects (b <> c) across two distinct cases
// (c1 <> c2), exactly as written, with no APOC dependency: the pairing
// is expressed by carrying the case id on the IS_CO_SUBJECT_WITH
// relationship (etl/graph_sync.py writes r.case_id), which removes the
// need for the set-difference the earlier note worried about.

// REJECTION GUARD (Section 5.5): the cross-case hub flag is an inferred
// fact an investigator can reject like any other — "this is two different
// people with the same name, not one hub". A rejection here is keyed on the
// subject alone, since the flag is a property of the subject rather than a
// relationship between two things. Without this guard, a rejected hub flag
// would be silently re-asserted on the next pipeline run, which is exactly
// the behaviour Principle 14 forbids.
MATCH (a:Subject)-[r1:IS_CO_SUBJECT_WITH]-(b:Subject),
      (a)-[r2:IS_CO_SUBJECT_WITH]-(c:Subject)
WHERE a.subject_id IN $scope_subject_ids
  AND b.subject_id <> c.subject_id
  AND r1.case_id IS NOT NULL AND r2.case_id IS NOT NULL
  AND r1.case_id <> r2.case_id
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "CROSS_CASE_HUB", status: "active"})
        WHERE rej.from_key = a.subject_id
      }
WITH a, collect(DISTINCT r1.case_id) + collect(DISTINCT r2.case_id) AS raw_case_ids
UNWIND raw_case_ids AS cid
WITH a, collect(DISTINCT cid) AS hub_case_ids
SET a.is_cross_case           = true,
    a.hub_case_ids            = hub_case_ids,
    a.cross_case_source_rule  = "Rule_11_Cross_Case_Hub",
    a.cross_case_confidence   = "Medium",
    a.cross_case_asserted_at  = $asserted_at
RETURN count(DISTINCT a) AS writes
