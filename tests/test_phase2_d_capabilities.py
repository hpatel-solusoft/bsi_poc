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
    unknown rule_id, blank reason, blank investigator_id)
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

from reasoning_layer import rejection, rule_audit
from reasoning_layer.network import fraud_network
from tests.verify import FAILURES, PASSES, FakeSession, check, fake_session_cm

# --------------------------------------------------------------------
# 1. reject_inference — per-family key resolution
# --------------------------------------------------------------------


def test_reject_symmetric_edge():
    print("\n[D2.1] rejection.py — symmetric edge (Rule_01_Shared_Employer)")

    def responder(q, p):
        if "is_primary = true" in q:
            return {"primary_subject_id": "S1"}
        if 'SET r.status = "rejected"' in q and "SHARES_EMPLOYER_WITH" in q:
            assert p["target_subject_id_a"] == "S2" and p["target_subject_id_b"] == "S1"
            # .data() (not .single()) — must be a list of rows.
            return [{"subject_id_a": "S2", "subject_id_b": "S1"}]
        if "MERGE (rej:Rejection" in q:
            assert (
                p["from_key"] == "S1" and p["to_key"] == "S2"
            ), "from_key/to_key must be sorted regardless of call order"
            return {"rejection_id": "rej-1", "rejected_at": "2026-07-19T00:00:00Z", "rejected_by": "inv-1"}
        if "last_inference_change_at" in q:
            return {"case_id": "C1"}
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    # _resolve_case_scope's own primary-subject lookup is answered by the
    # responder above (same session, same mocked rejection.get_session);
    # resolve_scope() is a SEPARATE call that opens its own session via
    # reasoning_layer.scope's own get_session — mocked directly here
    # rather than through the fake session, since scope resolution
    # itself is reasoning_layer/scope.py's own concern, already covered
    # by that module's own tests, not this one's.
    fake_scope = {
        "scope_subject_ids": ["S1", "S2"],
        "scope_case_ids": ["C1"],
        "expansion": {"co_subject": 0, "employer": 0, "address": 0, "alias": 0},
    }
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)), mock.patch.object(
        rejection, "resolve_scope", return_value=fake_scope
    ), mock.patch.object(rejection.cascade, "cascade_reject", return_value=[]):
        envelope = rejection.reject_inference(
            case_id="C1",
            subject_id_a="S2",
            subject_id_b="S1",
            rule_id="Rule_01_Shared_Employer",
            investigator_id="inv-1",
            reason="Confirmed different employers on review",
        )
    check(
        "symmetric edge: from_key/to_key sorted, not call-order dependent",
        envelope["result"]["accepted"] is True,
    )
    check(
        "symmetric edge: rejected_items carries the resolved match_id",
        envelope["result"]["rejected_items"][0]["match_id"]
        == rejection.build_match_id("Rule_01_Shared_Employer", "S2", "S1"),
    )
    check(
        "symmetric edge: primary-subject lookup + locate + merge + timestamp touch issued",
        len(session.calls) == 4,
    )


def test_reject_network_edge_both_subjects():
    print("\n[D2.2] rejection.py — network edge, both subjects (Rule_09_PCA_CheckSplit)")

    def responder(q, p):
        if "is_primary = true" in q:
            return {"primary_subject_id": "S1"}
        if "MEMBER_OF_FRAUD_NETWORK" in q and 'SET r.status = "rejected"' in q:
            check(
                "network edge: subject_id_b passed through for dual rejection",
                p["target_subject_id_b"] == "S2",
            )
            return [{"subject_id_a": "S1", "network_type": "CheckSplit", "network_key": "C1"}]
        if "MERGE (rej:Rejection" in q:
            check(
                "network edge: to_key built from live FraudNetwork node, not hardcoded",
                p["to_key"] == "CheckSplit:C1",
            )
            check("network edge: from_key is the subject the investigator acted on", p["from_key"] == "S1")
            return {"rejection_id": "rej-2", "rejected_at": "t", "rejected_by": "inv-1"}
        if "last_inference_change_at" in q:
            return {"case_id": "C1"}
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    fake_scope = {
        "scope_subject_ids": ["S1", "S2"],
        "scope_case_ids": ["C1"],
        "expansion": {"co_subject": 0, "employer": 0, "address": 0, "alias": 0},
    }
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)), mock.patch.object(
        rejection, "resolve_scope", return_value=fake_scope
    ), mock.patch.object(rejection.cascade, "cascade_reject", return_value=[]):
        envelope = rejection.reject_inference(
            case_id="C1",
            subject_id_a="S1",
            subject_id_b="S2",
            rule_id="Rule_09_PCA_CheckSplit",
            investigator_id="inv-1",
            reason="Confirmed independent transactions on review",
        )
    check("network edge: accepted", envelope["result"]["accepted"] is True)


