// BSI Phase 2 — Neo4j graph schema: constraints + indexes.
//
// Per the Python Implementation Reference, Section 3.4. Node labels and
// relationship types (Sections 3.1/3.2) need no DDL in a property graph —
// they come into existence the first time ETL or a rule writes them. Only
// uniqueness constraints and the rule match-key indexes are declared up
// front.
//
// Idempotent (IF NOT EXISTS throughout) — safe to run on every deploy.
// Apply with:  python -m reasoning_layer.apply_schema
// or:          cat reasoning_layer/schema.cypher | cypher-shell -u neo4j -p ...

// ---------------------------------------------------------------
// Uniqueness constraints — Section 3.4 verbatim, plus the keys the
// ETL and the rule library actually MERGE on. A MERGE on an
// unconstrained property is a full label scan AND a race: two
// concurrent ingests can each create their own :Employer node for the
// same employer, after which Rule 1 silently stops matching across
// them. The constraint is what makes idempotency real rather than
// merely intended.
// ---------------------------------------------------------------
CREATE CONSTRAINT subject_id_unique IF NOT EXISTS
  FOR (s:Subject) REQUIRE s.subject_id IS UNIQUE;

CREATE CONSTRAINT case_id_unique IF NOT EXISTS
  FOR (c:Case) REQUIRE c.case_id IS UNIQUE;

CREATE CONSTRAINT allegation_id_unique IF NOT EXISTS
  FOR (a:Allegation) REQUIRE a.allegation_id IS UNIQUE;

// employer_key / address_key are schema extensions (see
// etl/normalizers.py for why each exists and what it replaces). Both
// are flagged in etl/GAP_ANALYSIS.md.
CREATE CONSTRAINT employer_key_unique IF NOT EXISTS
  FOR (e:Employer) REQUIRE e.employer_key IS UNIQUE;

CREATE CONSTRAINT address_key_unique IF NOT EXISTS
  FOR (a:Address) REQUIRE a.address_key IS UNIQUE;

CREATE CONSTRAINT alias_value_unique IF NOT EXISTS
  FOR (al:Alias) REQUIRE al.alias_value IS UNIQUE;

// :Commentary had no id in Section 3.1, which forced the previous ETL to
// CREATE rather than MERGE and duplicated every comment on every re-sync.
// comment_id (etl/normalizers.commentary_id) is a deterministic id, so a
// re-sync of the same case is a no-op instead of an accumulation.
CREATE CONSTRAINT commentary_id_unique IF NOT EXISTS
  FOR (c:Commentary) REQUIRE c.comment_id IS UNIQUE;

// A FraudNetwork is identified by (type, key): the same employer can
// anchor only one Employer-type network, one case only one CheckSplit
// network. Without this, a re-run creates a second network node and the
// investigator sees the same network twice.
CREATE CONSTRAINT fraud_network_unique IF NOT EXISTS
  FOR (n:FraudNetwork) REQUIRE (n.network_type, n.network_key) IS UNIQUE;

CREATE CONSTRAINT inference_rule_unique IF NOT EXISTS
  FOR (r:InferenceRule) REQUIRE r.rule_id IS UNIQUE;

// ---------------------------------------------------------------
// Indexes on rule match keys — the fields the rules filter or join on.
// ---------------------------------------------------------------
CREATE INDEX employer_fein IF NOT EXISTS
  FOR (e:Employer) ON (e.fein);

// Section 3.4's composite index, kept as specified. The rules match on
// address_key (constrained above); this composite still serves any
// consumer querying by the raw four fields — e.g. the Copilot's
// get_subject_connections template, or an investigator in Neo4j Browser.
CREATE INDEX address_normalized IF NOT EXISTS
  FOR (a:Address) ON (a.street, a.city, a.state, a.zip);

CREATE INDEX alias_value IF NOT EXISTS
  FOR (al:Alias) ON (al.alias_value);

// Rules 2, 4, 6, 9 and 12 all filter allegations by type and status; the
// rules_fired assembly filters by case status. Unindexed, each is a label
// scan on every pipeline run.
CREATE INDEX allegation_type IF NOT EXISTS
  FOR (a:Allegation) ON (a.allegation_type);

CREATE INDEX allegation_status IF NOT EXISTS
  FOR (a:Allegation) ON (a.status);

CREATE INDEX case_status IF NOT EXISTS
  FOR (c:Case) ON (c.status);

// The rejection guard runs inside every single rule (Section 5.5:
// "every future pipeline run checks for an existing rejection before
// re-asserting the same fact"). It is the most frequently executed
// lookup in the entire library and must not be a scan.
CREATE INDEX rejection_lookup IF NOT EXISTS
  FOR (r:Rejection) ON (r.relationship_type, r.from_key, r.to_key);
