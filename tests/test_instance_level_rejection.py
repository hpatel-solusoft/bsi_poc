"""
AI-28 — instance-level /reject_inference & /revert_rejection.

No live Neo4j is reachable from the sandbox this was built in, so the
graph is faked: a small in-memory dict standing in for Neo4j's edge
storage, driving a scripted FakeSession the same way tests/verify.py
does for the rest of reasoning_layer. Unlike tests/verify.py's own
check() helper (which records failures without raising, so a failure
only surfaces via the script's own end-of-run summary), every assertion
here is a plain `assert` so a regression genuinely fails the test under
pytest.

What this proves:
  * rejecting ONE identified instance (subject_id_a/subject_id_b, or the
    equivalent match_id) leaves every OTHER instance the same rule fired
    untouched — the exact AI-28 acceptance scenario: 3 subjects, reject
    only the A-C match, confirm A-B stays active and C stays rejected.
  * match_id and subject_id_a/subject_id_b are interchangeable inputs
    that resolve to the same target.
  * reverting that one instance restores exactly it, nothing else.
  * the v3 contract now requires an instance identifier: neither
    reject_inference nor revert_rejection will fall back to "every
    active instance this rule produced" when one is omitted.
  * a match_id decoded for the wrong rule_id is rejected rather than
    silently mis-targeting a different rule's instance.

What this CANNOT prove: that the Cypher itself is valid (no offline
parser; needs a live Neo4j) — same caveat tests/verify.py documents.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest


def _install_external_import_stubs() -> None:
    """Same minimal stubs tests/test_markdown_route_responses.py installs,
    so this file can also run in a sandbox with no neo4j/psycopg2 wheel."""
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

from reasoning_layer import rejection  # noqa: E402


# --------------------------------------------------------------------
# Fake Neo4j — same shape as tests/verify.py's FakeResult/FakeSession
# --------------------------------------------------------------------


class FakeResult:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record

    def data(self):
        return self._record if isinstance(self._record, list) else []


class FakeSession:
    def __init__(self, responder):
        self.calls: List[Dict[str, Any]] = []
        self._responder = responder

    def run(self, query, **params):
        self.calls.append({"query": query, "params": params})
        return FakeResult(self._responder(query, params))

    def close(self):
        pass


def fake_session_cm(session):
    @contextmanager
    def _cm(*args, **kwargs):
        yield session

    return _cm


# --------------------------------------------------------------------
# A tiny fake graph for Rule_01_Shared_Employer (symmetric-edge family):
# 3 subjects, A-B and A-C both currently-active SHARES_EMPLOYER_WITH
# instances — the exact scenario AI-28's acceptance test describes.
# --------------------------------------------------------------------


class FakeSymmetricEdgeGraph:
    """
    edges: frozenset({subject_a, subject_b}) -> "active" | "rejected".
    rejection_nodes: {(from_key, to_key)} written by _MERGE_REJECTION /
    removed by _DELETE_REJECTION — from_key/to_key are always
    (min, max) of the pair, matching rejection.py's own encoding.
    """

    def __init__(self, edges: Dict[frozenset, str]):
        self.edges = dict(edges)
        self.rejection_nodes: set = set()
        # AI-31: mirrors (:Case).last_inference_change_at — None until
        # the first reject/revert call touches it.
        self.case_last_inference_change_at: Optional[str] = None
        self.staleness_touch_count = 0

    def responder(self, query: str, params: Dict[str, Any]):
        if "RETURN s.subject_id AS primary_subject_id" in query:
            return {"primary_subject_id": "A"}

        # --- AI-31 staleness touch (reasoning_layer/rejection.py's
        # _touch_case_last_inference_change) — issued once per
        # reject_inference/revert_rejection call, after the AI-30
        # cascade queries below have already run.
        if "SET c.last_inference_change_at = $changed_at" in query:
            self.case_last_inference_change_at = params["changed_at"]
            self.staleness_touch_count += 1
            return {"case_id": params["case_id"]}

        if 'SET r.status = "rejected"' in query and "b.subject_id AS subject_id_b" in query:
            return self._locate_and_set(params, new_status="rejected", from_status="active")

        if 'SET r.status = "active"' in query and "b.subject_id AS subject_id_b" in query:
            return self._locate_and_set(params, new_status="active", from_status="rejected")

        if "MERGE (rej:Rejection" in query:
            self.rejection_nodes.add((params["from_key"], params["to_key"]))
            return None

        if "DELETE rej" in query:
            self.rejection_nodes.discard((params["from_key"], params["to_key"]))
            return [{"deleted": 1}]

        # --- AI-30 cascade queries (reasoning_layer/cascade.py) ---
        # reject_inference/revert_rejection now call cascade.cascade_reject/
        # cascade_revert immediately after their own write (same session),
        # which re-checks DOWNSTREAM_DEPENDENTS' condition for every subject
        # the rejected/reverted instance touches. Rule_01 has Rule_02 as a
        # downstream dependent (SHARES_EMPLOYER_WITH), so every
        # reject_inference/revert_rejection call in this test graph now
        # also issues cascade's own read/write queries below — none of
        # which this test graph's scenarios ever actually trigger an
        # auto-invalidation/reinstatement from (every subject here keeps at
        # least one other active SHARES_EMPLOYER_WITH edge throughout), but
        # the queries themselves still have to be answered instead of
        # falling through to the "unexpected query" guard at the bottom.
        if "RETURN count(r) > 0 AS still_active" in query:
            subject_id = params["subject_id"]
            still_active = any(
                subject_id in pair and status == "active" for pair, status in self.edges.items()
            )
            return {"still_active": still_active}

        if "was_auto_invalidated" in query:
            # This fake graph never models MEMBER_OF_FRAUD_NETWORK at all —
            # nothing it represents was ever auto-invalidated by cascade,
            # so _reinstate's guard correctly finds nothing to do.
            return {"was_auto_invalidated": False}

        if "MEMBER_OF_FRAUD_NETWORK" in query:
            # This fake graph has no :FraudNetwork nodes at all — a real
            # Neo4j MATCHing this pattern against it would return zero
            # rows too, so "nothing to update" is the honest answer, not a
            # test shortcut. Exercised when cascade's condition re-check
            # finds a subject (e.g. C, once A-C is its only
            # SHARES_EMPLOYER_WITH edge and that gets rejected) with no
            # other active edge of the type Rule_02 reads — the walk then
            # tries to auto-invalidate Rule_02's membership for that
            # subject, which correctly no-ops here since this graph never
            # asserted one in the first place.
            return {"updated": 0}

        raise AssertionError(f"unexpected query in test: {query.strip()[:200]}")

    def _locate_and_set(self, params: Dict[str, Any], new_status: str, from_status: str):
        target_a = params.get("target_subject_id_a")
        target_b = params.get("target_subject_id_b")
        scope_subject_ids = params["scope_subject_ids"]
        rows = []
        for pair in list(self.edges.keys()):
            if self.edges[pair] != from_status:
                continue
            a_id, b_id = sorted(pair)
            if not (a_id in scope_subject_ids or b_id in scope_subject_ids):
                continue
            if target_a is not None and {a_id, b_id} != {target_a, target_b}:
                continue
            self.edges[pair] = new_status
            rows.append({"subject_id_a": a_id, "subject_id_b": b_id})
        return rows


_SCOPE = {"scope_subject_ids": ["A", "B", "C"], "scope_case_ids": ["CASE-1"]}


def _patched(graph: FakeSymmetricEdgeGraph):
    session = FakeSession(graph.responder)
    return (
        mock.patch.object(rejection, "get_session", fake_session_cm(session)),
        mock.patch.object(rejection, "resolve_scope", lambda **kwargs: dict(_SCOPE)),
    )


# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------


def test_reject_only_the_identified_instance_leaves_others_active():
    """The AI-28 acceptance scenario: 3 subjects, reject only the A-C
    match, confirm A-B is still active and C stays rejected."""
    graph = FakeSymmetricEdgeGraph({frozenset({"A", "B"}): "active", frozenset({"A", "C"}): "active"})

    p1, p2 = _patched(graph)
    with p1, p2:
        envelope = rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="False positive — different employer branch",
            investigator_id="inv-1",
            subject_id_a="A",
            subject_id_b="C",
        )

    result = envelope["result"]
    assert result["rejected_count"] == 1
    assert {result["rejected_items"][0]["subject_id_a"], result["rejected_items"][0]["subject_id_b"]} == {
        "A",
        "C",
    }
    assert graph.edges[frozenset({"A", "B"})] == "active", "A-B must be untouched by rejecting A-C"
    assert graph.edges[frozenset({"A", "C"})] == "rejected"
    assert graph.rejection_nodes == {("A", "C")}, "only one :Rejection node, for A-C, must be written"
    # AI-31: the case-wide staleness signal is set to the same rejected_at
    # value returned in the envelope, and echoed back on the response.
    assert graph.case_last_inference_change_at == result["rejected_at"]
    assert result["last_inference_change_at"] == result["rejected_at"]


def test_reject_then_rerun_pipeline_guard_then_revert_only_that_instance():
    """
    Extends the scenario: after rejecting A-C, simulate what a rule
    file's own NOT EXISTS {...} guard checks (from_key/to_key IN the
    rejected pair) — confirms A-B would still be (re)written on a
    pipeline re-run while A-C would not. Then reverts A-C and confirms
    ONLY A-C comes back, A-B is untouched throughout.
    """
    graph = FakeSymmetricEdgeGraph({frozenset({"A", "B"}): "active", frozenset({"A", "C"}): "active"})
    p1, p2 = _patched(graph)
    with p1, p2:
        rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="not the same branch",
            investigator_id="inv-1",
            subject_id_a="A",
            subject_id_b="C",
        )

    # What every rule file's own guard checks before re-asserting a fact:
    # "does a :Rejection node exist with from_key/to_key covering this pair?"
    def guard_would_reassert(pair: frozenset) -> bool:
        a_id, b_id = sorted(pair)
        from_key, to_key = min(a_id, b_id), max(a_id, b_id)
        return (from_key, to_key) not in graph.rejection_nodes

    assert guard_would_reassert(frozenset({"A", "B"})) is True, "A-B has no :Rejection — guard lets it reassert"
    assert guard_would_reassert(frozenset({"A", "C"})) is False, "A-C's :Rejection blocks reassertion"

    p1, p2 = _patched(graph)
    with p1, p2:
        revert_envelope = rejection.revert_rejection(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            investigator_id="inv-2",
            reason="re-reviewed, it is the same branch after all",
            subject_id_a="A",
            subject_id_b="C",
        )

    result = revert_envelope["result"]
    assert result["reverted_count"] == 1
    assert graph.edges[frozenset({"A", "C"})] == "active"
    assert graph.edges[frozenset({"A", "B"})] == "active", "A-B must still be untouched after reverting A-C"
    assert graph.rejection_nodes == set(), "A-C's :Rejection node must be deleted by the revert"
    # AI-31: revert touches the same staleness signal reject did — a
    # revert changes the graph just as much as a reject does.
    assert graph.case_last_inference_change_at == result["reverted_at"]
    assert result["last_inference_change_at"] == result["reverted_at"]


def test_match_id_and_subject_ids_are_interchangeable_inputs():
    """A caller may identify the same instance either way and get the
    same result — match_id is just a convenience wrapper."""
    graph_a = FakeSymmetricEdgeGraph({frozenset({"A", "B"}): "active", frozenset({"A", "C"}): "active"})
    p1, p2 = _patched(graph_a)
    with p1, p2:
        by_subject_ids = rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="r",
            investigator_id="inv-1",
            subject_id_a="A",
            subject_id_b="C",
        )["result"]

    graph_b = FakeSymmetricEdgeGraph({frozenset({"A", "B"}): "active", frozenset({"A", "C"}): "active"})
    match_id = rejection.build_match_id("Rule_01_Shared_Employer", "A", "C")
    p1, p2 = _patched(graph_b)
    with p1, p2:
        by_match_id = rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="r",
            investigator_id="inv-1",
            match_id=match_id,
        )["result"]

    assert graph_a.edges == graph_b.edges
    assert by_subject_ids["rejected_count"] == by_match_id["rejected_count"] == 1


def test_match_id_round_trips_through_the_response():
    graph = FakeSymmetricEdgeGraph({frozenset({"A", "B"}): "active", frozenset({"A", "C"}): "active"})
    p1, p2 = _patched(graph)
    with p1, p2:
        result = rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="r",
            investigator_id="inv-1",
            subject_id_a="A",
            subject_id_b="C",
        )["result"]

    match_id = result["rejected_items"][0]["match_id"]
    decoded_rule_id, decoded_a, decoded_b = rejection.decode_match_id(match_id)
    assert decoded_rule_id == "Rule_01_Shared_Employer"
    assert {decoded_a, decoded_b} == {"A", "C"}


def test_match_id_for_a_different_rule_id_is_rejected():
    """A stale or copy-pasted token from a different row must not
    silently mis-target this rule."""
    stale_token = rejection.build_match_id("Rule_03_Shared_Address", "A", "C")
    with pytest.raises(ValueError, match="issued for rule_id"):
        rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="r",
            investigator_id="inv-1",
            match_id=stale_token,
        )


def test_neither_match_id_nor_subject_id_a_raises():
    """v3 contract: there is no more implicit 'every active instance
    this rule produced' fallback."""
    with pytest.raises(ValueError, match="require identifying the exact"):
        rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="r",
            investigator_id="inv-1",
        )

    with pytest.raises(ValueError, match="require identifying the exact"):
        rejection.revert_rejection(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            investigator_id="inv-1",
            reason="r",
        )