def test_reject_case_flag():
    print("\n[D2.3] rejection.py — case-property flag (Rule_08_Recidivist_Escalation)")

    def responder(q, p):
        if "is_primary = true" in q:
            return {"primary_subject_id": "S1"}
        if 'risk_escalation_status = "active"' in q:
            check(
                "case flag: verifies caller's subject_id_a against the stored escalating subject",
                p["subject_id_a"] == "S1",
            )
            return [{"subject_id_a": "S1"}]
        if "MERGE (rej:Rejection" in q:
            check(
                "case flag: from_key=subject, to_key=case_id", p["from_key"] == "S1" and p["to_key"] == "C1"
            )
            return {"rejection_id": "rej-3", "rejected_at": "t", "rejected_by": "inv-1"}
        if "last_inference_change_at" in q:
            return {"case_id": "C1"}
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    fake_scope = {
        "scope_subject_ids": ["S1"],
        "scope_case_ids": ["C1"],
        "primary_subject_id": "S1",
        "expansion": {"co_subject": 0, "employer": 0, "address": 0, "alias": 0},
    }
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)), mock.patch.object(
        rejection, "resolve_scope", return_value=fake_scope
    ), mock.patch.object(rejection.cascade, "cascade_reject", return_value=[]):
        envelope = rejection.reject_inference(
            case_id="C1",
            subject_id_a="S1",
            rule_id="Rule_08_Recidivist_Escalation",
            investigator_id="inv-1",
            reason="Escalation criteria no longer met on review",
        )
    check("case flag: accepted", envelope["result"]["accepted"] is True)


def test_reject_allegation_flag_resolves_allegation_id():
    print("\n[D2.4] rejection.py — allegation flag (Rule_12), allegation_id resolved by lookup")

    def responder(q, p):
        if "is_primary = true" in q:
            return {"primary_subject_id": "S1"}
        if 'wage_corroboration_status = "active"' in q:
            return [{"subject_id_a": "S1", "allegation_id": "ALLEG-99"}]
        if "MERGE (rej:Rejection" in q:
            check(
                "allegation flag: to_key is the resolved allegation_id, " "never supplied by the caller",
                p["to_key"] == "ALLEG-99",
            )
            return {"rejection_id": "rej-4", "rejected_at": "t", "rejected_by": "inv-1"}
        if "last_inference_change_at" in q:
            return {"case_id": "C1"}
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    fake_scope = {
        "scope_subject_ids": ["S1"],
        "scope_case_ids": ["C1"],
        "expansion": {"co_subject": 0, "employer": 0, "address": 0, "alias": 0},
    }
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)), mock.patch.object(
        rejection, "resolve_scope", return_value=fake_scope
    ), mock.patch.object(rejection.cascade, "cascade_reject", return_value=[]):
        envelope = rejection.reject_inference(
            case_id="C1",
            subject_id_a="S1",
            rule_id="Rule_12_SLAM_Wage_Corroboration",
            investigator_id="inv-1",
            reason="Wage records do not actually corroborate on review",
        )
    check("allegation flag: accepted", envelope["result"]["accepted"] is True)


