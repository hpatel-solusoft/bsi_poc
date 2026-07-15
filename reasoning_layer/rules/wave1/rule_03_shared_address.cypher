// Rule 3: Shared Address Detection — Wave 1 (structural, no LLM).
//
// Trigger  : Subjects A, B at the same normalised :Address, A != B.
// Writes   : (A)-[:SHARES_ADDRESS_WITH]-(B), symmetric.
// Confidence: Medium. Rule 14 elevates it to High when the Extraction
//             Stage independently confirms the connection in narrative
//             text (Section 6.1: "Medium, elevated if corroborated").
//             This rule never writes High itself — that would defeat the
//             point of an independent corroboration check.
//
// MATCH QUALITY: the join is on :Address.address_key, the normalised
// composite built by etl/normalizers.address_key(), NOT on the raw
// free-text street line AppWorks stores. Section 3.4's composite index
// on (street, city, state, zip) only matches when two investigators
// typed an address identically, which is not a safe assumption against
// real data entry — see etl/GAP_ANALYSIS.md.

MATCH (a:Subject)-[:HAS_ADDRESS]->(addr:Address)<-[:HAS_ADDRESS]-(b:Subject)
WHERE a.subject_id < b.subject_id
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "SHARES_ADDRESS_WITH", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key   IN [a.subject_id, b.subject_id]
      }
MERGE (a)-[r:SHARES_ADDRESS_WITH]-(b)
ON CREATE SET r.first_asserted_at = $asserted_at
SET r.confidence   = CASE WHEN coalesce(r.corroborated, false) THEN "High" ELSE "Medium" END,
    r.address_key  = addr.address_key,
    r.source_rule  = "Rule_03_Shared_Address",
    r.asserted_at  = $asserted_at,
    r.status       = coalesce(r.status, "active"),
    r.corroborated = coalesce(r.corroborated, false)
RETURN count(DISTINCT r) AS writes
