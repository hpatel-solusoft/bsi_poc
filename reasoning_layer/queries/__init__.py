"""
Cypher query text, split out of their consuming logic files (Section 6D:
this package owns query strings only, never business logic). Grouped
here because each was a single large embedded string/dict inside a
file whose real job is something else — parsing, shaping, or auditing
what the query returns, not the query text itself.

rules_fired_queries.py    -> reasoning_layer/rules_fired.py
rule_audit_queries.py     -> reasoning_layer/rule_audit.py
report_generation_queries.py -> reasoning_layer/reports/report_generation.py
fraud_network_query.py    -> reasoning_layer/fraud_network.py

Each module here is imported by exactly the one file above — nothing
else in the codebase depends on these paths.
"""