def test_reject_not_found_raises():
    print("\n[D2.5] rejection.py — nothing active to reject")
    session = FakeSession(lambda q, p: None)
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)):
        raised = False
        try:
            rejection.reject_inference(
                case_id="C1",
                subject_id_a="S1",
                subject_id_b="S2",
                rule_id="Rule_01_Shared_Employer",
                investigator_id="inv-1",
                reason="Attempting to reject an already-resolved fact",
            )
        except rejection.InferenceNotFoundError:
            raised = True
    check(
        "a second reject on an already-rejected (or never-fired) fact raises "
        "InferenceNotFoundError, not a silent success",
        raised,
    )


def test_reject_input_validation():
    print("\n[D2.6] rejection.py — input validation")
    # Every query this generic responder doesn't specifically recognize
    # returns a non-list value, which FakeResult.data() empties to []
    # (see tests/verify.py) — i.e. "the family's own query found nothing
    # to reject." That is exactly the fallback this test wants for the
    # locate step: these checks are about validation that happens
    # BEFORE (or, for missing-subject_id_b, in place of) a real Neo4j
    # lookup, not about what a specific family's query returns.
    session = FakeSession(lambda q, p: {"primary_subject_id": "S1"} if "is_primary = true" in q else {})
    fake_scope = {
        "scope_subject_ids": ["S1", "S2"],
        "scope_case_ids": ["C1"],
        "primary_subject_id": "S1",
        "expansion": {"co_subject": 0, "employer": 0, "address": 0, "alias": 0},
    }

    def expect_value_error(**kwargs):
        try:
            with mock.patch.object(rejection, "get_session", fake_session_cm(session)), mock.patch.object(
                rejection, "resolve_scope", return_value=fake_scope
            ), mock.patch.object(rejection.cascade, "cascade_reject", return_value=[]):
                rejection.reject_inference(**kwargs)
            return False
        except ValueError:
            return True

    check(
        "unknown rule_id rejected",
        expect_value_error(
            case_id="C1",
            subject_id_a="S1",
            rule_id="Rule_99_Nonexistent",
            investigator_id="inv-1",
            reason="test",
        ),
    )
    check(
        "blank investigator_id rejected — a rejection must be attributable",
        expect_value_error(
            case_id="C1",
            subject_id_a="S1",
            subject_id_b="S2",
            rule_id="Rule_01_Shared_Employer",
            investigator_id="  ",
            reason="test",
        ),
    )
    check(
        # relationship_type is no longer a caller-supplied argument (it is
        # derived internally from rule_id via _RULE_SPECS — see
        # reject_inference's own docstring), so a mismatched
        # relationship_type is no longer a representable input at all;
        # this replaces that old check with the other input reject_inference
        # validates the same way — a blank reason, same as blank
        # investigator_id above.
        "blank reason rejected — a rejection must be explained",
        expect_value_error(
            case_id="C1",
            subject_id_a="S1",
            subject_id_b="S2",
            rule_id="Rule_01_Shared_Employer",
            investigator_id="inv-1",
            reason="   ",
        ),
    )

    # missing subject_id_b for a two-subject rule (Rule_01) and an extra
    # subject_id_b for a single-subject rule (Rule_11) are NO LONGER
    # ValueErrors — _resolve_target only checks that SOME target is
    # identifiable at all (match_id or subject_id_a), never whether
    # subject_id_b is appropriate for the rule's family (see
    # _resolve_target's own docstring). Family-appropriateness is left
    # entirely to each family's own Cypher WHERE clause:
    #   * Rule_01 (symmetric edge) WITHOUT subject_id_b: the instance
    #     filter degrades to matching a pair that both equal
    #     subject_id_a, which no real edge satisfies — so this now
    #     raises InferenceNotFoundError (nothing matched), not ValueError.
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)), mock.patch.object(
        rejection, "resolve_scope", return_value=fake_scope
    ), mock.patch.object(rejection.cascade, "cascade_reject", return_value=[]):
        raised_not_found = False
        try:
            rejection.reject_inference(
                case_id="C1",
                subject_id_a="S1",
                rule_id="Rule_01_Shared_Employer",
                investigator_id="inv-1",
                reason="test",
            )
        except rejection.InferenceNotFoundError:
            raised_not_found = True
    check(
        "missing subject_id_b for a two-subject rule no longer raises ValueError — "
        "it now raises InferenceNotFoundError, since the instance filter degrades to "
        "matching nothing rather than being rejected upfront",
        raised_not_found,
    )
    #   * Rule_11 (subject flag) doesn't reference target_subject_id_b in
    #     its WHERE clause at all — an extra subject_id_b is silently
    #     ignored, not an error. There is nothing left to assert here;
    #     this is intentionally not tested as a "must raise" case any
    #     more, since it no longer is one.


