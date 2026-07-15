// Rule 5: Alias-Based Identity Connection — Wave 1 (structural, no LLM).
//
// Trigger  : Subjects A, B share an exact :Alias value, A != B.
// Writes   : (A)-[:SHARES_ALIAS_PATTERN_WITH]-(B), symmetric.
// Confidence: High.
//
// EXACT MATCH ONLY, deliberately. Section 6.1 is explicit ("Exact string
// match only") and ETL stores alias_value trimmed but not case-folded or
// fuzzed (etl/normalizers.alias_value). Widening this to a fuzzy match
// would quietly change a rule the spec keeps narrow on purpose — if
// fuzzy alias matching is wanted, that is a spec change, not a Cypher
// tweak.

MATCH (a:Subject)-[:HAS_ALIAS]->(al:Alias)<-[:HAS_ALIAS]-(b:Subject)
WHERE a.subject_id < b.subject_id
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "SHARES_ALIAS_PATTERN_WITH", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key   IN [a.subject_id, b.subject_id]
      }
MERGE (a)-[r:SHARES_ALIAS_PATTERN_WITH]-(b)
ON CREATE SET r.first_asserted_at = $asserted_at
SET r.confidence   = "High",
    r.alias_value  = al.alias_value,
    r.source_rule  = "Rule_05_Alias_Identity",
    r.asserted_at  = $asserted_at,
    r.status       = coalesce(r.status, "active"),
    r.corroborated = coalesce(r.corroborated, false)
RETURN count(DISTINCT r) AS writes
