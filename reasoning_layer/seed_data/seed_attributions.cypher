// BSI Phase 2 — hand-written attribution edges for LLM-free Wave 2 testing.
//
// The Extraction Stage (an LLM call) normally produces
// ALLEGATION_LIKELY_AGAINST_SUBJECT edges by reading the commentary in
// poc_seed_data.cypher. Every Wave 2 rule is gated on those edges. This
// file writes the same edges by hand, so Wave 2 can be validated WITHOUT
// an OpenAI key — or so a Wave 2 rule failure can be isolated from an
// extraction-quality problem.
//
// Apply this INSTEAD OF running the real Extraction Stage, not in addition
// to it — both write the same edges. Apply poc_seed_data.cypher first.
//
// The confidence values below match what the seed commentary should
// produce if the LLM reads it correctly (explicit naming -> High), so a
// Wave 2 run on these edges and a Wave 2 run on real extraction output
// should agree. Where they diverge, the divergence is an extraction
// quality signal worth looking at.

// Scenario A — CheckSplit (Rule 9), both co-subjects attributed.
MATCH (al:Allegation {allegation_id: "AL-1001"}), (s:Subject {subject_id: "S-1001"})
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
  SET r.confidence = "High", r.source_rule = "Extraction_Stage", r.status = "active",
      r.rationale = "seed: commentary names both signer and submitter";
MATCH (al:Allegation {allegation_id: "AL-1001"}), (s:Subject {subject_id: "S-1002"})
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
  SET r.confidence = "High", r.source_rule = "Extraction_Stage", r.status = "active";

// Scenario A — prior guilty (Rule 7).
MATCH (al:Allegation {allegation_id: "AL-0900"}), (s:Subject {subject_id: "S-1001"})
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
  SET r.confidence = "High", r.source_rule = "Extraction_Stage", r.status = "active";

// Scenario B — employer network (Rule 2).
MATCH (al:Allegation {allegation_id: "AL-1003"}), (s:Subject {subject_id: "S-1001"})
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
  SET r.confidence = "High", r.source_rule = "Extraction_Stage", r.status = "active";
MATCH (al:Allegation {allegation_id: "AL-1002"}), (s:Subject {subject_id: "S-1003"})
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
  SET r.confidence = "High", r.source_rule = "Extraction_Stage", r.status = "active";

// Scenario C — address network (Rule 4), different cases.
MATCH (al:Allegation {allegation_id: "AL-1004"}), (s:Subject {subject_id: "S-1004"})
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
  SET r.confidence = "High", r.source_rule = "Extraction_Stage", r.status = "active";
// NOTE: AL-1005 -> S-1005 is deliberately NOT written here — it is the
// rejected attribution (Scenario G). Writing it would defeat the rejection
// test. Rule 4 therefore needs the OTHER side; add AL-1005->S-1005 only to
// confirm the rejection suppresses it.

// Scenario D — identity network (Rule 6).
MATCH (al:Allegation {allegation_id: "AL-1006"}), (s:Subject {subject_id: "S-1006"})
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
  SET r.confidence = "High", r.source_rule = "Extraction_Stage", r.status = "active";

// Scenario F — SLAM wage corroboration (Rule 12).
MATCH (al:Allegation {allegation_id: "AL-1007"}), (s:Subject {subject_id: "S-1010"})
MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(s)
  SET r.confidence = "High", r.source_rule = "Extraction_Stage", r.status = "active";
