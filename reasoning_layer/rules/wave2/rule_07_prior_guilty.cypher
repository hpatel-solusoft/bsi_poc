// Rule 7: Prior Guilty Subject Identification — Wave 2.
//
// Trigger : Subject A appears in a CLOSED case C carrying a Guilty-outcome
//           allegation ATTRIBUTABLE TO A -> (A)-[:HAS_PRIOR_GUILTY_CASE]->(C).
// Confidence: High, attribution-dependent (Section 6.1).
//
// WHY THIS IS WAVE 2, NOT WAVE 1 (the reference doc corrects itself on this
// and it is worth restating): a guilty verdict on a case A merely appears in
// is not a guilty verdict against A. Two co-subjects, one conviction — only
// attribution tells you whose. Without ALLEGATION_LIKELY_AGAINST_SUBJECT this
// rule would brand every co-subject of a convicted person a recidivist, and
// Rule 8 and Rule 13 would then escalate on that. It reads structural at a
// glance; it is not.
//
// The current case is excluded ($case_id): a case is not its own prior.
// Outcome and closed-status vocabularies come from the rule registry.

MATCH (a:Subject)-[:APPEARS_IN_CASE]->(c:Case)-[:HAS_ALLEGATION]->(al:Allegation)
MATCH (al)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a)
WHERE a.subject_id IN $scope_subject_ids
  AND c.case_id <> $case_id
  AND att.status = "active"
  AND toLower(coalesce(c.status, "")) IN $closed_case_statuses
  AND (
        any(v IN $guilty_outcome_values WHERE toLower(coalesce(al.outcome, "")) CONTAINS v)
     OR any(v IN $guilty_outcome_values WHERE toLower(coalesce(al.status, "")) CONTAINS v)
     OR any(v IN $guilty_outcome_values WHERE toLower(coalesce(c.disposition, "")) CONTAINS v)
      )
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "HAS_PRIOR_GUILTY_CASE", status: "active"})
        WHERE rej.from_key = a.subject_id AND rej.to_key = c.case_id
      }
MERGE (a)-[r:HAS_PRIOR_GUILTY_CASE]->(c)
ON CREATE SET r.first_asserted_at = $asserted_at
SET r.confidence     = CASE WHEN att.confidence = "Unresolved" THEN "Unresolved"
                            WHEN att.confidence = "High" THEN "High"
                            ELSE "Medium" END,
    r.allegation_id  = al.allegation_id,
    r.outcome        = coalesce(al.outcome, al.status, c.disposition),
    r.date_closed    = c.closed_date,
    r.source_rule    = "Rule_07_Prior_Guilty",
    r.asserted_at    = $asserted_at,
    r.status         = coalesce(r.status, "active"),
    r.corroborated   = coalesce(r.corroborated, false)
RETURN count(DISTINCT r) AS writes
