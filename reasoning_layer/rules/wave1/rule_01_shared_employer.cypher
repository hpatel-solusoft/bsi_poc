// Rule 1: Shared Employer Detection — Wave 1 (structural, no LLM).
//
// Trigger  : Subjects A, B employed by the same :Employer, A != B.
// Writes   : (A)-[:SHARES_EMPLOYER_WITH]-(B), symmetric.
// Confidence: High when the employer is matched on FEIN, Medium when it
//             is matched on name only (Section 6.1).
//
// SCOPE: Section 5.2 limits every rule to the primary subject plus one
// hop. $scope_subject_ids is resolved by reasoning_layer/scope.py. At
// least one side of the pair must be in scope — otherwise this rule
// would assert relationships between subjects who have nothing to do
// with the case being opened.
//
// REJECTION GUARD (Section 5.5): "every future pipeline run checks for
// an existing rejection before re-asserting the same fact." The guard
// filters the pair out BEFORE the MERGE, so a rejected relationship
// keeps its status:"rejected" and is never quietly flipped back to
// active by a re-run.

MATCH (a:Subject)-[:EMPLOYED_BY]->(e:Employer)<-[:EMPLOYED_BY]-(b:Subject)
WHERE a.subject_id < b.subject_id
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "SHARES_EMPLOYER_WITH", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key   IN [a.subject_id, b.subject_id]
      }
MERGE (a)-[r:SHARES_EMPLOYER_WITH]-(b)
ON CREATE SET r.first_asserted_at = $asserted_at
SET r.confidence   = CASE WHEN e.fein IS NOT NULL THEN "High" ELSE "Medium" END,
    r.match_basis  = CASE WHEN e.fein IS NOT NULL THEN "fein" ELSE "employer_name" END,
    r.employer_key = e.employer_key,
    r.source_rule  = "Rule_01_Shared_Employer",
    r.asserted_at  = $asserted_at,
    r.status       = coalesce(r.status, "active"),
    r.corroborated = coalesce(r.corroborated, false)
RETURN count(DISTINCT r) AS writes