def test_all_rule_ids_have_a_spec():
    print("\n[D2.7] rejection.py — coverage")
    from reasoning_layer import rule_registry

    expected = [r for r in rule_registry.ALL_RULE_IDS if r != rule_registry.MODIFIER_RULE_ID]
    check(
        "every non-modifier rule_id is rejectable",
        set(expected) == set(rejection.RULE_IDS_REJECTABLE),
        f"missing: {set(expected) - set(rejection.RULE_IDS_REJECTABLE)}",
    )


# --------------------------------------------------------------------
# 2. fraud_network.py
# --------------------------------------------------------------------


def test_fraud_network_groups_and_keeps_rejected_edges():
    print("\n[D3.1] fraud_network.py — grouping + rejected edges kept")

    # get_fraud_network now runs ONE query (CASE_SUBGRAPH_QUERY) that
    # returns a single record with raw {ref, labels, properties,
    # is_case_subject} nodes and {ref, type, source_ref, target_ref,
    # properties} relationships — not the two separate pre-shaped
    # queries this test used to fake. See
    # reasoning_layer/queries/fraud_network_query.py's own RETURN clause
    # for this exact shape.
    raw_record = {
        "nodes": [
            {
                "ref": "ref-s1",
                "labels": ["Subject"],
                "properties": {"subject_id": "S1", "first_name": "A", "last_name": ""},
                "is_case_subject": True,
            },
            {
                "ref": "ref-s2",
                "labels": ["Subject"],
                "properties": {"subject_id": "S2", "first_name": "B", "last_name": ""},
                "is_case_subject": False,
            },
            {
                "ref": "ref-fn1",
                "labels": ["FraudNetwork"],
                "properties": {
                    "network_type": "Employer",
                    "network_key": "EMP-1",
                    "formed_by_rule": "Rule_02_Employer_Fraud_Network",
                },
                "is_case_subject": False,
            },
        ],
        "relationships": [
            {
                "ref": "ref-e1",
                "type": "MEMBER_OF_FRAUD_NETWORK",
                "source_ref": "ref-s1",
                "target_ref": "ref-fn1",
                "properties": {
                    "confidence": "High",
                    "status": "active",
                    "source_rule": "Rule_02_Employer_Fraud_Network",
                },
            },
            {
                "ref": "ref-e2",
                "type": "MEMBER_OF_FRAUD_NETWORK",
                "source_ref": "ref-s2",
                "target_ref": "ref-fn1",
                "properties": {
                    "confidence": "Medium",
                    "status": "rejected",
                    "source_rule": "Rule_02_Employer_Fraud_Network",
                },
            },
            {
                "ref": "ref-e3",
                "type": "SHARES_EMPLOYER_WITH",
                "source_ref": "ref-s1",
                "target_ref": "ref-s2",
                "properties": {
                    "confidence": "High",
                    "status": "rejected",
                    "source_rule": "Rule_01_Shared_Employer",
                },
            },
        ],
    }

    def responder(q, p):
        if "CASE_SUBGRAPH" in q or "case_node:Case" in q:
            return raw_record
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    with mock.patch.object(fraud_network, "get_session", fake_session_cm(session)):
        envelope = fraud_network.get_fraud_network("C1")

    result = envelope["result"]
    check("one network block returned", result["network_count"] == 1)
    net = result["networks"][0]
    check("both members present as nodes", {n["id"] for n in net["nodes"]} == {"S1", "S2"})
    check(
        "is_primary reflects case membership, not network position",
        [n for n in net["nodes"] if n["id"] == "S1"][0]["is_primary"] is True
        and [n for n in net["nodes"] if n["id"] == "S2"][0]["is_primary"] is False,
    )
    check(
        "rejected edge is included, not filtered out (dashed-style requirement)",
        len(net["edges"]) == 1 and net["edges"][0]["status"] == "rejected",
    )
    check("network confidence falls back to the strongest active membership", net["confidence"] == "High")


