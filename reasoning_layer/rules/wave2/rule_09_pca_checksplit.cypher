// Rule 9: PCA Check-Split Network Detection — Wave 2. The signature rule
// for the primary demo case (BSI-2026-1247).
//
// Trigger : Consumer A is co-subject with PCA B on case C, C carries a
//           Check-Splitting allegation attributed to A and/or B, AND A
//           shares a WAGE-RECORD employer with B
//           -> MEMBER_OF_FRAUD_NETWORK, network_type "CheckSplit".
// Confidence: High, attribution-dependent.
//
// The wage-record leg is the point of the rule, not decoration: a consumer
// and their personal care attendant appearing on one case together is
// normal. The two of them ALSO turning up on the same employer's wage
// records is the check-splitting signature. HAS_WAGE_RECORD_WITH — not
// EMPLOYED_BY — is what Section 6.2's worked example matches on, because
// the Wage table is the independent, better-covered path (Section 3.2).
// It is also why the Wage-table FEIN ask in GAP_ANALYSIS.md matters: with
// no FEIN, two subjects' wage employers only unify when AppWorks gives
// them the same employer id.

MATCH (a:Subject)-[co:IS_CO_SUBJECT_WITH]-(b:Subject)
WHERE a.subject_id < b.subject_id
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
MATCH (a)-[:APPEARS_IN_CASE]->(c:Case)<-[:APPEARS_IN_CASE]-(b)
MATCH (c)-[:HAS_ALLEGATION]->(al:Allegation)
WHERE any(t IN $checksplit_allegation_types
          WHERE toLower(coalesce(al.allegation_type, "")) CONTAINS t)
MATCH (al)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(attributed:Subject)
WHERE att.status = "active"
  AND attributed.subject_id IN [a.subject_id, b.subject_id]
  AND EXISTS {
        MATCH (a)-[:HAS_WAGE_RECORD_WITH]->(e:Employer)<-[:HAS_WAGE_RECORD_WITH]-(b)
      }
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "MEMBER_OF_FRAUD_NETWORK", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key = "CheckSplit:" + c.case_id
      }
WITH a, b, c, att,
     CASE WHEN att.confidence = "Unresolved" THEN "Unresolved"
          WHEN att.confidence = "High" THEN "High"
          ELSE "Medium" END AS capped_confidence
MERGE (network:FraudNetwork {network_type: "CheckSplit", network_key: c.case_id})
ON CREATE SET network.formed_by_rule = "Rule_09_PCA_CheckSplit",
              network.formed_at      = $asserted_at,
              network.case_id        = c.case_id
MERGE (a)-[ra:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET ra.first_asserted_at = $asserted_at
SET ra.confidence   = capped_confidence,
    ra.source_rule  = "Rule_09_PCA_CheckSplit",
    ra.asserted_at  = $asserted_at,
    ra.status       = coalesce(ra.status, "active"),
    ra.corroborated = coalesce(ra.corroborated, false)
MERGE (b)-[rb:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET rb.first_asserted_at = $asserted_at
SET rb.confidence   = capped_confidence,
    rb.source_rule  = "Rule_09_PCA_CheckSplit",
    rb.asserted_at  = $asserted_at,
    rb.status       = coalesce(rb.status, "active"),
    rb.corroborated = coalesce(rb.corroborated, false)
RETURN count(DISTINCT network) AS writes
