"""
Tests for reasoning_layer/fraud_network.py's _build_edges — specifically
the AI-28 completeness fix: MEMBER_OF_FRAUD_NETWORK edges (Subject ->
FraudNetwork, Rule_02/04/06/09's family) used to be excluded from
rejectable/subject_id_a/subject_id_b/match_id entirely, because the old
`subject_to_subject` check only recognized Subject-to-Subject edges. A
caller reading only GET /fraud_network's graph.edges (rather than a
separate call to GET /rule_audit) had no way to target one specific
network member's own membership for reject/revert.
"""

from __future__ import annotations

import importlib.util
import sys
import types


def _install_external_import_stubs() -> None:
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

from reasoning_layer.fraud_network import _build_edges  # noqa: E402
from reasoning_layer.rejection import build_match_id, decode_match_id  # noqa: E402


def _member_edge(subject_key: str, network_key_composite: str, rule_id: str = "Rule_02_Employer_Fraud_Network"):
    ref_to_id = {"src": f"Subject:{subject_key}", "tgt": f"FraudNetwork:{network_key_composite}"}
    node_by_id = {
        f"Subject:{subject_key}": {"label": "Subject", "key": subject_key},
        f"FraudNetwork:{network_key_composite}": {"label": "FraudNetwork", "key": network_key_composite},
    }
    raw_edges = [
        {
            "ref": "edge-1",
            "source_ref": "src",
            "target_ref": "tgt",
            "type": "MEMBER_OF_FRAUD_NETWORK",
            "properties": {"source_rule": rule_id, "confidence": "High", "status": "active"},
        }
    ]
    return _build_edges(raw_edges, ref_to_id, node_by_id)[0]


def test_member_to_network_edge_is_rejectable():
    edge = _member_edge("658636801", "Employer:FEIN:047821334")
    assert edge["rejectable"] is True


def test_member_to_network_edge_gets_correct_subject_ids():
    edge = _member_edge("658636801", "Employer:FEIN:047821334")
    assert edge["subject_id_a"] == "658636801"
    assert edge["subject_id_b"] == "Employer:FEIN:047821334"
    assert edge["rule_id"] == "Rule_02_Employer_Fraud_Network"


def test_member_to_network_edge_match_id_matches_rule_audit_encoding():
    """Byte-identical to what rule_audit.py would build for the same
    member's row — the whole point of this fix."""
    edge = _member_edge("658636801", "Employer:FEIN:047821334")
    expected = build_match_id("Rule_02_Employer_Fraud_Network", "658636801", "Employer:FEIN:047821334")
    assert edge["match_id"] == expected
    assert decode_match_id(edge["match_id"]) == (
        "Rule_02_Employer_Fraud_Network",
        "658636801",
        "Employer:FEIN:047821334",
    )


def test_two_members_of_the_same_network_get_distinct_match_ids():
    """The whole point: John and Kevin, same network, must be
    individually targetable — not collapsed to one token."""
    john_edge = _member_edge("658636801", "Employer:FEIN:047821334")
    kevin_edge = _member_edge("658653191", "Employer:FEIN:047821334")
    assert john_edge["match_id"] != kevin_edge["match_id"]
    assert decode_match_id(kevin_edge["match_id"])[1] == "658653191"


def test_etl_sourced_member_edge_has_no_rule_id_and_is_not_rejectable():
    """A membership edge with no source_rule (shouldn't happen for
    MEMBER_OF_FRAUD_NETWORK in practice, but the guard must hold) must
    not be rejectable and must carry no match_id."""
    ref_to_id = {"src": "Subject:658636801", "tgt": "FraudNetwork:Employer:FEIN:047821334"}
    node_by_id = {
        "Subject:658636801": {"label": "Subject", "key": "658636801"},
        "FraudNetwork:Employer:FEIN:047821334": {"label": "FraudNetwork", "key": "Employer:FEIN:047821334"},
    }
    raw_edges = [
        {
            "ref": "edge-1",
            "source_ref": "src",
            "target_ref": "tgt",
            "type": "MEMBER_OF_FRAUD_NETWORK",
            "properties": {"confidence": "High", "status": "active"},  # no source_rule
        }
    ]
    edge = _build_edges(raw_edges, ref_to_id, node_by_id)[0]
    assert edge["rejectable"] is False
    assert "match_id" not in edge


def test_subject_to_subject_edges_still_work_unchanged():
    """Regression guard: the original subject-to-subject path (Rule_01/
    03/05/07/10) must be completely unaffected by this change."""
    ref_to_id = {"src": "Subject:658636801", "tgt": "Subject:658653191"}
    node_by_id = {
        "Subject:658636801": {"label": "Subject", "key": "658636801"},
        "Subject:658653191": {"label": "Subject", "key": "658653191"},
    }
    raw_edges = [
        {
            "ref": "edge-1",
            "source_ref": "src",
            "target_ref": "tgt",
            "type": "SHARES_EMPLOYER_WITH",
            "properties": {"source_rule": "Rule_01_Shared_Employer", "confidence": "High", "status": "active"},
        }
    ]
    edge = _build_edges(raw_edges, ref_to_id, node_by_id)[0]
    assert edge["rejectable"] is True
    assert edge["subject_id_a"] == "658636801"
    assert edge["subject_id_b"] == "658653191"
    assert edge["match_id"] == build_match_id("Rule_01_Shared_Employer", "658636801", "658653191")


def test_non_membership_subject_to_network_edge_type_is_not_treated_as_membership():
    """Only relationship_type == MEMBER_OF_FRAUD_NETWORK triggers the
    member_to_network path — a hypothetical different Subject->FraudNetwork
    edge type must not be silently treated as rejectable membership."""
    ref_to_id = {"src": "Subject:658636801", "tgt": "FraudNetwork:Employer:FEIN:047821334"}
    node_by_id = {
        "Subject:658636801": {"label": "Subject", "key": "658636801"},
        "FraudNetwork:Employer:FEIN:047821334": {"label": "FraudNetwork", "key": "Employer:FEIN:047821334"},
    }
    raw_edges = [
        {
            "ref": "edge-1",
            "source_ref": "src",
            "target_ref": "tgt",
            "type": "SOME_OTHER_RELATIONSHIP",
            "properties": {"source_rule": "Rule_99_Hypothetical", "confidence": "High", "status": "active"},
        }
    ]
    edge = _build_edges(raw_edges, ref_to_id, node_by_id)[0]
    assert edge["rejectable"] is False
    assert "match_id" not in edge
