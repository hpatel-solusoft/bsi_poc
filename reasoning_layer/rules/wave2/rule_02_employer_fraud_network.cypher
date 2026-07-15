// Rule 2: Employer Fraud Network Formation — Wave 2 (attribution-dependent).
//
// Trigger : A and B share an employer (Rule 1's SHARES_EMPLOYER_WITH)
//           AND both have an active PCA/Employment allegation *attributed
//           to them* -> MEMBER_OF_FRAUD_NETWORK, network_type "Employer".
// Confidence: High, capped by attribution quality (Section 6.1) — so the
//           written confidence is the WEAKER of the two attributions, never
//           a flat "High". An attribution the Extraction Stage marked
//           "Medium" cannot produce a High network membership; that is what
//           "capped by attribution" means, and writing High anyway would
//           overstate the evidence to the investigator.
//
// WAVE 2 PRECONDITION: this reads ALLEGATION_LIKELY_AGAINST_SUBJECT, which
// only exists after the Extraction Stage (Steps 3-4) has run. Running this
// against a graph with no attribution edges is not an error — it fires
// zero times, correctly.
//
// Allegation-type and status vocabularies come from the :InferenceRule
// registry ($employer_allegation_types, $active_allegation_statuses), not
// from string literals here — BSI can retune them without a deploy.

MATCH (a:Subject)-[shared:SHARES_EMPLOYER_WITH]-(b:Subject)
WHERE a.subject_id < b.subject_id
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
  AND shared.status = "active"
MATCH (e:Employer {employer_key: shared.employer_key})
MATCH (al_a:Allegation)-[att_a:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a)
MATCH (al_b:Allegation)-[att_b:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(b)
WHERE att_a.status = "active" AND att_b.status = "active"
  AND toLower(coalesce(al_a.status, "")) IN $active_allegation_statuses
  AND toLower(coalesce(al_b.status, "")) IN $active_allegation_statuses
  AND any(t IN $employer_allegation_types
          WHERE toLower(coalesce(al_a.allegation_type, "")) CONTAINS t)
  AND any(t IN $employer_allegation_types
          WHERE toLower(coalesce(al_b.allegation_type, "")) CONTAINS t)
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "MEMBER_OF_FRAUD_NETWORK", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key = "Employer:" + shared.employer_key
      }
WITH a, b, e, shared,
     // "Capped by attribution": the network is only as strong as the weakest
     // attribution underneath it.
     CASE WHEN att_a.confidence = "High" AND att_b.confidence = "High" THEN "High"
          WHEN att_a.confidence = "Unresolved" OR att_b.confidence = "Unresolved" THEN "Unresolved"
          ELSE "Medium" END AS capped_confidence
MERGE (network:FraudNetwork {network_type: "Employer", network_key: e.employer_key})
ON CREATE SET network.formed_by_rule = "Rule_02_Employer_Fraud_Network",
              network.formed_at      = $asserted_at,
              network.employer_name  = e.employer_name,
              network.employer_fein  = e.fein
MERGE (a)-[ra:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET ra.first_asserted_at = $asserted_at
SET ra.confidence  = capped_confidence,
    ra.source_rule = "Rule_02_Employer_Fraud_Network",
    ra.asserted_at = $asserted_at,
    ra.status      = coalesce(ra.status, "active"),
    ra.corroborated = coalesce(ra.corroborated, false)
MERGE (b)-[rb:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET rb.first_asserted_at = $asserted_at
SET rb.confidence  = capped_confidence,
    rb.source_rule = "Rule_02_Employer_Fraud_Network",
    rb.asserted_at = $asserted_at,
    rb.status      = coalesce(rb.status, "active"),
    rb.corroborated = coalesce(rb.corroborated, false)
RETURN count(DISTINCT network) AS writes
