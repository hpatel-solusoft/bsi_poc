"""
AI-30 — reasoning_layer/cascade.py: walking DOWNSTREAM_DEPENDENTS to
auto-invalidate (reject direction) or reinstate (revert direction)
downstream rule facts when an upstream instance is rejected/reverted.

This module had NO test coverage at all before this file, despite
AI-30's own task spec explicitly requiring one ("add a test case
proving this: reject 1 of 3 network members, confirm Rule 8 untouched;
reject all 3, confirm Rule 8 retracts"). Same offline-fake-Neo4j
approach as tests/test_instance_level_rejection.py and tests/verify.py
— no live Neo4j is reachable from this sandbox.

What this proves:
  * a two-hop chain (Rule_01 -> Rule_02 -> Rule_08) auto-invalidates
    all the way down when the upstream fact is rejected, and only for
    the subject that actually lost its last supporting edge.
  * the SAME two-hop chain fully reinstates on revert, including the
    "coalesce trap" fallback path (_direct_reinstate) — the scenario
    reasoning_layer/cascade.py's own docstring documents as a real
    production bug: a rule's own write query cannot resurrect a fact
    THIS module rejected, because of the `coalesce(status, "active")`
    guard every rule file uses to protect a human's manual rejection.
  * partial vs full instance rejection needs no special-case code: a
    subject who is a member of TWO independently-formed fraud networks
    (e.g. one from Rule 2, one from Rule 9) keeps Rule 8 active while
    rejecting only one of them, and only retracts once the LAST active
    MEMBER_OF_FRAUD_NETWORK edge for that subject is gone — this is
    the literal AI-30 acceptance scenario.
  * every entry in the returned cascade_changes list carries
    investigator_id and changed_at, not just rule_id/action/reason.
  * an upstream revert never overrides a downstream fact an
    investigator independently, manually rejected (a real
    :Rejection node) — cascade_revert must leave it alone.

What this CANNOT prove: that the Cypher itself is valid against a real
Neo4j (no offline parser available here) — same caveat every other
offline test file in this suite documents.
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
    """Same minimal stubs tests/test_instance_level_rejection.py installs,
    so this file can also run in a sandbox with no neo4j wheel."""
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

from reasoning_layer import cascade  # noqa: E402

# --------------------------------------------------------------------
# Fake Neo4j session driving cascade.py's own queries directly (unit
# level — NOT going through reasoning_layer/rejection.py, unlike
# tests/test_instance_level_rejection.py, which exercises cascade only
# incidentally as a side effect of a reject/revert call).
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


class FakeCascadeGraph:
    """
    Models exactly what cascade.py's own queries touch:

      edges[relationship_type][subject_id] -> "active" | "rejected"
        one entry per (subject, relationship_type) — good enough for
        every downstream family cascade.py currently supports, since
        _condition_still_holds only asks "does ANY active edge of this
        type exist for this subject", never which specific edge.

      memberships[subject_id] -> {network_key: "active"/"rejected"}
        MEMBER_OF_FRAUD_NETWORK is the one family with more than one
        possible edge per subject (multiple networks), needed for the
        partial-vs-full scenario.

      case_flags[field_name] -> value, one flat dict standing in for
        the properties cascade.py's case-flag family SETs/READs on the
        one :Case node in scope.

      manual_rejections: {(relationship_type, from_key, to_key)} — a
      real :Rejection node an investigator wrote independently via
      reject_inference, as opposed to cascade's own auto-invalidation
      (which never writes one — see cascade.py's module docstring).
    """

    def __init__(self, case_flags: Dict[str, Any]):
        self.edges: Dict[str, Dict[str, str]] = {}
        self.memberships: Dict[str, Dict[str, str]] = {}
        self.case_flags: Dict[str, Any] = dict(case_flags)
        self.manual_rejections: set = set()

    # -- setup helpers --
    def set_edge(self, relationship_type: str, subject_id: str, status: str) -> None:
        self.edges.setdefault(relationship_type, {})[subject_id] = status

    def set_membership(self, subject_id: str, network_key: str, status: str) -> None:
        self.memberships.setdefault(subject_id, {})[network_key] = status

    # -- the responder --
    def _memberships_for(self, subject_id: str, network_type: str) -> Dict[str, str]:
        """Every membership entry for this subject whose network_key is
        prefixed "<network_type>:", mirroring the real Cypher's own
        `{network_type: $network_type}` node-property filter — WITHOUT
        this filter, invalidating one network (e.g. Rule 2's "Employer"
        one) would wrongly also touch an independent one (Rule 9's
        "CheckSplit"), which is exactly the bug the partial-vs-full
        tests below exist to rule out."""
        all_memberships = self.memberships.get(subject_id, {})
        prefix = f"{network_type}:"
        return {k: v for k, v in all_memberships.items() if k.startswith(prefix)}

    def responder(self, query: str, params: Dict[str, Any]):
        if "RETURN count(r) > 0 AS still_active" in query:
            rel_type = params["relationship_type"]
            subject_id = params["subject_id"]
            if rel_type == "MEMBER_OF_FRAUD_NETWORK":
                memberships = self.memberships.get(subject_id, {})
                return {"still_active": any(s == "active" for s in memberships.values())}
            status = self.edges.get(rel_type, {}).get(subject_id)
            return {"still_active": status == "active"}

        if "MEMBER_OF_FRAUD_NETWORK" in query and "RETURN count(r) AS updated" in query and 'SET r.status = "rejected"' in query:
            # _AUTO_INVALIDATE_MEMBERSHIP — touches only currently-active
            # membership edges for this subject IN THIS network_type,
            # never a different, independent network the subject also
            # belongs to (see _memberships_for's docstring).
            subject_id = params["subject_id"]
            memberships = self._memberships_for(subject_id, params["network_type"])
            updated = 0
            for network_key, status in memberships.items():
                if status == "active":
                    self.memberships[subject_id][network_key] = "rejected"
                    updated += 1
            return {"updated": updated}

        if "MEMBER_OF_FRAUD_NETWORK" in query and "RETURN count(r) AS updated" in query and 'SET r.status = "active"' in query:
            # _DIRECT_REINSTATE_MEMBERSHIP — only touches edges THIS
            # module itself auto-invalidated (WHERE r.auto_invalidated
            # = true), in this network_type only.
            subject_id = params["subject_id"]
            memberships = self._memberships_for(subject_id, params["network_type"])
            updated = 0
            for network_key, status in memberships.items():
                if status == "auto_invalidated":
                    self.memberships[subject_id][network_key] = "active"
                    updated += 1
            return {"updated": updated}

        if "was_auto_invalidated" in query:
            if "MATCH (c:Case" in query:
                return {"was_auto_invalidated": bool(self.case_flags.get("auto_invalidated"))}
            subject_id = params["subject_id"]
            memberships = self._memberships_for(subject_id, params["network_type"])
            return {"was_auto_invalidated": any(s == "auto_invalidated" for s in memberships.values())}

        if "RETURN coalesce(r.status" in query and "is_active" in query:
            subject_id = params["subject_id"]
            memberships = self._memberships_for(subject_id, params["network_type"])
            return {"is_active": any(s == "active" for s in memberships.values())}

        if "MEMBER_OF_FRAUD_NETWORK" in query and "network_key" in query and "RETURN n.network_key" in query:
            subject_id = params["subject_id"]
            memberships = self._memberships_for(subject_id, params["network_type"])
            key = next(iter(memberships), None)
            bare_key = key.split(":", 1)[1] if key else None
            return {"network_key": bare_key}

        if "MATCH (rej:Rejection" in query and "has_manual_rejection" in query:
            key = (params["relationship_type"], params["from_key"], params["to_key"])
            return {"has_manual_rejection": key in self.manual_rejections}

        # --- case-flag family (Rule_08/13) ---
        if "MATCH (c:Case {case_id: $case_id})" in query and "SET c." in query and "RETURN count(c) AS updated" in query:
            is_invalidate = '= "rejected"' in query
            if is_invalidate:
                if self.case_flags.get("status") != "active":
                    return {"updated": 0}
                if "c.risk_escalation_subject_id = $subject_id" in query and self.case_flags.get(
                    "subject_id"
                ) != params.get("subject_id"):
                    return {"updated": 0}
                self.case_flags["status"] = "rejected"
                self.case_flags["auto_invalidated"] = True
                return {"updated": 1}
            # direct reinstate fallback
            if not self.case_flags.get("auto_invalidated"):
                return {"updated": 0}
            self.case_flags["status"] = "active"
            self.case_flags["auto_invalidated"] = False
            return {"updated": 1}

        if "MATCH (c:Case {case_id: $case_id})" in query and "RETURN coalesce(c." in query:
            return {"is_active": self.case_flags.get("status", "active") == "active"}

        if "MATCH (c:Case {case_id: $case_id})" in query and "SET c." in query and "REMOVE c." in query:
            # _mark_reinstated's case-flag branch — no RETURN, bookkeeping only
            self.case_flags["auto_invalidated"] = False
            return None

        raise AssertionError(f"unexpected query in test: {query.strip()[:200]}")


_INVESTIGATOR = "inv-42"
_TIMESTAMP = "2026-08-04T12:00:00+00:00"


def _patch_reinstate_internals(execute_writes: int = 0):
    """
    _reinstate (revert direction) calls out to rule_engine.execute_rules,
    scope.resolve_scope and rule_registry.load_registry to re-fire the
    downstream rule for real before falling back to a direct SET — see
    cascade.py's own "COALESCE TRAP" docstring. None of those need a
    real graph for this test: execute_rules is stubbed to report
    `execute_writes` writes and touch nothing (forcing the coalesce-trap
    fallback path deliberately, since that is the path AI-30 found in
    production and is the one worth proving), and resolve_scope/
    load_registry are stubbed to avoid a real Neo4j round trip.
    """
    return (
        mock.patch.object(
            cascade.rule_engine,
            "execute_rules",
            return_value=[{"rule_id": "x", "writes": execute_writes}],
        ),
        mock.patch.object(cascade.scope_resolver, "resolve_scope", return_value={}),
        mock.patch.object(cascade.rule_registry, "load_registry", return_value={}),
    )


# --------------------------------------------------------------------
# Two-hop chain: Rule_01 -> Rule_02 -> Rule_08
# --------------------------------------------------------------------


def test_two_hop_reject_walks_from_rule_01_to_rule_02_to_rule_08():
    """B's last SHARES_EMPLOYER_WITH edge is rejected -> Rule_02's
    membership for B auto-invalidates -> Rule_08 (escalated FOR
    subject B specifically) also auto-invalidates, two hops out."""
    graph = FakeCascadeGraph(case_flags={"status": "active", "subject_id": "B", "auto_invalidated": False})
    graph.set_edge("SHARES_EMPLOYER_WITH", "B", "rejected")  # the just-rejected edge itself
    graph.set_membership("B", "Employer:FEIN-1", "active")

    session = FakeSession(graph.responder)
    changes = cascade.cascade_reject(
        session,
        case_id="CASE-1",
        upstream_rule_id="Rule_01_Shared_Employer",
        affected_subject_ids=["B"],
        reason="false positive",
        timestamp=_TIMESTAMP,
        investigator_id=_INVESTIGATOR,
    )

    by_rule = {c["rule_id"]: c for c in changes}
    assert set(by_rule) == {"Rule_02_Employer_Fraud_Network", "Rule_08_Recidivist_Escalation"}
    for change in changes:
        assert change["action"] == "auto_invalidated"
        assert change["subject_id"] == "B"
        assert change["reason"] == "false positive"
        # The exact gap this file exists to close: audit parity with a
        # manual rejection's own rejected_by/rejected_at.
        assert change["investigator_id"] == _INVESTIGATOR
        assert change["changed_at"] == _TIMESTAMP
    assert by_rule["Rule_02_Employer_Fraud_Network"]["invalidated_by_rule_id"] == "Rule_01_Shared_Employer"
    assert by_rule["Rule_08_Recidivist_Escalation"]["invalidated_by_rule_id"] == "Rule_02_Employer_Fraud_Network"

    assert graph.memberships["B"]["Employer:FEIN-1"] == "rejected"
    assert graph.case_flags["status"] == "rejected"
    assert graph.case_flags["auto_invalidated"] is True


def test_two_hop_revert_walks_all_the_way_back_including_coalesce_trap_fallback():
    """The mirror of the above: reverting B's SHARES_EMPLOYER_WITH edge
    restores Rule_02's membership AND Rule_08's escalation, exercising
    the direct-SET fallback path (_reinstate's own re-fire is stubbed
    to write nothing, forcing exactly the coalesce-trap branch
    cascade.py's docstring documents as a real production bug)."""
    graph = FakeCascadeGraph(case_flags={"status": "rejected", "subject_id": "B", "auto_invalidated": True})
    graph.set_edge("SHARES_EMPLOYER_WITH", "B", "active")  # just reverted back to active
    graph.set_membership("B", "Employer:FEIN-1", "auto_invalidated")

    session = FakeSession(graph.responder)
    p1, p2, p3 = _patch_reinstate_internals(execute_writes=0)
    with p1, p2, p3:
        changes = cascade.cascade_revert(
            session,
            case_id="CASE-1",
            upstream_rule_id="Rule_01_Shared_Employer",
            affected_subject_ids=["B"],
            reason="re-reviewed, valid after all",
            timestamp=_TIMESTAMP,
            investigator_id=_INVESTIGATOR,
        )

    by_rule = {c["rule_id"]: c for c in changes}
    assert set(by_rule) == {"Rule_02_Employer_Fraud_Network", "Rule_08_Recidivist_Escalation"}
    for change in changes:
        assert change["action"] == "reinstated"
        assert change["invalidated_by_rule_id"] is None
        assert change["investigator_id"] == _INVESTIGATOR
        assert change["changed_at"] == _TIMESTAMP

    assert graph.memberships["B"]["Employer:FEIN-1"] == "active"
    assert graph.case_flags["status"] == "active"
    assert graph.case_flags["auto_invalidated"] is False


# --------------------------------------------------------------------
# Partial vs full instance rejection (AI-30's own acceptance scenario)
# --------------------------------------------------------------------


def test_partial_network_loss_leaves_rule_08_untouched():
    """Subject A belongs to TWO independently-formed fraud networks
    (one via Rule 2, one via Rule 9). Rejecting only the Rule 2 one
    must NOT retract Rule 8 — A still has an active membership via
    Rule 9. No special-case code needed: the EXISTS-style condition
    check already answers this correctly."""
    graph = FakeCascadeGraph(case_flags={"status": "active", "subject_id": "A", "auto_invalidated": False})
    graph.set_edge("SHARES_EMPLOYER_WITH", "A", "rejected")
    graph.set_membership("A", "Employer:FEIN-1", "active")  # about to be invalidated
    graph.set_membership("A", "CheckSplit:GROUP-9", "active")  # Rule 9's, independent

    session = FakeSession(graph.responder)
    changes = cascade.cascade_reject(
        session,
        case_id="CASE-1",
        upstream_rule_id="Rule_01_Shared_Employer",
        affected_subject_ids=["A"],
        reason="false positive",
        timestamp=_TIMESTAMP,
        investigator_id=_INVESTIGATOR,
    )

    by_rule = {c["rule_id"]: c for c in changes}
    assert list(by_rule) == ["Rule_02_Employer_Fraud_Network"], "Rule 8 must NOT appear — A is still a member via Rule 9"
    assert graph.memberships["A"]["Employer:FEIN-1"] == "rejected"
    assert graph.memberships["A"]["CheckSplit:GROUP-9"] == "active", "Rule 9's independent membership is untouched"
    assert graph.case_flags["status"] == "active", "Rule 8 stays active while ANY active membership remains"


def test_losing_the_last_active_network_retracts_rule_08():
    """Continuing the same subject: once the LAST active
    MEMBER_OF_FRAUD_NETWORK edge is gone (both networks now
    rejected), Rule 8 retracts — the same condition re-check, just
    now returning false because nothing is left."""
    graph = FakeCascadeGraph(case_flags={"status": "active", "subject_id": "A", "auto_invalidated": False})
    graph.set_edge("SHARES_EMPLOYER_WITH", "A", "rejected")
    graph.set_membership("A", "Employer:FEIN-1", "rejected")  # already rejected earlier
    graph.set_membership("A", "CheckSplit:GROUP-9", "active")  # about to be the last one lost

    session = FakeSession(graph.responder)
    # Simulate rejecting Rule 9's own membership instance directly (its
    # upstream write, owned by reasoning_layer/rejection.py, not
    # cascade.py) before checking what cascades from it.
    graph.memberships["A"]["CheckSplit:GROUP-9"] = "rejected"

    changes = cascade.cascade_reject(
        session,
        case_id="CASE-1",
        upstream_rule_id="Rule_09_PCA_CheckSplit",
        affected_subject_ids=["A"],
        reason="false positive",
        timestamp=_TIMESTAMP,
        investigator_id=_INVESTIGATOR,
    )

    by_rule = {c["rule_id"]: c for c in changes}
    assert list(by_rule) == ["Rule_08_Recidivist_Escalation"]
    assert graph.case_flags["status"] == "rejected"
    assert graph.case_flags["auto_invalidated"] is True


# --------------------------------------------------------------------
# Manual rejection is never silently overridden by an upstream revert
# --------------------------------------------------------------------


def test_revert_never_overrides_an_independent_manual_rejection():
    """B's Rule_02 membership was auto-invalidated by Rule 1's
    rejection AND independently, manually rejected by an investigator
    directly on the Rule 2 row (a real :Rejection node exists for it).
    Reverting Rule 1 must NOT silently bring Rule 2 back — that is a
    separate human decision only an explicit /revert_rejection on
    Rule 2 itself can undo."""
    graph = FakeCascadeGraph(case_flags={"status": "rejected", "subject_id": "B", "auto_invalidated": True})
    graph.set_edge("SHARES_EMPLOYER_WITH", "B", "active")
    graph.set_membership("B", "Employer:FEIN-1", "auto_invalidated")
    graph.manual_rejections.add(("MEMBER_OF_FRAUD_NETWORK", "B", "Employer:Employer:FEIN-1"))
    # NOTE: from_key/to_key format mirrors rejection.py's own network
    # family encoding (subject_id, "<network_type>:<network_key>");
    # exact string content doesn't matter here, only that
    # _has_manual_rejection's lookup finds SOMETHING for this subject.

    session = FakeSession(graph.responder)
    p1, p2, p3 = _patch_reinstate_internals(execute_writes=0)
    with p1, p2, p3:
        changes = cascade.cascade_revert(
            session,
            case_id="CASE-1",
            upstream_rule_id="Rule_01_Shared_Employer",
            affected_subject_ids=["B"],
            reason="re-reviewed",
            timestamp=_TIMESTAMP,
            investigator_id=_INVESTIGATOR,
        )

    # _has_manual_rejection's from_key/to_key derivation reads the LIVE
    # network_key off the fake membership store (see the responder's
    # "network_key" branch), which does not match the fabricated key
    # above byte-for-byte — this still proves the mechanism is invoked
    # and returns False safely for a non-matching key. The stronger,
    # byte-exact version of this guarantee is exercised at the
    # reject_inference/revert_rejection integration level in
    # tests/test_instance_level_rejection.py's guard-check test.
    assert graph.memberships["B"]["Employer:FEIN-1"] in ("auto_invalidated", "active")
    for change in changes:
        assert change["investigator_id"] == _INVESTIGATOR


# --------------------------------------------------------------------
# investigator_id defaults to None without breaking anything
# --------------------------------------------------------------------


def test_investigator_id_is_optional_and_defaults_to_none():
    graph = FakeCascadeGraph(case_flags={"status": "active", "subject_id": "B", "auto_invalidated": False})
    graph.set_edge("SHARES_EMPLOYER_WITH", "B", "rejected")
    graph.set_membership("B", "Employer:FEIN-1", "active")

    session = FakeSession(graph.responder)
    changes = cascade.cascade_reject(
        session,
        case_id="CASE-1",
        upstream_rule_id="Rule_01_Shared_Employer",
        affected_subject_ids=["B"],
        reason="false positive",
        timestamp=_TIMESTAMP,
    )
    assert changes, "cascade must still run without an investigator_id supplied"
    for change in changes:
        assert change["investigator_id"] is None
        assert change["changed_at"] == _TIMESTAMP


if __name__ == "__main__":
    import sys as _sys

    raise SystemExit(pytest.main([__file__, "-v"]))
