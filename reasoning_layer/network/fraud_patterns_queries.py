"""
Owns: the Cypher text for the Fraud Patterns tab (reasoning_layer/network/
fraud_patterns.py). Query text only: all grouping, ordering, counting and
classification happen in fraud_patterns.py, in plain functions that can be
tested without Neo4j.

Every query is read-only and takes only $case_id. Path walked:

    (Case)-[:HAS_ALLEGATION_TYPE]->(AllegationType)-[:BELONGS_TO]->(Category)

HAS_ALLEGATION_TYPE is written by etl/graph_sync.py; the type and category
nodes are loaded from config/taxonomy.yaml by reasoning_layer/
taxonomy_registry.py. Case ids are compared with toString on the "other
case" side, exactly as the Similar Cases query does.
"""

# This case's own allegation types and the category each one belongs to.
OWN_TYPES = """
MATCH (c:Case {case_id: $case_id})-[:HAS_ALLEGATION_TYPE]->(t:AllegationType)
OPTIONAL MATCH (t)-[:BELONGS_TO]->(cat:Category)
RETURN t.code AS code, t.description AS description, cat.name AS category
ORDER BY code
"""

# Section 1 — other cases in any category this case belongs to. Returns
# every candidate; the display cap is applied in Python so the true totals
# are known. Own-history exclusion is the Similar Cases rule: drop c2 when
# c1's primary subject is also c2's primary subject. A people link counts
# unless its status is "rejected" (a missing status is active).
RELATED_CASES = """
MATCH (c1:Case {case_id: $case_id})-[:HAS_ALLEGATION_TYPE]->(own:AllegationType)
WITH c1, collect(DISTINCT own) AS own_types
UNWIND own_types AS own_type
MATCH (own_type)-[:BELONGS_TO]->(cat:Category)
WITH c1, own_types, collect(DISTINCT cat) AS own_categories
UNWIND own_categories AS cat
MATCH (cat)<-[:BELONGS_TO]-(:AllegationType)<-[:HAS_ALLEGATION_TYPE]-(c2:Case)
WHERE toString(c2.case_id) <> toString($case_id)
  AND NOT EXISTS {
        MATCH (c1)<-[ap1:APPEARS_IN_CASE]-(:Subject)-[ap2:APPEARS_IN_CASE]->(c2)
        WHERE ap1.is_primary = true AND ap2.is_primary = true
      }
WITH c1, c2, own_types, collect(DISTINCT cat.name) AS shared_categories
MATCH (c2)-[:HAS_ALLEGATION_TYPE]->(c2_type:AllegationType)
WITH c1, c2, own_types, shared_categories, collect(DISTINCT c2_type) AS c2_types
OPTIONAL MATCH (c1)<-[:APPEARS_IN_CASE]-(:Subject)
      -[link:SHARES_EMPLOYER_WITH|SHARES_ADDRESS_WITH|SHARES_ALIAS_PATTERN_WITH|IS_CO_SUBJECT_WITH]-
      (:Subject)-[:APPEARS_IN_CASE]->(c2)
WHERE coalesce(link.status, "active") <> "rejected"
WITH c2, own_types, shared_categories, c2_types, count(link) AS people_link_count
RETURN c2.case_id          AS case_id,
       c2.complaint_number AS complaint_no,
       c2.status           AS status,
       c2.fraud_amount     AS fraud_amount,
       c2.opened_date      AS date_opened,
       [t IN c2_types | t.description]            AS case_types,
       shared_categories,
       any(t IN c2_types WHERE t IN own_types)    AS also_same_type,
       people_link_count > 0                      AS also_linked_through_people
ORDER BY case_id
"""

# Sections 2 and 4 — every case in each of this case's categories (this case
# included), one row per category and case. Exposure and trend are both
# computed from these rows in Python.
CATEGORY_CASES = """
MATCH (:Case {case_id: $case_id})-[:HAS_ALLEGATION_TYPE]->(:AllegationType)-[:BELONGS_TO]->(cat:Category)
WITH DISTINCT cat
MATCH (cat)<-[:BELONGS_TO]-(:AllegationType)<-[:HAS_ALLEGATION_TYPE]-(c:Case)
RETURN DISTINCT cat.name AS category,
       c.case_id         AS case_id,
       c.fraud_amount    AS fraud_amount,
       c.opened_date     AS opened_date
ORDER BY category, case_id
"""

# Section 3 — closed cases (those with a closed_date) in each of this case's
# categories, excluding this case, with every field Rule 7 inspects for a
# guilty outcome: allegation outcome, allegation status, case disposition.
CLOSED_CASE_OUTCOMES = """
MATCH (:Case {case_id: $case_id})-[:HAS_ALLEGATION_TYPE]->(:AllegationType)-[:BELONGS_TO]->(cat:Category)
WITH DISTINCT cat
MATCH (cat)<-[:BELONGS_TO]-(:AllegationType)<-[:HAS_ALLEGATION_TYPE]-(c:Case)
WHERE toString(c.case_id) <> toString($case_id)
  AND coalesce(trim(toString(c.closed_date)), "") <> ""
WITH DISTINCT cat, c
OPTIONAL MATCH (c)-[:HAS_ALLEGATION]->(al:Allegation)
RETURN cat.name AS category,
       c.case_id AS case_id,
       c.disposition AS disposition,
       collect(al.outcome) AS outcomes,
       collect(al.status)  AS statuses
ORDER BY category, case_id
"""
