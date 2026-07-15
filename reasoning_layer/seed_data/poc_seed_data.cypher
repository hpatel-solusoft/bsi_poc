// BSI Phase 2 — POC / demo seed data for Neo4j.
//
// Purpose: give the reasoning layer real graph data to run against
// without a live AppWorks fetch. Every one of the fourteen rules has a
// dedicated fixture below, with its expected outcome stated in the
// comment, so a test run can be checked against something concrete
// rather than eyeballed.
//
// Apply, in this order:
//     python -m reasoning_layer.apply_schema          # constraints + rule registry
//     cat reasoning_layer/seed_data/poc_seed_data.cypher | cypher-shell -u neo4j -p ...
//     cat reasoning_layer/seed_data/seed_attributions.cypher | cypher-shell ...   # optional, see below
//
// Every MERGE below keys on exactly what etl/graph_sync.py keys on
// (employer_key, address_key, comment_id, subject_id, case_id), so this
// seed and a real AppWorks ingest write to the SAME nodes rather than
// two parallel shadow graphs. That is deliberate: a demo built on nodes
// the real ETL would never touch proves nothing about the real ETL.
//
// WHAT THIS FILE DOES NOT SEED: ALLEGATION_LIKELY_AGAINST_SUBJECT edges.
// Those are the Extraction Stage's output — the LLM reads the commentary
// below and produces them. That is the real path and it is what should be
// exercised. seed_attributions.cypher hand-writes those same edges for
// when you need to validate Wave 2 WITHOUT an LLM key (or want to isolate
// a Wave 2 rule failure from an extraction-quality problem). Use one or
// the other, not both — they write the same edges.

// ============================================================
// SCENARIO A — the signature demo case.
// Consumer + PCA on one case, Check-Splitting allegation, shared wage
// employer, and the consumer has a prior guilty case.
//
// Expected to fire: Rule 1 (shared employer, High/FEIN), Rule 9
// (CheckSplit network), Rule 7 (prior guilty), Rule 8 (recidivist in an
// active network -> case risk High), Rule 13 (fraud amount 75k > 50k
// threshold + prior guilty + not already fast-tracked), Rule 11 (S-1001
// is a co-subject across two cases), Rule 14 (the commentary confirms the
// shared employer in its own words).
// ============================================================
MERGE (c1:Case {case_id: "CASE-1001"})
  SET c1.status = "Open", c1.complaint_number = "CN-1001",
      c1.fraud_amount = 75000.0, c1.is_fasttrack = false, c1.is_dta_case = false,
      c1.fraud_start_date = "2025-01-01", c1.fraud_end_date = "2025-12-31",
      c1.opened_date = "2026-01-15",
      c1.source_system = "SEED", c1.source_table = "Workfolder", c1.stub = false;

MERGE (s1:Subject {subject_id: "S-1001"})
  SET s1.first_name = "Alice", s1.last_name = "Nolan", s1.subject_type = "Individual",
      s1.source_system = "SEED";
MERGE (s2:Subject {subject_id: "S-1002"})
  SET s2.first_name = "Robert", s2.last_name = "Keene", s2.subject_type = "Individual",
      s2.source_system = "SEED";

