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
//
// WAGE-LINK DEGRADATION, EXPLICIT AND FLAGGED (added — same pattern as
// Rule 12's date-range degradation, for the same reason).
//
// The wage leg above is the rule's signature and it has NOT been deleted.
// It has been demoted from a hard precondition to a recorded outcome,
// because AppWorks' Subject_Job and Subject_SubjectWages endpoints return
// HTTP 500 and the graph therefore holds zero wage records. As a hard
// EXISTS, the rule could never fire — not because the pattern is absent
// from the data, but because the evidence for it cannot be fetched.
//
// Two honest options existed: refuse to fire at all, or fire without the
// wage check and say so. Silently dropping the condition and still writing
// "High" would be the one unacceptable choice, because MEMBER_OF_FRAUD_NETWORK
// is read by Rule 8 to escalate a case to High risk — an unverified network
// would propagate into a risk escalation an investigator cannot audit.
//
// So the rule fires either way, and records which it did:
//   wage_link_verified = true  + confidence as capped by attribution
//                        (shared wage employer confirmed)
//   wage_link_verified = false + confidence capped at Medium
//                        (no shared wage record — co-subject + check-split
//                         allegation only, wage corroboration UNVERIFIED)
//
// The Medium cap is deliberate and is what keeps this safe: a network with
// no wage evidence can never present as High to an investigator, and can
// never be the sole High-confidence basis of a Rule 8 escalation. When the
// Subject_Job / Subject_SubjectWages endpoints are fixed and wage records
// load, verified pairs upgrade themselves on the next run with no code
// change — this is not a permanent loosening of the rule.

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
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "MEMBER_OF_FRAUD_NETWORK", status: "active"})
        WHERE rej.from_key IN [a.subject_id, b.subject_id]
          AND rej.to_key = "CheckSplit:" + c.case_id
      }
// Evaluated, not required. The shared wage employer is still the strongest
// evidence this rule has; it now grades the result instead of gating it.
WITH a, b, c, att,
     EXISTS {
        MATCH (a)-[:HAS_WAGE_RECORD_WITH]->(:Employer)<-[:HAS_WAGE_RECORD_WITH]-(b)
     } AS wage_link_verified
WITH a, b, c, wage_link_verified,
     CASE WHEN att.confidence = "Unresolved" THEN "Unresolved"
          WHEN att.confidence = "High" THEN "High"
          ELSE "Medium" END AS attribution_confidence
WITH a, b, c, wage_link_verified,
     // Capped twice over: by attribution quality, then by whether the wage
     // link could be confirmed at all. Without the wage evidence the network
     // cannot exceed Medium, whatever the attribution says.
     CASE WHEN NOT wage_link_verified AND attribution_confidence = "High"
               THEN "Medium"
          ELSE attribution_confidence END AS capped_confidence
MERGE (network:FraudNetwork {network_type: "CheckSplit", network_key: c.case_id})
ON CREATE SET network.formed_by_rule = "Rule_09_PCA_CheckSplit",
              network.formed_at      = $asserted_at,
              network.case_id        = c.case_id
SET network.wage_link_verified = wage_link_verified,
    network.evidence_basis     = CASE WHEN wage_link_verified
                                      THEN "co_subject + check_split_allegation + shared_wage_employer"
                                      ELSE "co_subject + check_split_allegation (wage records unavailable)"
                                 END
MERGE (a)-[ra:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET ra.first_asserted_at = $asserted_at
SET ra.confidence         = capped_confidence,
    ra.wage_link_verified = wage_link_verified,
    ra.source_rule        = "Rule_09_PCA_CheckSplit",
    ra.asserted_at        = $asserted_at,
    ra.status             = coalesce(ra.status, "active"),
    ra.corroborated       = coalesce(ra.corroborated, false)
MERGE (b)-[rb:MEMBER_OF_FRAUD_NETWORK]->(network)
ON CREATE SET rb.first_asserted_at = $asserted_at
SET rb.confidence         = capped_confidence,
    rb.wage_link_verified = wage_link_verified,
    rb.source_rule        = "Rule_09_PCA_CheckSplit",
    rb.asserted_at        = $asserted_at,
    rb.status             = coalesce(rb.status, "active"),
    rb.corroborated       = coalesce(rb.corroborated, false)
RETURN count(DISTINCT network) AS writes