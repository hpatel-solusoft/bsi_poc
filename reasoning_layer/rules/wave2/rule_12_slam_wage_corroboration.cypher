// Rule 12: SLAM Wage Corroboration — Wave 2.
//
// Trigger : Subject A has a SLAM allegation attributed to them AND A has a
//           wage record overlapping the case's fraud date range
//           -> assert corroborating wage evidence.
// Writes  : :Allegation.wage_corroborated = true and
//           :Allegation.corroborating_employer_fein — both already declared
//           :Allegation properties in Section 3.1. No new relationship type
//           is invented; the schema already anticipated this rule's output.
//
// DATE-RANGE DEGRADATION, EXPLICIT AND FLAGGED:
// The rule as written depends on "the case's fraud date range". No
// confirmed AppWorks source for that range exists (GAP_ANALYSIS.md), so
// :Case.fraud_start_date / fraud_end_date will usually be null. Two honest
// options existed: refuse to fire at all, or fire without the date check
// and say so. Silently dropping the date condition and still reporting
// "High" would be the one unacceptable choice, because it presents an
// unverified overlap as verified evidence.
//
// So: the rule fires either way, but records which it did.
//   date_overlap_verified = true  + confidence High   (dates present, overlap holds)
//   date_overlap_verified = false + confidence Medium (dates absent — wage
//                                   record exists, overlap UNVERIFIED)
// An investigator reading the graph can tell the two apart, and so can
// Rule 14, Report Generation and the Copilot.

MATCH (c:Case)-[:HAS_ALLEGATION]->(al:Allegation)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject)
WHERE a.subject_id IN $scope_subject_ids
  AND att.status = "active"
  AND any(t IN $slam_allegation_types
          WHERE toLower(coalesce(al.allegation_type, "")) CONTAINS t)
MATCH (a)-[w:HAS_WAGE_RECORD_WITH]->(e:Employer)
WITH c, al, a, e, w,
     (c.fraud_start_date IS NOT NULL AND c.fraud_end_date IS NOT NULL
      AND w.period_start IS NOT NULL AND w.period_end IS NOT NULL) AS dates_present
WITH c, al, a, e, w, dates_present,
     CASE WHEN dates_present
          THEN (date(w.period_start) <= date(c.fraud_end_date)
                AND date(w.period_end) >= date(c.fraud_start_date))
          ELSE false END AS overlaps
// Keep the row when the overlap genuinely holds, OR when the dates simply
// are not there to check — never when dates ARE present and the periods do
// not overlap, which is a real disconfirmation and must not corroborate.
// Parentheses are load-bearing: AND binds tighter than OR in Cypher, so
// without them the rejection guard would only apply to the no-dates branch.
WHERE (overlaps OR NOT dates_present)
  AND NOT EXISTS {
        MATCH (rej:Rejection {relationship_type: "WAGE_CORROBORATION", status: "active"})
        WHERE rej.from_key = a.subject_id AND rej.to_key = al.allegation_id
      }
SET al.wage_corroborated             = true,
    al.corroborating_employer_fein   = e.fein,
    al.corroborating_employer_key    = e.employer_key,
    al.wage_corroboration_verified   = overlaps,
    al.wage_corroboration_confidence = CASE WHEN overlaps THEN "High" ELSE "Medium" END,
    al.wage_corroboration_rule       = "Rule_12_SLAM_Wage_Corroboration",
    al.wage_corroboration_asserted_at = $asserted_at,
    al.wage_corroboration_status     = "active"
RETURN count(DISTINCT al) AS writes
