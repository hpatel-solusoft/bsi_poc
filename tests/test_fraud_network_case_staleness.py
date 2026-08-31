"""
AI-31 — confirms (without changing any code) the claim
reasoning_layer/rejection.py's module docstring and api/models.py's
FraudNetworkResponse docstring both make: GET /fraud_network needs no
query change to surface (:Case).last_inference_change_at, because
reasoning_layer/fraud_network.py's _CASE_SUBGRAPH_QUERY already returns
full properties(case_node) as part of graph.nodes, and _build_nodes
passes properties(n) through unfiltered for every node — the Case node
included.

Same reasoning proves the AI-30 auto_invalidated/invalidated_by_rule_id
pair cascade.py sets directly on a MEMBER_OF_FRAUD_NETWORK relationship
(no field-name prefix — see cascade.py's module docstring) also reaches
this endpoint with no query change, via the identical properties(r)
pass-through _build_edges already does.

This is a pure regression guard: if a future change to
_CASE_SUBGRAPH_QUERY, _build_nodes, or _build_edges ever narrows their
RETURN/property list back down to a fixed set of fields (the same
mistake reasoning_layer/rule_audit.py's Rule_08/Rule_13 queries made,
fixed alongside this test), this test starts failing immediately instead
of the gap going unnoticed until a frontend engineer asks where the
staleness field went.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from typing import Any, Dict
from unittest import mock


def _install_external_import_stubs() -> None:
    """Same minimal stubs the other offline reasoning_layer test files
    install, so this file can also run in a sandbox with no neo4j wheel."""
    if importlib.util.find_spec("neo4j") is None:
        neo4j = types.ModuleType("neo4j")

        class _GraphDatabase:
            @staticmethod
            def driver(*args, **kwargs):
                return types.SimpleNamespace(verify_connectivity=lambda: None, close=lambda: None)

        neo4j.Driver = object
        neo4j.GraphDatabase = _GraphDatabase
        neo4j.Session = object

        neo4j_exceptions = types.ModuleType("neo4j.exceptions")

        class Neo4jError(Exception):
            pass

        neo4j_exceptions.Neo4jError = Neo4jError
        neo4j_exceptions.AuthError = type("AuthError", (Neo4jError,), {})
        neo4j_exceptions.ServiceUnavailable = type("ServiceUnavailable", (Neo4jError,), {})
        neo4j.exceptions = neo4j_exceptions

        sys.modules.setdefault("neo4j", neo4j)
        sys.modules.setdefault("neo4j.exceptions", neo4j_exceptions)


_install_external_import_stubs()

from reasoning_layer.network import fraud_network  # noqa: E402


class FakeResult:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record


class FakeSession:
    def __init__(self, record):
        self._record = record

    def run(self, query, **params):
        return FakeResult(self._record)

    def close(self):
        pass


def fake_session_cm(session):
    @contextmanager
    def _cm(*args, **kwargs):
        yield session

    return _cm


def _raw_subgraph_record() -> Dict[str, Any]:
    """One Case node (already touched by an earlier reject_inference call
    — carrying last_inference_change_at) and one Subject with a
    MEMBER_OF_FRAUD_NETWORK edge that AI-30's cascade auto-invalidated."""
    return {
        "nodes": [
            {
                "ref": "case-elem-1",
                "labels": ["Case"],
                "properties": {
                    "case_id": "CASE-1",
                    # AI-31 — the field under test.
                    "last_inference_change_at": "2026-08-05T12:00:00+00:00",
                },
                "is_case_subject": False,
            },
            {
                "ref": "subj-elem-1",
                "labels": ["Subject"],
                "properties": {"subject_id": "S1", "first_name": "Jane", "last_name": "Doe"},
                "is_case_subject": True,
            },
            {
                "ref": "network-elem-1",
                "labels": ["FraudNetwork"],
                "properties": {
                    "network_type": "Employer",
                    "network_key": "FEIN:12345",
                    "key": "Employer:FEIN:12345",
                },
                "is_case_subject": False,
            },
        ],
        "relationships": [
            {
                "ref": "rel-elem-1",
                "type": "MEMBER_OF_FRAUD_NETWORK",
                "source_ref": "subj-elem-1",
                "target_ref": "network-elem-1",
                "properties": {
                    "source_rule": "Rule_02_Employer_Fraud_Network",
                    "confidence": "High",
                    "status": "rejected",
                    # AI-30 — the pair under test, set by cascade.py
                    # directly on the relationship, no field-name prefix.
                    "auto_invalidated": True,
                    "invalidated_by_rule_id": "Rule_01_Shared_Employer",
                },
            },
        ],
    }


def test_case_node_staleness_timestamp_reaches_fraud_network_graph_unmodified():
    session = FakeSession(_raw_subgraph_record())
    with mock.patch.object(fraud_network, "get_session", fake_session_cm(session)):
        envelope = fraud_network.get_fraud_network("CASE-1")

    nodes = envelope["result"]["graph"]["nodes"]
    case_node = next(n for n in nodes if n["label"] == "Case")
    assert case_node["properties"]["last_inference_change_at"] == "2026-08-05T12:00:00+00:00"


def test_membership_auto_invalidation_pair_reaches_fraud_network_graph_unmodified():
    session = FakeSession(_raw_subgraph_record())
    with mock.patch.object(fraud_network, "get_session", fake_session_cm(session)):
        envelope = fraud_network.get_fraud_network("CASE-1")

    edges = envelope["result"]["graph"]["edges"]
    membership_edge = next(e for e in edges if e["relationship_type"] == "MEMBER_OF_FRAUD_NETWORK")
    assert membership_edge["properties"]["auto_invalidated"] is True
    assert membership_edge["properties"]["invalidated_by_rule_id"] == "Rule_01_Shared_Employer"
    # And it's still individually rejectable/revertable via the same
    # match_id contract as any other membership edge (AI-28) — AI-30/
    # AI-31 add read-only audit fields, they don't change targetability.
    assert membership_edge["rejectable"] is True
    assert membership_edge["subject_id_a"] == "S1"
    assert membership_edge["subject_id_b"] == "Employer:FEIN:12345"
