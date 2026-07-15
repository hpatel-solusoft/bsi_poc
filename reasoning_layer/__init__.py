"""
Layer 4 — Reasoning Layer (BSI Phase 2 Python Implementation Reference,
Section 1). Neo4j-backed graph inference over the case network.

Owns: the Neo4j driver connection (neo4j_client.py), the reasoning
pipeline orchestrator (pipeline.py), and the Cypher rule library
(rules/*.cypher).

A peer to Layer 3 (appworks/), not a replacement for it. Reached only
via semantic_layer/dispatcher.py — exactly like appworks_services.py —
so Principle 2 ("all tool calls are routed through the dispatcher")
holds without exception. Nothing in api/ or agent_service/ imports from
this package directly; every entry point here is registered in
config/manifest.yaml as a python_function and resolved dynamically by
the dispatcher at call time.

Layer 4 does not call Layer 3, and Layer 3 does not call Layer 4
(Section 1). Where an agent needs both AppWorks and graph data, it makes
two separate dispatcher calls — the layers themselves stay decoupled.
"""
