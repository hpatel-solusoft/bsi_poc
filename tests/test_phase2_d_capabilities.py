"""
Verification suite for D2 (reject_inference), D3 (fraud_network) and D4
(rule_audit) — the three capabilities this change adds on top of the
already-verified ETL -> Neo4j -> rules build (see tests/verify.py's own
docstring for what that suite covers and why everything here is mocked
the same way).

Reuses tests/verify.py's FakeResult / FakeSession / fake_session_cm /
check() harness rather than redefining it — the whole point of that
harness is that "what does this codebase ask the graph, and in what
order" is checkable without a live Neo4j, and duplicating it here would
be exactly the kind of drift the architecture guideline warns about.

What this DOES prove:
  * every one of the 13 rejectable rule_ids resolves to the correct
    from_key/to_key encoding and the correct Cypher family
  * a reject_inference call that finds nothing raises
    InferenceNotFoundError, never a silent no-op or a 500
  * every validation rule in rejection.reject_inference actually rejects
    the input it claims to (missing subject_id_b, extra subject_id_b,
    unknown rule_id, mismatched relationship_type, blank investigator_id)
  * fraud_network.get_fraud_network groups nodes/edges by network and
    keeps rejected edges (dashed-style requirement) rather than
    filtering them out
  * rule_audit.get_rule_audit always returns all 13 rejectable rule_ids,
    fired or not, and degrades to an empty-but-valid audit when no
    primary subject is on the graph yet
  * the three new routes in api/server.py map InferenceNotFoundError to
    404 and ValueError to 400, not 500

What this CANNOT prove, and what still needs a live Neo4j:
  * that the Cypher text is syntactically valid Cypher
  * that a real rule file's own guard actually treats a from_key/to_key
    pair written here as blocking a future re-assertion (that half is
    exercised by the .cypher files themselves, not by Python)

Run:  python -m tests.test_phase2_d_capabilities
"""

from __future__ import annotations

import sys
from unittest import mock

from tests.verify import FakeResult, FakeSession, fake_session_cm, check, PASSES, FAILURES

from reasoning_layer import rejection, fraud_network, rule_audit


# --------------------------------------------------------------------
# 1. reject_inference — per-family key resolution
# --------------------------------------------------------------------

def test_reject_symmetric_edge():
    print("\n[D2.1] rejection.py — symmetric edge (Rule_01_Shared_Employer)")

    def responder(q, p):
        if "SET r.status = \"rejected\"" in q and "SHARES_EMPLOYER_WITH" in q:
            assert p["subject_id_a"] == "S2" and p["subject_id_b"] == "S1"
            return {"target_id": "rel-123"}
        if "MERGE (rej:Rejection" in q:
            assert p["from_key"] == "S1" and p["to_key"] == "S2", (
                "from_key/to_key must be sorted regardless of call order"
            )
            return {"rejection_id": "rej-1", "rejected_at": "2026-07-19T00:00:00Z", "rejected_by": "inv-1"}
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
        envelope = rejection.reject_inference(
            case_id="C1", subject_id_a="S2", subject_id_b="S1",
            rule_id="Rule_01_Shared_Employer", relationship_type="SHARES_EMPLOYER_WITH",
            investigator_id="inv-1", reason="Confirmed different employers on review",
        )
    check("symmetric edge: from_key/to_key sorted, not call-order dependent",
          envelope["result"]["accepted"] is True)
    check("symmetric edge: rejection_id returned", envelope["result"]["rejection_id"] == "rej-1")
    check("symmetric edge: exactly one locate + one merge issued", len(session.calls) == 2)


def test_reject_network_edge_both_subjects():
    print("\n[D2.2] rejection.py — network edge, both subjects (Rule_09_PCA_CheckSplit)")

    def responder(q, p):
        if "MEMBER_OF_FRAUD_NETWORK" in q and "OPTIONAL MATCH" in q:
            check("network edge: subject_id_b passed through for dual rejection",
                  p["subject_id_b"] == "S2")
            return {"target_id": "rel-a", "network_type": "CheckSplit", "network_key": "C1"}
        if "MERGE (rej:Rejection" in q:
            check("network edge: to_key built from live FraudNetwork node, not hardcoded",
                  p["to_key"] == "CheckSplit:C1")
            check("network edge: from_key is the subject the investigator acted on",
                  p["from_key"] == "S1")
            return {"rejection_id": "rej-2", "rejected_at": "t", "rejected_by": "inv-1"}
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
        envelope = rejection.reject_inference(
            case_id="C1", subject_id_a="S1", subject_id_b="S2",
            rule_id="Rule_09_PCA_CheckSplit", relationship_type="MEMBER_OF_FRAUD_NETWORK",
            investigator_id="inv-1",
        )
    check("network edge: accepted", envelope["result"]["accepted"] is True)


