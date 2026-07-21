// Rule 2: Employer Fraud Network Formation — Wave 2 (attribution-dependent).
//
// Trigger : A and B share an employer (Rule 1's SHARES_EMPLOYER_WITH), that
//           employer exists in the graph, AND both subjects have an active
//           allegation attributed to them OF THE SAME TYPE
//           -> MEMBER_OF_FRAUD_NETWORK, network_type "Employer".
//
// The same-type condition is the point of this rule: two people at one
// employer is an HR fact, but two people at one employer facing the SAME
// kind of allegation is a pattern. Requiring only "both have some
// allegation" would group a wage complaint with an unrelated identity
// complaint and call it a fraud network.
//
// WAVE 2 PRECONDITION: this reads ALLEGATION_LIKELY_AGAINST_SUBJECT, which
// only exists after the Extraction Stage (Steps 3-4) has run. Running it
// against a graph with no attribution edges is not an error — it fires
// zero times, correctly. (This precondition was absent while the rule was
// briefly employer-only; it is back, so Rule 2 again depends on extraction
// having completed for BOTH subjects, not just the primary.)
//
// TYPE COMPARISON is on toLower(trim(...)) so "PCA" and "pca " are the
// same allegation type. It is an exact normalised match, NOT a substring
// match: "Wage" must not silently group with "Wage Theft", because those
// are different allegations that would produce a network no investigator
// asserted.

MATCH (a:Subject)-[shared:SHARES_EMPLOYER_WITH]-(b:Subject)
// Each undirected pair matches twice (A-B and B-A). This orders the pair so
// the rule does the work once — MERGE would dedupe the data either way, but
// the write count reported to the investigator would otherwise be doubled.
WHERE a.subject_id < b.subject_id
  // Runs for every subject the pipeline has in scope, from either end of
  // the shared-employer edge.
  AND (a.subject_id IN $scope_subject_ids OR b.subject_id IN $scope_subject_ids)
  AND shared.status = "active"
  // An investigator who rejected this network must not have it silently
  // re-formed on the next pipeline run. This guard is what makes
  // /reject_inference durable, and is kept deliberately.
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "MEMBER_OF_FRAUD_NETWORK", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key = "Employer:" + shared.employer_key
      }
MATCH (e:Employer {employer_key: shared.employer_key})
MATCH (al_a:Allegation)-[att_a:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a)
MATCH (al_b:Allegation)-[att_b:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(b)
WHERE att_a.status = "active" AND att_b.status = "active"
  // NULL GUARD, and it is load-bearing: without it two subjects who both
  // have NO allegation type recorded would compare equal (null = null is
  // not true in Cypher, but coalesce to "" would be) and form a network on
  // the absence of evidence.
  AND al_a.allegation_type IS NOT NULL
  AND al_b.allegation_type IS NOT NULL
  AND trim(al_a.allegation_type) <> ""
  AND trim(al_b.allegation_type) <> ""
  // THE SAME-TYPE CONDITION.
  AND toLower(trim(al_a.allegation_type)) = toLower(trim(al_b.allegation_type))

WITH a, b, e, shared,
     // The type that actually formed this membership, recorded on the edge
     // so an investigator can see WHICH shared allegation put these two
     // subjects together rather than inferring it from the employer.
     trim(al_a.allegation_type) AS matched_allegation_type
MERGE (network:FraudNetwork {network_type: "Employer", network_key: e.employer_key})
ON CREATE SET network.formed_by_rule = "Rule_02_Employer_Fraud_Network",
              network.formed_at      = $asserted_at,
              // Read from the :Employer node, never hardcoded — the network
              // must name the employer the edge actually points at.
              network.employer_name  = e.employer_name,
              network.employer_fein  = e.fein
MERGE (a)-[ra:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET ra.first_asserted_at = $asserted_at
SET ra.confidence       = "High",
    ra.source_rule      = "Rule_02_Employer_Fraud_Network",
    ra.allegation_type  = matched_allegation_type,
    ra.asserted_at      = $asserted_at,
    // coalesce, not a plain SET: a re-run must not resurrect an edge an
    // investigator rejected, nor clear a Rule 14 corroboration.
    ra.status           = coalesce(ra.status, "active"),
    ra.corroborated     = coalesce(ra.corroborated, false)
MERGE (b)-[rb:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET rb.first_asserted_at = $asserted_at
SET rb.confidence       = "High",
    rb.source_rule      = "Rule_02_Employer_Fraud_Network",
    rb.allegation_type  = matched_allegation_type,
    rb.asserted_at      = $asserted_at,
    rb.status           = coalesce(rb.status, "active"),
    rb.corroborated     = coalesce(rb.corroborated, false)
RETURN count(DISTINCT network) AS writes