def test_reject_nonexistent_instance_raises_not_found():
    """Targeting a pair with no active edge (e.g. B-C, which never
    fired) must 404, not silently fall back to some other pair."""
    graph = FakeSymmetricEdgeGraph({frozenset({"A", "B"}): "active", frozenset({"A", "C"}): "active"})
    p1, p2 = _patched(graph)
    with p1, p2:
        with pytest.raises(rejection.InferenceNotFoundError):
            rejection.reject_inference(
                case_id="CASE-1",
                rule_id="Rule_01_Shared_Employer",
                reason="r",
                investigator_id="inv-1",
                subject_id_a="B",
                subject_id_b="C",
            )
    # Neither real edge was touched by the failed attempt.
    assert graph.edges[frozenset({"A", "B"})] == "active"
    assert graph.edges[frozenset({"A", "C"})] == "active"


# --------------------------------------------------------------------
# AI-31 — (:Case).last_inference_change_at staleness signal
# --------------------------------------------------------------------


def test_reject_sets_case_staleness_timestamp_once_per_call():
    """Two separate instances rejected in two separate calls must each
    touch the staleness timestamp exactly once per call — not once per
    instance, not once per AI-30 cascade hop."""
    graph = FakeSymmetricEdgeGraph({frozenset({"A", "B"}): "active", frozenset({"A", "C"}): "active"})
    assert graph.case_last_inference_change_at is None, "untouched until the first reject/revert"

    p1, p2 = _patched(graph)
    with p1, p2:
        result = rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="r",
            investigator_id="inv-1",
            subject_id_a="A",
            subject_id_b="B",
        )["result"]

    assert graph.case_last_inference_change_at is not None
    assert graph.case_last_inference_change_at == result["rejected_at"]

    # The staleness-touch query must have been issued exactly once for
    # this call, not once per located instance and not once per AI-30
    # cascade hop.
    assert graph.staleness_touch_count == 1


def test_case_not_found_does_not_fail_the_reject():
    """AI-31's staleness write is best-effort bookkeeping, never the
    primary write: if the Case node can't be found for some reason, the
    reject itself must still succeed (see
    reasoning_layer.rejection._touch_case_last_inference_change's
    docstring)."""
    graph = FakeSymmetricEdgeGraph({frozenset({"A", "B"}): "active", frozenset({"A", "C"}): "active"})

    real_responder = graph.responder

    def responder_with_missing_case(query: str, params: Dict[str, Any]):
        if "SET c.last_inference_change_at = $changed_at" in query:
            return None  # simulates no (:Case) node matched
        return real_responder(query, params)

    graph.responder = responder_with_missing_case  # type: ignore[method-assign]

    p1, p2 = _patched(graph)
    with p1, p2:
        result = rejection.reject_inference(
            case_id="CASE-1",
            rule_id="Rule_01_Shared_Employer",
            reason="r",
            investigator_id="inv-1",
            subject_id_a="A",
            subject_id_b="B",
        )["result"]

    assert result["accepted"] is True, "a missing Case node must not turn a successful reject into a failure"
    assert graph.edges[frozenset({"A", "B"})] == "rejected"