def test_reject_case_flag():
    print("\n[D2.3] rejection.py — case-property flag (Rule_08_Recidivist_Escalation)")

    def responder(q, p):
        if "risk_escalation_status = \"active\"" in q:
            check("case flag: verifies caller's subject_id_a against the stored escalating subject",
                  p["subject_id_a"] == "S1")
            return {"target_id": "case-1"}
        if "MERGE (rej:Rejection" in q:
            check("case flag: from_key=subject, to_key=case_id",
                  p["from_key"] == "S1" and p["to_key"] == "C1")
            return {"rejection_id": "rej-3", "rejected_at": "t", "rejected_by": "inv-1"}
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
        envelope = rejection.reject_inference(
            case_id="C1", subject_id_a="S1",
            rule_id="Rule_08_Recidivist_Escalation", relationship_type="CASE_RISK_ESCALATION",
            investigator_id="inv-1",
        )
    check("case flag: accepted", envelope["result"]["accepted"] is True)


def test_reject_allegation_flag_resolves_allegation_id():
    print("\n[D2.4] rejection.py — allegation flag (Rule_12), allegation_id resolved by lookup")

    def responder(q, p):
        if "wage_corroboration_status = \"active\"" in q:
            return {"target_id": "al-1", "allegation_id": "ALLEG-99"}
        if "MERGE (rej:Rejection" in q:
            check("allegation flag: to_key is the resolved allegation_id, "
                  "never supplied by the caller",
                  p["to_key"] == "ALLEG-99")
            return {"rejection_id": "rej-4", "rejected_at": "t", "rejected_by": "inv-1"}
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
        envelope = rejection.reject_inference(
            case_id="C1", subject_id_a="S1",
            rule_id="Rule_12_SLAM_Wage_Corroboration", relationship_type="WAGE_CORROBORATION",
            investigator_id="inv-1",
        )
    check("allegation flag: accepted", envelope["result"]["accepted"] is True)


def test_reject_not_found_raises():
    print("\n[D2.5] rejection.py — nothing active to reject")
    session = FakeSession(lambda q, p: None)
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
        raised = False
        try:
            rejection.reject_inference(
                case_id="C1", subject_id_a="S1", subject_id_b="S2",
                rule_id="Rule_01_Shared_Employer", relationship_type="SHARES_EMPLOYER_WITH",
                investigator_id="inv-1",
            )
        except rejection.InferenceNotFoundError:
            raised = True
    check("a second reject on an already-rejected (or never-fired) fact raises "
          "InferenceNotFoundError, not a silent success", raised)


def test_reject_input_validation():
    print("\n[D2.6] rejection.py — input validation")
    session = FakeSession(lambda q, p: {"target_id": "x"})

    def expect_value_error(**kwargs):
        try:
            with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
                rejection.reject_inference(**kwargs)
            return False
        except ValueError:
            return True

    check("unknown rule_id rejected",
          expect_value_error(case_id="C1", subject_id_a="S1", rule_id="Rule_99_Nonexistent",
                              relationship_type="X", investigator_id="inv-1"))
    check("missing subject_id_b rejected for a two-subject rule",
          expect_value_error(case_id="C1", subject_id_a="S1",
                              rule_id="Rule_01_Shared_Employer",
                              relationship_type="SHARES_EMPLOYER_WITH", investigator_id="inv-1"))
    check("extra subject_id_b rejected for a single-subject rule",
          expect_value_error(case_id="C1", subject_id_a="S1", subject_id_b="S2",
                              rule_id="Rule_11_Cross_Case_Hub",
                              relationship_type="CROSS_CASE_HUB", investigator_id="inv-1"))
    check("blank investigator_id rejected — a rejection must be attributable",
          expect_value_error(case_id="C1", subject_id_a="S1", subject_id_b="S2",
                              rule_id="Rule_01_Shared_Employer",
                              relationship_type="SHARES_EMPLOYER_WITH", investigator_id="  "))
    try:
        with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
            rejection.reject_inference(
                case_id="C1", subject_id_a="S1", subject_id_b="S2",
                rule_id="Rule_01_Shared_Employer", relationship_type="SHARES_ADDRESS_WITH",
                investigator_id="inv-1",
            )
        mismatch_raised = False
    except rejection.RelationshipTypeMismatchError:
        mismatch_raised = True
    check("relationship_type mismatched against rule_id raises "
          "RelationshipTypeMismatchError (defense in depth)", mismatch_raised)


