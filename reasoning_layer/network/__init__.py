"""
Case-network graph views — features that show an investigator a
subject/case's connections rather than a single rule finding.

fraud_network.py  - GET /fraud_network/{case_id}: the whole subgraph
                     for one case, shaped into nodes/edges/networks
                     (D3 contract). Depends on reasoning_layer.rejection
                     (build_match_id) and reasoning_layer.neo4j_client,
                     both outside this package — unaffected by the move,
                     since imports here are absolute.
similar_cases.py   - find_structural_matches: the archive-search half of
                     the /similar_cases route (Section 8.3 AI-14),
                     called from api/pipeline_execution.py and, lazily,
                     from reasoning_layer/copilot_templates.py's
                     get_structural_similar_cases tool.
"""
