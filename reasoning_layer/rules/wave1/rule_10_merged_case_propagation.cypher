// Rule 10: Merged Case Subject History Propagation — Wave 1.
//
// Trigger : Case C1 MERGED_INTO_CASE C2, and Subject A APPEARS_IN_CASE C1
//           -> assert A also APPEARS_IN_CASE C2, annotated merge-derived.
// Confidence: High.
//
// MIXED PROVENANCE ON ONE RELATIONSHIP TYPE — FLAGGED, NOT SILENT:
// Section 3.2 lists APPEARS_IN_CASE as an *asserted* type (sourced from
// the Subjects bridge table, carrying source_table/retrieved_at). This
// rule writes a new edge of that same type which is *inferred*. Two
// provenance shapes therefore coexist on one relationship type, and a
// consumer must be able to tell them apart:
//
//   ETL-asserted  : has source_table, has NO source_rule
//   Rule-derived  : has source_rule = "Rule_10_...", merge_derived = true
//
// ON CREATE SET (not SET) is what protects the asserted edges: if A was
// already directly on C2 per AppWorks, this rule leaves that edge exactly
// as ETL wrote it and does not stamp rule provenance onto a fact AppWorks
// asserted. Worth confirming with whoever owns the reference doc before
// Wave 1 sign-off.

MATCH (c1:Case)-[:MERGED_INTO_CASE]->(c2:Case),
      (a:Subject)-[:APPEARS_IN_CASE]->(c1)
WHERE a.subject_id IN $scope_subject_ids
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "APPEARS_IN_CASE", status: "active"})
        WHERE rej.from_key = a.subject_id AND rej.to_key = c2.case_id
      }
MERGE (a)-[r:APPEARS_IN_CASE]->(c2)
ON CREATE SET r.merge_derived      = true,
              r.merged_from_case_id = c1.case_id,
              r.source_rule        = "Rule_10_Merged_Case_Propagation",
              r.confidence         = "High",
              r.asserted_at        = $asserted_at,
              r.first_asserted_at  = $asserted_at,
              r.status             = "active"
RETURN count(DISTINCT r) AS writes
