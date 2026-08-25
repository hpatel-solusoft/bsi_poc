"""
Owns: the Cypher MERGE/SET statements etl/graph_sync.py's _tx_load uses to
write one case's canonical dict into Neo4j — Case, MergedInto, Allegations,
Subjects, Addresses, Aliases, Employers, Wage records, Commentary, and
Co-Subject pairs. Every asserted relationship carries source_table +
retrieved_at (Section 3.3 provenance); nodes additionally carry
source_system so an investigator can tell an ETL-sourced node from a
rule-created one (:FraudNetwork) without inspecting its edges.

Split out of graph_sync.py verbatim, same rationale as
reasoning_layer/rules_fired_queries.py and friends: this file owns query
text only, not the load orchestration (retry, transaction boundaries,
prune/reconcile) that stays in graph_sync.py, which imports each Q_*
constant from here.

Every query here is additive only (a MERGE) — the RECONCILE section back
in graph_sync.py is what retires an edge these queries stop reporting.
"""

# ============================================================
# LOAD — canonical dict -> Neo4j
# Section 3.1 node labels, 3.2 relationship types, 3.3 provenance.
# Every asserted relationship carries source_table + retrieved_at.
# Nodes additionally carry source_system, so an investigator can tell an
# ETL-sourced node from a rule-created one (:FraudNetwork) without
# inspecting its edges.
# ============================================================

Q_CASE = """
MERGE (c:Case {case_id: $case.case_id})
SET c.complaint_number  = $case.complaint_number,
    c.status            = $case.status,
    c.fraud_amount      = $case.fraud_amount,
    c.is_fasttrack      = $case.is_fasttrack,
    c.is_dta_case       = $case.is_dta_case,
    c.disposition       = $case.disposition,
    c.fraud_start_date  = $case.fraud_start_date,
    c.fraud_end_date    = $case.fraud_end_date,
    c.opened_date       = $case.opened_date,
    c.closed_date       = $case.closed_date,
    c.source_system     = $source_system,
    c.source_table      = $case.source_table,
    c.retrieved_at      = $case.retrieved_at,
    c.stub              = false
RETURN 1 AS n
"""

Q_MERGED_INTO = """
MATCH (c1:Case {case_id: $case_id})
UNWIND $target_case_ids AS target_id
MERGE (c2:Case {case_id: target_id})
  ON CREATE SET c2.source_system = $source_system,
                c2.source_table  = "Workfolder_MergeCases",
                c2.retrieved_at  = $retrieved_at,
                c2.stub          = true
MERGE (c1)-[r:MERGED_INTO_CASE]->(c2)
SET r.source_table = "Workfolder_MergeCases", r.retrieved_at = $retrieved_at
RETURN count(r) AS n
"""
# stub=true marks a Case node created only because another case merged
# into it, before that case's own ETL run has happened. Rule 10 reads it
# either way; the flag exists so nobody mistakes an empty Case node for a
# case AppWorks has no data on.

Q_ALLEGATIONS = """
MATCH (c:Case {case_id: $case_id})
UNWIND $allegations AS a
MERGE (al:Allegation {allegation_id: a.allegation_id})
SET al.allegation_type = a.allegation_type,
    al.status          = a.status,
    al.record_status   = a.record_status,
    al.norris_code     = a.norris_code,
    al.outcome         = a.outcome,
    al.date_closed     = a.date_closed,
    al.comment_text    = a.comment_text,
    al.source_system   = $source_system,
    al.source_table    = a.source_table,
    al.retrieved_at    = a.retrieved_at
MERGE (c)-[r:HAS_ALLEGATION]->(al)
SET r.source_table = "Allegations_Workfolder_Id", r.retrieved_at = a.retrieved_at,
    r._prune_pending = null, r._prune_flagged_at = null
RETURN count(al) AS n
"""