MATCH (c:Case {case_id: "CASE-1001"}), (s:Subject {subject_id: "S-1001"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c)
  SET r.subject_role = "Consumer", r.is_primary = true,
      r.source_table = "Workfolder_SubjectsRelationship", r.retrieved_at = "2026-01-15";
MATCH (c:Case {case_id: "CASE-1001"}), (s:Subject {subject_id: "S-1002"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c)
  SET r.subject_role = "PCA", r.is_primary = false,
      r.source_table = "Workfolder_SubjectsRelationship", r.retrieved_at = "2026-01-15";

MATCH (a:Subject {subject_id: "S-1001"}), (b:Subject {subject_id: "S-1002"})
MERGE (a)-[r:IS_CO_SUBJECT_WITH]-(b)
  SET r.case_id = "CASE-1001", r.source_table = "Workfolder_SubjectsRelationship";

// Employer, FEIN-keyed -> Rule 1 writes confidence "High" (not "Medium")
MERGE (e1:Employer {employer_key: "FEIN:041234567"})
  SET e1.fein = "041234567", e1.employer_name = "Acme Staffing Corp", e1.source_system = "SEED";
MATCH (s:Subject {subject_id: "S-1001"}), (e:Employer {employer_key: "FEIN:041234567"})
MERGE (s)-[r:EMPLOYED_BY]->(e) SET r.source_table = "Subject_Job";
MATCH (s:Subject {subject_id: "S-1002"}), (e:Employer {employer_key: "FEIN:041234567"})
MERGE (s)-[r:EMPLOYED_BY]->(e) SET r.source_table = "Subject_Job";

// Wage records — Rule 9's second leg. Both subjects on the same employer's
// payroll in the same quarter is the check-splitting signature; without
// these two edges Rule 9 does not fire, no matter how good the narrative is.
MATCH (s:Subject {subject_id: "S-1001"}), (e:Employer {employer_key: "FEIN:041234567"})
MERGE (s)-[r:HAS_WAGE_RECORD_WITH {period_key: "2025|Q1|2025-01-01|2025-03-31"}]->(e)
  SET r.period_start = "2025-01-01", r.period_end = "2025-03-31",
      r.wage_amount = 8200.0, r.source_table = "Subject_SubjectWages";
MATCH (s:Subject {subject_id: "S-1002"}), (e:Employer {employer_key: "FEIN:041234567"})
MERGE (s)-[r:HAS_WAGE_RECORD_WITH {period_key: "2025|Q1|2025-01-01|2025-03-31"}]->(e)
  SET r.period_start = "2025-01-01", r.period_end = "2025-03-31",
      r.wage_amount = 7900.0, r.source_table = "Subject_SubjectWages";

MERGE (al1:Allegation {allegation_id: "AL-1001"})
  SET al1.allegation_type = "Check-Splitting", al1.status = "Open",
      al1.comment_text = "Timesheets submitted by Robert Keene for hours billed to Alice Nolan's care plan were split across two pay periods.",
      al1.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1001"}), (al:Allegation {allegation_id: "AL-1001"})
MERGE (c)-[r:HAS_ALLEGATION]->(al) SET r.source_table = "Allegations_Workfolder_Id";

// Commentary: attributes the allegation explicitly (Extraction Stage should
// return High confidence) AND independently confirms the shared employer
// (which is what gives Rule 14 something to elevate).
MERGE (cm1:Commentary {comment_id: "Case_Commentary:seed-1001-a"})
  SET cm1.comment_text = "Robert Keene, the personal care attendant, submitted the split timesheets. Alice Nolan signed off on them. Both are on the payroll of Acme Staffing Corp, the same employer.",
      cm1.comment_type = "Investigator Note", cm1.created_date = "2026-01-20",
      cm1.case_id = "CASE-1001", cm1.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1001"}), (cm:Commentary {comment_id: "Case_Commentary:seed-1001-a"})
MERGE (c)-[r:HAS_COMMENTARY]->(cm) SET r.source_table = "WorkfolderCommentary";

// Prior guilty case for S-1001 -> Rules 7, 8, 13 all depend on this.
MERGE (c0:Case {case_id: "CASE-0900"})
  SET c0.status = "Closed", c0.disposition = "Guilty", c0.closed_date = "2023-11-02",
      c0.fraud_amount = 21000.0, c0.source_system = "SEED", c0.stub = false;
MATCH (c:Case {case_id: "CASE-0900"}), (s:Subject {subject_id: "S-1001"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c)
  SET r.subject_role = "Subject", r.is_primary = true, r.source_table = "Workfolder_SubjectsRelationship";
MERGE (al0:Allegation {allegation_id: "AL-0900"})
  SET al0.allegation_type = "Employment Fraud", al0.status = "Closed",
      al0.outcome = "Guilty", al0.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-0900"}), (al:Allegation {allegation_id: "AL-0900"})
MERGE (c)-[r:HAS_ALLEGATION]->(al);
MERGE (cm0:Commentary {comment_id: "Case_Commentary:seed-0900-a"})
  SET cm0.comment_text = "Alice Nolan was found guilty of falsifying employment records in this matter.",
      cm0.comment_type = "Disposition", cm0.created_date = "2023-11-02",
      cm0.case_id = "CASE-0900", cm0.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-0900"}), (cm:Commentary {comment_id: "Case_Commentary:seed-0900-a"})
MERGE (c)-[r:HAS_COMMENTARY]->(cm);

// ============================================================
// SCENARIO B — Rule 2 (Employer Fraud Network) and Rule 11 (cross-case hub).
// S-1001 turns up again on a second case with a DIFFERENT co-subject, both
// with Employment allegations, both at the same employer.
//
// Expected to fire: Rule 2 (Employer network), Rule 11 (S-1001 is a
// co-subject across CASE-1001 and CASE-1002 with two different people).
// ============================================================
MERGE (c2:Case {case_id: "CASE-1002"})
  SET c2.status = "Open", c2.fraud_amount = 31000.0, c2.is_fasttrack = false,
      c2.source_system = "SEED", c2.stub = false;
MERGE (s3:Subject {subject_id: "S-1003"})
  SET s3.first_name = "Dana", s3.last_name = "Ruiz", s3.subject_type = "Individual", s3.source_system = "SEED";

MATCH (c:Case {case_id: "CASE-1002"}), (s:Subject {subject_id: "S-1001"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c) SET r.subject_role = "Subject", r.is_primary = false;
MATCH (c:Case {case_id: "CASE-1002"}), (s:Subject {subject_id: "S-1003"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c) SET r.subject_role = "Subject", r.is_primary = true;
MATCH (a:Subject {subject_id: "S-1001"}), (b:Subject {subject_id: "S-1003"})
MERGE (a)-[r:IS_CO_SUBJECT_WITH]-(b) SET r.case_id = "CASE-1002";

MATCH (s:Subject {subject_id: "S-1003"}), (e:Employer {employer_key: "FEIN:041234567"})
MERGE (s)-[r:EMPLOYED_BY]->(e) SET r.source_table = "Subject_Job";

MERGE (al2:Allegation {allegation_id: "AL-1002"})
  SET al2.allegation_type = "PCA Employment Fraud", al2.status = "Open", al2.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1002"}), (al:Allegation {allegation_id: "AL-1002"})
MERGE (c)-[r:HAS_ALLEGATION]->(al);
MERGE (al3:Allegation {allegation_id: "AL-1003"})
  SET al3.allegation_type = "PCA Employment Fraud", al3.status = "Open", al3.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1002"}), (al:Allegation {allegation_id: "AL-1003"})
MERGE (c)-[r:HAS_ALLEGATION]->(al);

MERGE (cm2:Commentary {comment_id: "Case_Commentary:seed-1002-a"})
  SET cm2.comment_text = "Dana Ruiz billed for shifts she did not work (AL-1002). Alice Nolan approved the same shifts knowing they were not worked (AL-1003). Both are employed by Acme Staffing Corp.",
      cm2.comment_type = "Investigator Note", cm2.created_date = "2026-02-02",
      cm2.case_id = "CASE-1002", cm2.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1002"}), (cm:Commentary {comment_id: "Case_Commentary:seed-1002-a"})
MERGE (c)-[r:HAS_COMMENTARY]->(cm);

// ============================================================
// SCENARIO C — Rules 3 and 4 (shared address, address fraud network).
// Two subjects, one address, allegations on DIFFERENT cases — which is the
// condition that separates a fraud network from a household.
//
// The two street strings are deliberately written differently ("14 Main
// Street" vs "14 MAIN ST.") to prove normalisation is doing its job: both
// resolve to the same address_key, so Rule 3 matches. Keyed on the raw
// string, they would not.
// ============================================================
MERGE (c3:Case {case_id: "CASE-1003"}) SET c3.status = "Open", c3.fraud_amount = 12000.0, c3.source_system = "SEED";
MERGE (c4:Case {case_id: "CASE-1004"}) SET c4.status = "Open", c4.fraud_amount = 9000.0, c4.source_system = "SEED";
MERGE (s4:Subject {subject_id: "S-1004"}) SET s4.first_name = "Marcus", s4.last_name = "Hale", s4.source_system = "SEED";
MERGE (s5:Subject {subject_id: "S-1005"}) SET s5.first_name = "Priya", s5.last_name = "Raman", s5.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1003"}), (s:Subject {subject_id: "S-1004"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c) SET r.is_primary = true, r.subject_role = "Subject";
MATCH (c:Case {case_id: "CASE-1004"}), (s:Subject {subject_id: "S-1005"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c) SET r.is_primary = true, r.subject_role = "Subject";

MERGE (addr:Address {address_key: "14 main st|springfield|MA|01103"})
  SET addr.street = "14 Main Street", addr.city = "Springfield", addr.state = "MA", addr.zip = "01103",
      addr.street_normalized = "14 main st", addr.source_system = "SEED";
MATCH (s:Subject {subject_id: "S-1004"}), (a:Address {address_key: "14 main st|springfield|MA|01103"})
MERGE (s)-[r:HAS_ADDRESS]->(a) SET r.source_table = "Subject_Address";
MATCH (s:Subject {subject_id: "S-1005"}), (a:Address {address_key: "14 main st|springfield|MA|01103"})
MERGE (s)-[r:HAS_ADDRESS]->(a) SET r.source_table = "Subject_Address";

MERGE (al4:Allegation {allegation_id: "AL-1004"}) SET al4.allegation_type = "Benefits Fraud", al4.status = "Open", al4.source_system = "SEED";
MERGE (al5:Allegation {allegation_id: "AL-1005"}) SET al5.allegation_type = "Benefits Fraud", al5.status = "Open", al5.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1003"}), (al:Allegation {allegation_id: "AL-1004"}) MERGE (c)-[:HAS_ALLEGATION]->(al);
MATCH (c:Case {case_id: "CASE-1004"}), (al:Allegation {allegation_id: "AL-1005"}) MERGE (c)-[:HAS_ALLEGATION]->(al);

MERGE (cm3:Commentary {comment_id: "Case_Commentary:seed-1003-a"})
  SET cm3.comment_text = "Marcus Hale claimed benefits at an address he does not reside at (AL-1004). He shares the 14 Main Street address with Priya Raman.",
      cm3.comment_type = "Investigator Note", cm3.case_id = "CASE-1003", cm3.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1003"}), (cm:Commentary {comment_id: "Case_Commentary:seed-1003-a"})
MERGE (c)-[:HAS_COMMENTARY]->(cm);
MERGE (cm4:Commentary {comment_id: "Case_Commentary:seed-1004-a"})
  SET cm4.comment_text = "Priya Raman submitted a duplicate claim from the same household address (AL-1005).",
      cm4.comment_type = "Investigator Note", cm4.case_id = "CASE-1004", cm4.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1004"}), (cm:Commentary {comment_id: "Case_Commentary:seed-1004-a"})
MERGE (c)-[:HAS_COMMENTARY]->(cm);

// ============================================================
// SCENARIO D — Rules 5 and 6 (shared alias, identity fraud network).
// Section 10.2 records that the real 18-case set has no case exercising
// these two rules. This fixture is the only thing that does, which is
// exactly why it exists — and also why Rules 5 and 6 remain untested
// against real BSI data regardless of it passing here.
// ============================================================
MERGE (c5:Case {case_id: "CASE-1005"}) SET c5.status = "Open", c5.fraud_amount = 4000.0, c5.source_system = "SEED";
MERGE (s6:Subject {subject_id: "S-1006"}) SET s6.first_name = "Jonathan", s6.last_name = "Beck", s6.source_system = "SEED";
MERGE (s7:Subject {subject_id: "S-1007"}) SET s7.first_name = "John", s7.last_name = "Becker", s7.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1005"}), (s:Subject {subject_id: "S-1006"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c) SET r.is_primary = true, r.subject_role = "Subject";

MERGE (alias:Alias {alias_value: "Johnny B"}) SET alias.source_system = "SEED";
MATCH (s:Subject {subject_id: "S-1006"}), (a:Alias {alias_value: "Johnny B"})
MERGE (s)-[r:HAS_ALIAS]->(a) SET r.source_table = "Subject_Alias";
MATCH (s:Subject {subject_id: "S-1007"}), (a:Alias {alias_value: "Johnny B"})
MERGE (s)-[r:HAS_ALIAS]->(a) SET r.source_table = "Subject_Alias";

MERGE (al6:Allegation {allegation_id: "AL-1006"})
  SET al6.allegation_type = "False Identity", al6.status = "Open", al6.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1005"}), (al:Allegation {allegation_id: "AL-1006"}) MERGE (c)-[:HAS_ALLEGATION]->(al);
MERGE (cm5:Commentary {comment_id: "Case_Commentary:seed-1005-a"})
  SET cm5.comment_text = "Jonathan Beck used a false identity to open a second claim (AL-1006). He is known to use the alias Johnny B.",
      cm5.comment_type = "Investigator Note", cm5.case_id = "CASE-1005", cm5.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1005"}), (cm:Commentary {comment_id: "Case_Commentary:seed-1005-a"})
MERGE (c)-[:HAS_COMMENTARY]->(cm);

// ============================================================
// SCENARIO E — Rule 10 (merged case propagation).
// CASE-1006 was merged into CASE-1007. S-1008 was only ever on CASE-1006,
// so the rule should give them an APPEARS_IN_CASE edge to CASE-1007 marked
// merge_derived — while leaving every ETL-asserted APPEARS_IN_CASE edge on
// CASE-1007 untouched.
// ============================================================
MERGE (c6:Case {case_id: "CASE-1006"}) SET c6.status = "Closed", c6.source_system = "SEED", c6.stub = false;
MERGE (c7:Case {case_id: "CASE-1007"}) SET c7.status = "Open", c7.fraud_amount = 18000.0,
      c7.fraud_start_date = "2025-04-01", c7.fraud_end_date = "2025-09-30", c7.source_system = "SEED", c7.stub = false;
MATCH (a:Case {case_id: "CASE-1006"}), (b:Case {case_id: "CASE-1007"})
MERGE (a)-[r:MERGED_INTO_CASE]->(b) SET r.source_table = "Workfolder_MergeCases";
MERGE (s8:Subject {subject_id: "S-1008"}) SET s8.first_name = "Elena", s8.last_name = "Vasquez", s8.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1006"}), (s:Subject {subject_id: "S-1008"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c) SET r.is_primary = true, r.subject_role = "Subject",
      r.source_table = "Workfolder_SubjectsRelationship";

// ============================================================
// SCENARIO F — Rule 12 (SLAM wage corroboration), the date-verified path.
// S-1010's wage period (2025-05-01..2025-07-31) falls inside CASE-1007's
// fraud window (2025-04-01..2025-09-30), so the rule fires with
// wage_corroboration_verified = true and confidence High. Remove the
// case's fraud dates and it should degrade to verified=false / Medium —
// worth testing both ways.
// ============================================================
MERGE (s10:Subject {subject_id: "S-1010"}) SET s10.first_name = "Tomas", s10.last_name = "Ferreira", s10.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1007"}), (s:Subject {subject_id: "S-1010"})
MERGE (s)-[r:APPEARS_IN_CASE]->(c) SET r.is_primary = true, r.subject_role = "Subject";
MERGE (e2:Employer {employer_key: "FEIN:047654321"})
  SET e2.fein = "047654321", e2.employer_name = "Bay State Home Care", e2.source_system = "SEED";
MATCH (s:Subject {subject_id: "S-1010"}), (e:Employer {employer_key: "FEIN:047654321"})
MERGE (s)-[r:HAS_WAGE_RECORD_WITH {period_key: "2025|Q2|2025-05-01|2025-07-31"}]->(e)
  SET r.period_start = "2025-05-01", r.period_end = "2025-07-31", r.wage_amount = 15400.0,
      r.source_table = "Subject_SubjectWages";
MERGE (al7:Allegation {allegation_id: "AL-1007"})
  SET al7.allegation_type = "SLAM", al7.status = "Open", al7.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1007"}), (al:Allegation {allegation_id: "AL-1007"}) MERGE (c)-[:HAS_ALLEGATION]->(al);
MERGE (cm6:Commentary {comment_id: "Case_Commentary:seed-1007-a"})
  SET cm6.comment_text = "Tomas Ferreira collected benefits while working (AL-1007). Payroll records from Bay State Home Care cover the same months.",
      cm6.comment_type = "Investigator Note", cm6.case_id = "CASE-1007", cm6.source_system = "SEED";
MATCH (c:Case {case_id: "CASE-1007"}), (cm:Commentary {comment_id: "Case_Commentary:seed-1007-a"})
MERGE (c)-[:HAS_COMMENTARY]->(cm);

// ============================================================
// SCENARIO G — the rejection fixture (Section 5.5 / Principle 14).
// An investigator has already rejected the attribution of AL-1005 to
// S-1005. On the next pipeline run, the Extraction Stage may well propose
// it again from the narrative — and graph_load must SUPPRESS it, returning
// "previously flagged and rejected by ...", rather than writing it or
// going silent.
//
// This is synthetic on purpose: a real :Rejection can only be created by
// POST /reject_inference (Phase 9, not built), so without this fixture the
// suppression path in every rule and in graph_load is untestable.
// ============================================================
MERGE (rej:Rejection {
    relationship_type: "ALLEGATION_LIKELY_AGAINST_SUBJECT",
    from_key: "AL-1005",
    to_key:   "S-1005"
})
SET rej.status = "active", rej.rejected_by = "j.doe",
    rej.rejected_at = "2026-05-01", rej.rule_id = "Extraction_Stage",
    rej.reason = "Duplicate claim was filed by the landlord, not this subject.";
