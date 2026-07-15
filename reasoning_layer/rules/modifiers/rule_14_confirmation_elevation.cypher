// Rule 14: Extraction-Confirmed Relationship Elevation — CROSS-CUTTING
// MODIFIER. Not a 15th rule and not a wave member (Section 5.4).
//
// Trigger : any structurally-inferred relationship that the Extraction
//           Stage independently confirmed in narrative text
//           -> Medium becomes High; High gains corroborated = true.
//
// DEVIATION FROM THE DOC, FLAGGED RATHER THAN HIDDEN:
// Section 5.4 says Rule 14 is "embedded inside the implementation of every
// other rule" and that "there is no standalone rule_14.cypher file". This
// build implements it as ONE file, in rules/modifiers/ (not rules/wave1/
// or rules/wave2/, so it is still not a wave member), executed after both
// waves. Reasons, in order of weight:
//   1. The elevation logic is identical for every relationship it touches.
//      Embedding it inside 13 rule files means 13 copies of one CASE
//      expression, and a corroboration semantics change becoming a
//      13-file edit — the exact drift Himali's memo is about.
//   2. It CANNOT run inside Wave 1 anyway. Wave 1 executes before the
//      Extraction Stage, so at Wave 1 time no confirmation evidence
//      exists yet. A Rule 14 embedded in Rule 1 would be reading a field
//      that is guaranteed to be empty.
//   3. Section 6.2's own worked example for Rule 14 is written as a
//      standalone MATCH...SET, i.e. as a separate query, which is what
//      this is.
// The semantics are unchanged from the doc. The file layout is not. If
// the reference doc's author wants it literally inlined 13 times, that is
// a one-file-to-13 refactor, not a redesign.
//
// HOW CONFIRMATION IS RECORDED: reasoning_layer/graph_load.py writes
// (:Commentary).confirms_relationship_ids — a list of elementId(r) values
// for the relationships the Extraction Stage found the narrative to
// independently confirm. That is Section 6.2's own mechanism
// (comm.confirms_relationship_id, "set by Extraction Stage"), widened to a
// list because one comment routinely confirms more than one relationship.

MATCH (a:Subject)-[r:SHARES_EMPLOYER_WITH|SHARES_ADDRESS_WITH|SHARES_ALIAS_PATTERN_WITH|MEMBER_OF_FRAUD_NETWORK|HAS_PRIOR_GUILTY_CASE]-(other)
WHERE a.subject_id IN $scope_subject_ids
  AND r.status = "active"
  AND EXISTS {
        MATCH (comm:Commentary)
        WHERE comm.confirms_relationship_ids IS NOT NULL
          AND elementId(r) IN comm.confirms_relationship_ids
      }
SET r.confidence      = CASE WHEN r.confidence = "Medium" THEN "High" ELSE r.confidence END,
    r.corroborated    = true,
    r.corroborated_by = "Rule_14_Confirmation_Elevation",
    r.corroborated_at = $asserted_at
RETURN count(DISTINCT r) AS writes
