// Rule 8: High Risk Escalation — Recidivist in an Active Fraud Network. Wave 2.
//
// Trigger : A has HAS_PRIOR_GUILTY_CASE (Rule 7) AND A is
//           MEMBER_OF_FRAUD_NETWORK (Rules 2/4/6/9) AND A appears in an
//           ACTIVE case C -> case C carries High risk.
// Writes  : properties on the :Case node. There is no relationship type in
//           Section 3.2 for "case is high risk" and inventing one would put
//           a rule's conclusion into the ontology's vocabulary without a
//           spec change. A property, carrying the same Section 3.3
//           provenance quartet (confidence / source_rule / asserted_at /
//           status), keeps this auditable without that.
//
// EXECUTION ORDER IS A REAL DEPENDENCY: this rule reads what Rules 7 and
// 2/4/6/9 wrote *in this same run*. reasoning_layer/rule_registry.py's
// WAVE_2_RULE_IDS list order is the execution order and must not be sorted
// alphabetically or by rule number.

MATCH (a:Subject)-[pg:HAS_PRIOR_GUILTY_CASE]->(:Case)
MATCH (a)-[mem:MEMBER_OF_FRAUD_NETWORK]->(network:FraudNetwork)
MATCH (a)-[:APPEARS_IN_CASE]->(c:Case)
WHERE a.subject_id IN $scope_subject_ids
  AND pg.status = "active" AND mem.status = "active"
  AND toLower(coalesce(c.status, "")) IN $active_case_statuses
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "CASE_RISK_ESCALATION", status: "active"})
        WHERE rej.from_key = a.subject_id AND rej.to_key = c.case_id
      }
WITH c, a, collect(DISTINCT network.network_type) AS network_types
SET c.risk_escalation             = "High",
    c.risk_escalation_reason      = "Recidivist subject in an active fraud network",
    c.risk_escalation_subject_id  = a.subject_id,
    c.risk_escalation_networks    = network_types,
    c.risk_escalation_source_rule = "Rule_08_Recidivist_Escalation",
    c.risk_escalation_confidence  = "High",
    c.risk_escalation_asserted_at = $asserted_at,
    c.risk_escalation_status      = "active"
RETURN count(DISTINCT c) AS writes