Q_SUBJECTS = """
MATCH (c:Case {case_id: $case_id})
UNWIND $subjects AS s
MERGE (subj:Subject {subject_id: s.subject_id})
SET subj.first_name    = s.first_name,
    subj.last_name     = s.last_name,
    subj.company_name  = s.company_name,
    subj.fein          = s.fein,
    subj.subject_type  = s.subject_type,
    subj.source_system = $source_system,
    subj.source_table  = s.source_table,
    subj.retrieved_at  = s.retrieved_at
MERGE (subj)-[r:APPEARS_IN_CASE]->(c)
SET r.subject_role = s.subject_role,
    r.is_primary   = s.is_primary,
    r.source_table = "Workfolder_SubjectsRelationship",
    r.retrieved_at = s.retrieved_at,
    r._prune_pending = null, r._prune_flagged_at = null
RETURN count(subj) AS n
"""

Q_ADDRESSES = """
UNWIND $rows AS row
MATCH (s:Subject {subject_id: row.subject_id})
MERGE (addr:Address {address_key: row.address_key})
SET addr.street            = row.street,
    addr.city              = row.city,
    addr.state             = row.state,
    addr.zip               = row.zip,
    addr.street_normalized = row.street_normalized,
    addr.source_system     = $source_system,
    addr.source_table      = "Subject_Address",
    addr.retrieved_at      = $retrieved_at
MERGE (s)-[r:HAS_ADDRESS]->(addr)
SET r.source_table = "Subject_Address", r.retrieved_at = $retrieved_at,
    r._prune_pending = null, r._prune_flagged_at = null
RETURN count(r) AS n
"""

Q_ALIASES = """
UNWIND $rows AS row
MATCH (s:Subject {subject_id: row.subject_id})
MERGE (al:Alias {alias_value: row.alias_value})
SET al.source_system = $source_system,
    al.source_table  = "Subject_Alias",
    al.retrieved_at  = $retrieved_at
MERGE (s)-[r:HAS_ALIAS]->(al)
SET r.source_table = "Subject_Alias", r.retrieved_at = $retrieved_at,
    r._prune_pending = null, r._prune_flagged_at = null
RETURN count(r) AS n
"""

# coalesce(row.x, e.x) on every Employer property: a wage-sourced Employer
# node may be created with an AppWorks id and no FEIN, and a later
# job-sourced row may supply the FEIN for the same key. Overwriting with
# NULL would erase it. Never blank out a field the graph already knows.
Q_EMPLOYERS = """
UNWIND $rows AS row
MATCH (s:Subject {subject_id: row.subject_id})
MERGE (e:Employer {employer_key: row.employer_key})
SET e.employer_name = coalesce(row.employer_name, e.employer_name),
    e.fein          = coalesce(row.fein, e.fein),
    e.employer_fid  = coalesce(row.employer_fid, e.employer_fid),
    e.source_system = $source_system,
    e.source_table  = "Subject_Job",
    e.retrieved_at  = $retrieved_at
MERGE (s)-[r:EMPLOYED_BY]->(e)
SET r.start_date   = row.start_date,
    r.end_date     = row.end_date,
    r.source_table = "Subject_Job",
    r.retrieved_at = $retrieved_at,
    r._prune_pending = null, r._prune_flagged_at = null
RETURN count(r) AS n
"""

Q_WAGES = """
UNWIND $rows AS row
MATCH (s:Subject {subject_id: row.subject_id})
MERGE (e:Employer {employer_key: row.employer_key})
SET e.employer_name = coalesce(row.employer_name, e.employer_name),
    e.fein          = coalesce(row.fein, e.fein),
    e.employer_fid  = coalesce(row.employer_fid, e.employer_fid),
    e.source_system = $source_system,
    e.retrieved_at  = $retrieved_at
MERGE (s)-[r:HAS_WAGE_RECORD_WITH {period_key: row.period_key}]->(e)
SET r.period_start = row.period_start,
    r.period_end   = row.period_end,
    r.wage_year    = row.wage_year,
    r.wage_quarter = row.wage_quarter,
    r.wage_amount  = row.wage_amount,
    r.source_table = "Subject_SubjectWages",
    r.retrieved_at = $retrieved_at,
    r._prune_pending = null, r._prune_flagged_at = null
RETURN count(r) AS n
"""

