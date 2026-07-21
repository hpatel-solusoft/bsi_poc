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
//
// "ACTIVE" IS DERIVED BY EXCLUSION, NOT BY ALLOWLIST.
// This previously tested `c.status IN $active_case_statuses` against
// [open, active, under investigation, pending]. Any status AppWorks
// actually emits that is not on that list — "In Progress", "Assigned",
// "New" — made the rule match nothing, and produced writes=0 that is
// indistinguishable in the log from "this subject is not a recidivist".
// It is the same failure that kept Rule 7 silent until "adjudicated" was
// added to its closed list.
//
// A case is now active if it is NOT closed, reusing the SAME
// $closed_case_statuses list Rule 7 already maintains. This inverts the
// failure mode deliberately: an unrecognised status now yields an
// escalation an investigator can see and reject, rather than a silent
// non-escalation nobody knows to look for. For risk escalation, visible
// and wrong beats invisible and wrong.

MATCH (a:Subject)-[pg:HAS_PRIOR_GUILTY_CASE]->(prior:Case)
MATCH (a)-[mem:MEMBER_OF_FRAUD_NETWORK]->(network:FraudNetwork)
MATCH (a)-[:APPEARS_IN_CASE]->(c:Case)
WHERE a.subject_id IN $scope_subject_ids
  // coalesce, not strict equality. This graph carries null statuses on real
  // records (every :Case on case 658407433 has status null), and an edge
  // written before the status property existed would be silently skipped by
  // `= "active"`. Rule 2 itself writes status via coalesce(ra.status,
  // "active") for the same reason; reading it any more strictly than it is
  // written is how a rule ends up inert against its own upstream output.
  AND coalesce(pg.status, "active") = "active"
  AND coalesce(mem.status, "active") = "active"
  // Active by exclusion. A case with no status at all is treated as active:
  // absent data must not silently suppress a risk escalation.
  AND NOT toLower(coalesce(c.status, "")) IN $closed_case_statuses
  // The case being escalated cannot be the prior conviction itself.
  AND c.case_id <> prior.case_id
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "CASE_RISK_ESCALATION", status: "active"})
        WHERE rej.from_key = a.subject_id AND rej.to_key = c.case_id
      }
// Aggregate per CASE, not per (case, subject). Two recidivists on one case
// previously produced two rows that both SET the same properties, so
// risk_escalation_subject_id was whichever row Neo4j happened to write last
// — a non-deterministic audit field. Collecting first makes the winner
// explicit and the run reproducible.
WITH c,
     collect(DISTINCT a.subject_id)      AS subject_ids,
     collect(DISTINCT network.network_type) AS network_types
// reduce() rather than apoc.coll.sort: APOC is not a guaranteed dependency
// of this deployment, and a rule silently failing on a missing plugin is
// exactly the class of invisible failure this rule already suffered from.
WITH c, subject_ids, network_types,
     reduce(lead = head(subject_ids), s IN subject_ids |
            CASE WHEN s < lead THEN s ELSE lead END) AS lead_subject_id
SET c.risk_escalation             = "High",
    c.risk_escalation_reason      = "Recidivist subject in an active fraud network",
    c.risk_escalation_subject_id  = lead_subject_id,
    c.risk_escalation_subject_ids = subject_ids,
    c.risk_escalation_networks    = network_types,
    c.risk_escalation_source_rule = "Rule_08_Recidivist_Escalation",
    c.risk_escalation_confidence  = "High",
    c.risk_escalation_asserted_at = $asserted_at,
    c.risk_escalation_status      = "active"
RETURN count(DISTINCT c) AS writes