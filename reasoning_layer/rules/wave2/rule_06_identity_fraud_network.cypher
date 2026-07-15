// Rule 6: Identity Fraud Network Formation — Wave 2 (attribution-dependent).
//
// Trigger : A shares an alias pattern with B (Rule 5) AND A has a False
//           Identity allegation attributed to them
//           -> MEMBER_OF_FRAUD_NETWORK, network_type "Identity".
// Confidence: High (Section 6.1).
//
// Asymmetric on purpose: the allegation only has to be attributed to ONE
// side. That is what the rule says, and it is the right reading — the
// person whose identity was misused is not thereby a fraud suspect, but
// they ARE part of the identity network the investigator needs to see.
// Both subjects join the network; only A needs the allegation.
//
// NO CONFIRMED DEMO CASE (Section 10.2): Rules 5 and 6 have no case in
// the current 18-case set that exercises them. This is built to spec and
// unit-checkable against seed data, but it has never fired against real
// BSI data, and that is a known, stated gap — not an oversight.

MATCH (a:Subject)-[shared:SHARES_ALIAS_PATTERN_WITH]-(b:Subject)
WHERE (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
  AND shared.status = "active"
MATCH (al:Allegation)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a)
WHERE att.status = "active"
  AND toLower(coalesce(al.status, "")) IN $active_allegation_statuses
  AND any(t IN $identity_allegation_types
          WHERE toLower(coalesce(al.allegation_type, "")) CONTAINS t)
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "MEMBER_OF_FRAUD_NETWORK", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key = "Identity:" + shared.alias_value
      }
WITH a, b, shared, att,
     CASE WHEN att.confidence = "Unresolved" THEN "Unresolved"
          WHEN att.confidence = "High" THEN "High"
          ELSE "Medium" END AS capped_confidence
MERGE (network:FraudNetwork {network_type: "Identity", network_key: shared.alias_value})
ON CREATE SET network.formed_by_rule = "Rule_06_Identity_Fraud_Network",
              network.formed_at      = $asserted_at,
              network.alias_value    = shared.alias_value
MERGE (a)-[ra:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET ra.first_asserted_at = $asserted_at
SET ra.confidence   = capped_confidence,
    ra.source_rule  = "Rule_06_Identity_Fraud_Network",
    ra.asserted_at  = $asserted_at,
    ra.status       = coalesce(ra.status, "active"),
    ra.corroborated = coalesce(ra.corroborated, false)
MERGE (b)-[rb:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET rb.first_asserted_at = $asserted_at
SET rb.confidence   = capped_confidence,
    rb.source_rule  = "Rule_06_Identity_Fraud_Network",
    rb.asserted_at  = $asserted_at,
    rb.status       = coalesce(rb.status, "active"),
    rb.corroborated = coalesce(rb.corroborated, false)
RETURN count(DISTINCT network) AS writes