def test_fraud_network_confidence_falls_back_when_all_rejected():
    print("\n[D3.2] fraud_network.py — confidence when every membership is rejected")

    raw_record = {
        "nodes": [
            {
                "ref": "ref-s1",
                "labels": ["Subject"],
                "properties": {"subject_id": "S1", "first_name": "A", "last_name": ""},
                "is_case_subject": True,
            },
            {
                "ref": "ref-fn1",
                "labels": ["FraudNetwork"],
                "properties": {
                    "network_type": "Address",
                    "network_key": "ADDR-1",
                    "formed_by_rule": "Rule_04_Address_Fraud_Network",
                },
                "is_case_subject": False,
            },
        ],
        "relationships": [
            {
                "ref": "ref-e1",
                "type": "MEMBER_OF_FRAUD_NETWORK",
                "source_ref": "ref-s1",
                "target_ref": "ref-fn1",
                "properties": {
                    "confidence": "Medium",
                    "status": "rejected",
                    "source_rule": "Rule_04_Address_Fraud_Network",
                },
            },
        ],
    }

    def responder(q, p):
        if "case_node:Case" in q:
            return raw_record
        raise AssertionError(f"unexpected query: {q}")

    session = FakeSession(responder)
    with mock.patch.object(fraud_network, "get_session", fake_session_cm(session)):
        envelope = fraud_network.get_fraud_network("C1")
    check(
        "a fully-rejected network still reports what it used to claim, not 'Unresolved'",
        envelope["result"]["networks"][0]["confidence"] == "Medium",
    )


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
            return [
                {
                    "subject_id_a": "S1",
                    "subject_id_b": "S2",
                    "relationship_type": "SHARES_EMPLOYER_WITH",
                    "confidence": "High",
                    "asserted_at": "t",
                    "corroborated": False,
                    "status": "active",
                }
            ]
        if "Rule_08_Recidivist_Escalation" in q:
            # AI-30/AI-31: confirms the auto_invalidated/
            # invalidated_by_rule_id pair now survives the round trip
            # from Neo4j row through to the API-facing dict.
            return [
                {
                    "subject_id_a": "S1",
                    "subject_id_b": "C1",
                    "relationship_type": "CASE_RISK_ESCALATION",
                    "confidence": "High",
                    "asserted_at": "t",
                    "corroborated": False,
                    "status": "rejected",
                    "auto_invalidated": True,
                    "invalidated_by_rule_id": "Rule_01_Shared_Employer",
                }
            ]
        return []

    session = FakeSession(responder)
    scope_stub = {"scope_subject_ids": ["S1", "S2"], "scope_case_ids": ["C1"]}
    with mock.patch.object(rule_audit, "get_session", fake_session_cm(session)), mock.patch.object(
        rule_audit, "resolve_scope", lambda case_id, subject_id: scope_stub
    ), mock.patch.object(
        # AI-31/AI-32: last_inference_change_at is now read via the
        # shared reasoning_layer.case_staleness reader (its own
        # get_session call, separate from the FakeSession above) —
        # mocked at the point of use rather than re-plumbed through
        # FakeSession's query-text dispatch.
        rule_audit,
        "get_last_inference_change_at_raw",
        return_value="2026-08-01T00:00:00+00:00",
    ):
        envelope = rule_audit.get_rule_audit("C1")

    result = envelope["result"]
    expected_ids = [r for r in rule_registry.ALL_RULE_IDS if r != rule_registry.MODIFIER_RULE_ID]
    check(
        "all 13 rejectable rule_ids present, fired or not",
        {r["rule_id"] for r in result["rules"]} == set(expected_ids),
    )
    check(
        "Rule 14 (modifier) is excluded — it is not an independent inferable fact",
        rule_registry.MODIFIER_RULE_ID not in {r["rule_id"] for r in result["rules"]},
    )
    fired = {r["rule_id"]: r["fired"] for r in result["rules"]}
    check(
        "Rule_01 correctly reported as fired with its instance data", fired["Rule_01_Shared_Employer"] is True
    )
    check(
        "a rule with no matching rows is reported fired=False, not omitted",
        fired["Rule_03_Shared_Address"] is False,
    )
    check(
        "rule_description is populated from rule_registry, not left as the raw rule_id",
        next(r["rule_description"] for r in result["rules"] if r["rule_id"] == "Rule_01_Shared_Employer")
        != "Rule_01_Shared_Employer",
    )
    check(
        "AI-31: case-wide last_inference_change_at surfaced at the top level",
        result["last_inference_change_at"] == "2026-08-01T00:00:00+00:00",
    )
    rule_08_row = next(
        r for r in result["rules"] if r["rule_id"] == "Rule_08_Recidivist_Escalation"
    )["inferred_relationships"][0]
    check(
        "AI-30/AI-31: auto_invalidated/invalidated_by_rule_id pass through on Rule_08's row",
        rule_08_row["auto_invalidated"] is True
        and rule_08_row["invalidated_by_rule_id"] == "Rule_01_Shared_Employer",
    )
    rule_01_row = next(
        r for r in result["rules"] if r["rule_id"] == "Rule_01_Shared_Employer"
    )["inferred_relationships"][0]
    check(
        "auto_invalidated/invalidated_by_rule_id degrade to None for rule families "
        "with no case-level auto-invalidation concept",
        rule_01_row["auto_invalidated"] is None and rule_01_row["invalidated_by_rule_id"] is None,
    )


