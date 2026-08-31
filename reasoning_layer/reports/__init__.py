"""
Case report assembly (D4/D5 report contract) — everything
api/services/report_service.py needs to build the generated-report
markdown/PDF, grouped into its own package since it is a cohesive,
independently-testable slice of reasoning_layer with exactly one
external caller.

report_generation.py     - assembles the D5 Related Network section
                            (Cypher-backed) plus other report sections
decision_log.py           - builds the Decision & Override Log section
                            from reject/revert/cascade history
report_llm_context.py     - takes the assembled sections and produces
                            the final LLM prompt context for the report
                            narrative (build_report_generation_prompt)

Depends on reasoning_layer.neo4j_client and reasoning_layer.rules_fired_view
(both stay at the reasoning_layer top level) — those absolute imports are
unaffected by this package's own location.
"""