# HAS_COMMENTARY appears in the reference doc's own Rule 14 worked example
# ((Case)-[:HAS_COMMENTARY]->(:Commentary)) but is absent from Section
# 3.2's relationship table. Loaded here from all three narrative sources
# Section 5.3 Step 3 names — Case commentary, Subject_Comment, and the
# Allegation comment field. Flagged in GAP_ANALYSIS.md as a relationship
# type the reference doc should state explicitly rather than leave
# implicit in an example.
Q_COMMENTARY = """
UNWIND $rows AS row
MERGE (comm:Commentary {comment_id: row.comment_id})
SET comm.comment_text  = row.comment_text,
    comm.comment_type  = row.comment_type,
    comm.created_date  = row.created_date,
    comm.case_id       = $case_id,
    comm.source_system = $source_system,
    comm.source_table  = row.source_table,
    comm.retrieved_at  = $retrieved_at
WITH comm, row
// Attach each comment to whichever of Case/Subject/Allegation its attach_to
// names. Written with OPTIONAL MATCH + FOREACH rather than CALL subqueries:
// the bare `CALL { WITH ... }` form is deprecated in Neo4j 5.23+ (it wants
// the scoped `CALL (comm,row) {}` form, which in turn is not available on
// older 5.x). FOREACH-over-a-conditional-list is the one conditional-write
// idiom that is idempotent AND valid on every 4.x/5.x version, so it is the
// safe choice while the target Neo4j version is still settling. Two of the
// three OPTIONAL MATCHes bind null for any given row; the matching FOREACH
// writes exactly one HAS_COMMENTARY edge, the other two are no-ops.
OPTIONAL MATCH (c:Case {case_id: row.attach_id})
  WHERE row.attach_to = "case"
OPTIONAL MATCH (s:Subject {subject_id: row.attach_id})
  WHERE row.attach_to = "subject"
OPTIONAL MATCH (al:Allegation {allegation_id: row.attach_id})
  WHERE row.attach_to = "allegation"
FOREACH (x IN CASE WHEN c IS NOT NULL THEN [c] ELSE [] END |
    MERGE (x)-[r:HAS_COMMENTARY]->(comm)
    SET r.source_table = row.source_table, r.retrieved_at = $retrieved_at,
        r._prune_pending = null, r._prune_flagged_at = null)
FOREACH (x IN CASE WHEN s IS NOT NULL THEN [s] ELSE [] END |
    MERGE (x)-[r:HAS_COMMENTARY]->(comm)
    SET r.source_table = row.source_table, r.retrieved_at = $retrieved_at,
        r._prune_pending = null, r._prune_flagged_at = null)
FOREACH (x IN CASE WHEN al IS NOT NULL THEN [al] ELSE [] END |
    MERGE (x)-[r:HAS_COMMENTARY]->(comm)
    SET r.source_table = row.source_table, r.retrieved_at = $retrieved_at,
        r._prune_pending = null, r._prune_flagged_at = null)
RETURN count(DISTINCT comm) AS n
"""

Q_CO_SUBJECTS = """
UNWIND $pairs AS pair
MATCH (a:Subject {subject_id: pair.a})
MATCH (b:Subject {subject_id: pair.b})
MERGE (a)-[r:IS_CO_SUBJECT_WITH]-(b)
SET r.case_id      = $case_id,
    r.source_table = "Workfolder_SubjectsRelationship",
    r.retrieved_at = $retrieved_at,
    r._prune_pending = null, r._prune_flagged_at = null
RETURN count(r) AS n
"""