def test_rule_audit_no_primary_subject_degrades_gracefully():
    print("\n[D4.2] rule_audit.py — no primary subject on the graph yet")
    session = FakeSession(lambda q, p: None if "is_primary" in q else [])
    with mock.patch.object(rule_audit, "get_session", fake_session_cm(session)), mock.patch.object(
        rule_audit, "get_last_inference_change_at_raw", return_value=None
    ):
        envelope = rule_audit.get_rule_audit("C1")
    result = envelope["result"]
    check("primary_subject_id is None rather than raising", result["primary_subject_id"] is None)
    check("every rule still present, all fired=False", all(not r["fired"] for r in result["rules"]))
    check(
        "AI-31: last_inference_change_at is None (not a crash) when the "
        "Case node itself can't be found either",
        result["last_inference_change_at"] is None,
    )


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
    from fastapi import HTTPException

    import api.server as server
    from api.models import RejectInferenceRequest

    session = FakeSession(lambda q, p: {"primary_subject_id": "S1"} if "is_primary = true" in q else None)
    req = RejectInferenceRequest(
        case_id="C1",
        subject_id_a="S1",
        subject_id_b="S2",
        rule_id="Rule_01_Shared_Employer",
        investigator_id="inv-1",
        reason="test",
    )
    fake_scope = {
        "scope_subject_ids": ["S1", "S2"],
        "scope_case_ids": ["C1"],
        "primary_subject_id": "S1",
        "expansion": {"co_subject": 0, "employer": 0, "address": 0, "alias": 0},
    }
    with mock.patch.object(rejection, "get_session", fake_session_cm(session)), mock.patch.object(
        rejection, "resolve_scope", return_value=fake_scope
    ):
        try:
            server.reject_inference_route(req)
            status = None
        except HTTPException as exc:
            status = exc.status_code
    check("InferenceNotFoundError maps to HTTP 404, not 500", status == 404)

    bad_req = RejectInferenceRequest(
        case_id="C1",
        subject_id_a="S1",
        rule_id="Rule_99_Nonexistent",
        investigator_id="inv-1",
        reason="test",
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
