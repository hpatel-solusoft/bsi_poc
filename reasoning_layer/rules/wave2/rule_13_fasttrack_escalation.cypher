// Rule 13: FastTrack Escalation Recommendation — Wave 2.
//
// Trigger : Case fraud amount above threshold (registry parameter,
//           default $50,000) AND the primary subject has
//           HAS_PRIOR_GUILTY_CASE (Rule 7) AND the case is not already
//           FastTracked -> FastTrack recommendation.
// Writes  : properties on :Case. It is a RECOMMENDATION, never a mutation
//           of :Case.is_fasttrack — that field is AppWorks' asserted fact
//           and Neo4j is not its system of record (Principle 11). The rule
//           writes fasttrack_recommended; a human, in AppWorks, decides
//           whether is_fasttrack becomes true. Overwriting is_fasttrack
//           here would make the graph lie about what AppWorks says.
//
// Depends on Rule 7's output, which is why it is Wave 2 despite reading as
// a simple threshold check.
//
// Scoped to the PRIMARY subject specifically ($subject_id, not
// $scope_subject_ids): the rule says "Primary Subject has a prior guilty
// case". A co-subject's record is not grounds to fast-track the case.

MATCH (c:Case {case_id: $case_id})
MATCH (a:Subject {subject_id: $subject_id})-[pg:HAS_PRIOR_GUILTY_CASE]->(:Case)
WHERE pg.status = "active"
  AND c.fraud_amount IS NOT NULL
  AND c.fraud_amount > $fasttrack_fraud_threshold
  AND coalesce(c.is_fasttrack, false) = false
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "FASTTRACK_RECOMMENDATION", status: "active"})
        WHERE rej.from_key = a.subject_id AND rej.to_key = c.case_id
      }
WITH c, a, count(pg) AS prior_guilty_count
SET c.fasttrack_recommended            = true,
    c.fasttrack_reason                 = "Fraud amount above threshold and primary subject has a prior guilty case",
    c.fasttrack_threshold_applied      = $fasttrack_fraud_threshold,
    c.fasttrack_prior_guilty_count     = prior_guilty_count,
    c.fasttrack_recommendation_rule    = "Rule_13_FastTrack_Escalation",
    c.fasttrack_recommendation_confidence = "High",
    c.fasttrack_recommendation_asserted_at = $asserted_at,
    c.fasttrack_recommendation_status  = "active"
RETURN count(DISTINCT c) AS writes