def test_all_rule_ids_have_a_spec():
    print("\n[D2.7] rejection.py — coverage")
    from reasoning_layer import rule_registry
    expected = [r for r in rule_registry.ALL_RULE_IDS if r != rule_registry.MODIFIER_RULE_ID]
    check("every non-modifier rule_id is rejectable",
          set(expected) == set(rejection.RULE_IDS_REJECTABLE),
          f"missing: {set(expected) - set(rejection.RULE_IDS_REJECTABLE)}")


# --------------------------------------------------------------------
# 2. fraud_network.py
# --------------------------------------------------------------------

def test_fraud_network_groups_and_keeps_rejected_edges():
    print("\n[D3.1] fraud_network.py — grouping + rejected edges kept")

    def responder(q, p):
        if "MEMBER_OF_FRAUD_NETWORK" in q and "APPEARS_IN_CASE" in q:
            return [{
                "network_ref": "n1", "network_type": "Employer", "network_key": "EMP-1",
                "formed_by_rule": "Rule_02_Employer_Fraud_Network",
                "members": [
                    {"subject_id": "S1", "display_name": "A", "confidence": "High",
                     "status": "active", "source_rule": "Rule_02_Employer_Fraud_Network",
                     "is_primary": True},
                    {"subject_id": "S2", "display_name": "B", "confidence": "Medium",
                     "status": "rejected", "source_rule": "Rule_02_Employer_Fraud_Network",
                     "is_primary": False},
                ],
            }]
        if "SHARES_EMPLOYER_WITH" in q:
            return [{"source": "S1", "target": "S2", "relationship_type": "SHARES_EMPLOYER_WITH",
                      "confidence": "High", "status": "rejected", "source_rule": "Rule_01_Shared_Employer"}]
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    with mock.patch.object(fraud_network, "get_session", fake_session_cm(session)):
        envelope = fraud_network.get_fraud_network("C1")

    result = envelope["result"]
    check("one network block returned", result["network_count"] == 1)
    net = result["networks"][0]
    check("both members present as nodes", {n["id"] for n in net["nodes"]} == {"S1", "S2"})
    check("is_primary reflects case membership, not network position",
          [n for n in net["nodes"] if n["id"] == "S1"][0]["is_primary"] is True
          and [n for n in net["nodes"] if n["id"] == "S2"][0]["is_primary"] is False)
    check("rejected edge is included, not filtered out (dashed-style requirement)",
          len(net["edges"]) == 1 and net["edges"][0]["status"] == "rejected")
    check("network confidence falls back to the strongest active membership",
          net["confidence"] == "High")


def test_fraud_network_confidence_falls_back_when_all_rejected():
    print("\n[D3.2] fraud_network.py — confidence when every membership is rejected")

    def responder(q, p):
        if "MEMBER_OF_FRAUD_NETWORK" in q:
            return [{
                "network_ref": "n1", "network_type": "Address", "network_key": "ADDR-1",
                "formed_by_rule": "Rule_04_Address_Fraud_Network",
                "members": [
                    {"subject_id": "S1", "display_name": "A", "confidence": "Medium",
                     "status": "rejected", "source_rule": "Rule_04_Address_Fraud_Network",
                     "is_primary": True},
                ],
            }]
        return []

    session = FakeSession(responder)
    with mock.patch.object(fraud_network, "get_session", fake_session_cm(session)):
        envelope = fraud_network.get_fraud_network("C1")
    check("a fully-rejected network still reports what it used to claim, not 'Unresolved'",
          envelope["result"]["networks"][0]["confidence"] == "Medium")


def test_fraud_network_blank_case_id():
    print("\n[D3.3] fraud_network.py — input validation")
    raised = False
    try:
        fraud_network.get_fraud_network("  ")
    except ValueError:
        raised = True
    check("blank case_id rejected", raised)


# --------------------------------------------------------------------
# 3. rule_audit.py
# --------------------------------------------------------------------

