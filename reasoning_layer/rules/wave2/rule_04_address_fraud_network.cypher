// Rule 4: Address Fraud Network Formation — Wave 2 (attribution-dependent).
//
// Trigger : A and B share an address (Rule 3's SHARES_ADDRESS_WITH) AND
//           both have active allegations attributed to them under
//           DIFFERENT Cases -> MEMBER_OF_FRAUD_NETWORK, type "Address".
// Confidence: Medium-High, capped by attribution (Section 6.1).
//
// The "different Cases" condition is the whole point of this rule and is
// easy to lose: two people at one address on the SAME case is just a
// household, and is already covered by IS_CO_SUBJECT_WITH. Two people at
// one address turning up on SEPARATE cases is the pattern worth flagging.
// case_a.case_id <> case_b.case_id below is that condition, not a detail.

MATCH (a:Subject)-[shared:SHARES_ADDRESS_WITH]-(b:Subject)
WHERE a.subject_id < b.subject_id
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
  AND shared.status = "active"
MATCH (addr:Address {address_key: shared.address_key})
MATCH (case_a:Case)-[:HAS_ALLEGATION]->(al_a:Allegation)-[att_a:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a)
MATCH (case_b:Case)-[:HAS_ALLEGATION]->(al_b:Allegation)-[att_b:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(b)
WHERE case_a.case_id <> case_b.case_id
  AND att_a.status = "active" AND att_b.status = "active"
  AND toLower(coalesce(al_a.status, "")) IN $active_allegation_statuses
  AND toLower(coalesce(al_b.status, "")) IN $active_allegation_statuses
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "MEMBER_OF_FRAUD_NETWORK", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key = "Address:" + shared.address_key
      }
WITH a, b, addr,
     CASE WHEN att_a.confidence = "Unresolved" OR att_b.confidence = "Unresolved" THEN "Unresolved"
          WHEN att_a.confidence = "High" AND att_b.confidence = "High" THEN "High"
          ELSE "Medium" END AS capped_confidence
MERGE (network:FraudNetwork {network_type: "Address", network_key: addr.address_key})
ON CREATE SET network.formed_by_rule = "Rule_04_Address_Fraud_Network",
              network.formed_at      = $asserted_at,
              network.address_street = addr.street,
              network.address_city   = addr.city
MERGE (a)-[ra:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET ra.first_asserted_at = $asserted_at
SET ra.confidence   = capped_confidence,
    ra.source_rule  = "Rule_04_Address_Fraud_Network",
    ra.asserted_at  = $asserted_at,
    ra.status       = coalesce(ra.status, "active"),
    ra.corroborated = coalesce(ra.corroborated, false)
MERGE (b)-[rb:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET rb.first_asserted_at = $asserted_at
SET rb.confidence   = capped_confidence,
    rb.source_rule  = "Rule_04_Address_Fraud_Network",
    rb.asserted_at  = $asserted_at,
    rb.status       = coalesce(rb.status, "active"),
    rb.corroborated = coalesce(rb.corroborated, false)
RETURN count(DISTINCT network) AS writes