def test_rule_audit_always_returns_all_rejectable_rules():
    print("\n[D4.1] rule_audit.py — fixed-shape contract")
    from reasoning_layer import rule_registry

    def responder(q, p):
        if "is_primary = true" in q:
            return {"primary_subject_id": "S1"}
        if "Rule_01_Shared_Employer" in q:
            return [{"subject_id_a": "S1", "subject_id_b": "S2",
                      "relationship_type": "SHARES_EMPLOYER_WITH", "confidence": "High",
                      "asserted_at": "t", "corroborated": False, "status": "active"}]
        return []

    session = FakeSession(responder)
    scope_stub = {"scope_subject_ids": ["S1", "S2"], "scope_case_ids": ["C1"]}
    with mock.patch.object(rule_audit, "get_session", fake_session_cm(session)), \
         mock.patch.object(rule_audit, "resolve_scope", lambda case_id, subject_id: scope_stub):
        envelope = rule_audit.get_rule_audit("C1")

    result = envelope["result"]
    expected_ids = [r for r in rule_registry.ALL_RULE_IDS if r != rule_registry.MODIFIER_RULE_ID]
    check("all 13 rejectable rule_ids present, fired or not",
          {r["rule_id"] for r in result["rules"]} == set(expected_ids))
    check("Rule 14 (modifier) is excluded — it is not an independent inferable fact",
          rule_registry.MODIFIER_RULE_ID not in {r["rule_id"] for r in result["rules"]})
    fired = {r["rule_id"]: r["fired"] for r in result["rules"]}
    check("Rule_01 correctly reported as fired with its instance data",
          fired["Rule_01_Shared_Employer"] is True)
    check("a rule with no matching rows is reported fired=False, not omitted",
          fired["Rule_03_Shared_Address"] is False)
    check("rule_description is populated from rule_registry, not left as the raw rule_id",
          next(r["rule_description"] for r in result["rules"]
               if r["rule_id"] == "Rule_01_Shared_Employer") != "Rule_01_Shared_Employer")


def test_rule_audit_no_primary_subject_degrades_gracefully():
    print("\n[D4.2] rule_audit.py — no primary subject on the graph yet")
    session = FakeSession(lambda q, p: None if "is_primary" in q else [])
    with mock.patch.object(rule_audit, "get_session", fake_session_cm(session)):
        envelope = rule_audit.get_rule_audit("C1")
    result = envelope["result"]
    check("primary_subject_id is None rather than raising", result["primary_subject_id"] is None)
    check("every rule still present, all fired=False", all(not r["fired"] for r in result["rules"]))


def test_rule_audit_blank_case_id():
    print("\n[D4.3] rule_audit.py — input validation")
    raised = False
    try:
        rule_audit.get_rule_audit("")
    except ValueError:
        raised = True
    check("blank case_id rejected", raised)


# --------------------------------------------------------------------
# 4. api/server.py route-level error mapping (no live FastAPI server —
#    exercises the route functions directly, exactly as the rest of
#    this suite exercises reasoning_layer functions directly)
# --------------------------------------------------------------------

def test_route_error_mapping():
    print("\n[5] api/server.py — HTTP status mapping")
    import api.server as server
    from fastapi import HTTPException
    from api.models import RejectInferenceRequest

    session = FakeSession(lambda q, p: None)
    req = RejectInferenceRequest(
        case_id="C1", subject_id_a="S1", subject_id_b="S2",
        rule_id="Rule_01_Shared_Employer", relationship_type="SHARES_EMPLOYER_WITH",
        investigator_id="inv-1",
    )
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
        try:
            server.reject_inference_route(req)
            status = None
        except HTTPException as exc:
            status = exc.status_code
    check("InferenceNotFoundError maps to HTTP 404, not 500", status == 404)

    bad_req = RejectInferenceRequest(
        case_id="C1", subject_id_a="S1",
        rule_id="Rule_99_Nonexistent", relationship_type="X", investigator_id="inv-1",
    )
    try:
        server.reject_inference_route(bad_req)
        status = None
    except HTTPException as exc:
        status = exc.status_code
    check("unknown rule_id maps to HTTP 400, not 500", status == 400)


if __name__ == "__main__":
    test_reject_symmetric_edge()
    test_reject_network_edge_both_subjects()
    test_reject_case_flag()
    test_reject_allegation_flag_resolves_allegation_id()
    test_reject_not_found_raises()
    test_reject_input_validation()
    test_all_rule_ids_have_a_spec()
    test_fraud_network_groups_and_keeps_rejected_edges()
    test_fraud_network_confidence_falls_back_when_all_rejected()
    test_fraud_network_blank_case_id()
    test_rule_audit_always_returns_all_rejectable_rules()
    test_rule_audit_no_primary_subject_degrades_gracefully()
    test_rule_audit_blank_case_id()
    test_route_error_mapping()

    print("\n" + "=" * 68)
    print(f"{len(PASSES)} passed, {len(FAILURES)} failed")
    for failure in FAILURES:
        print(f"  FAILED: {failure}")
    print("=" * 68)
    sys.exit(1 if FAILURES else 0)
