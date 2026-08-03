"""
Regression test for the duplicate-instance display bug: two physical
:SHARES_EMPLOYER_WITH (etc.) relationships between the same pair of
subjects — created by MERGE on an undirected relationship pattern not
being reliably idempotent — must never be shown to an investigator as
two separate rows for the same fact.

Covers reasoning_layer/rules_fired.py's _dedupe_rows() directly (unit),
and build_rules_fired() end-to-end against a fake session returning the
exact duplicate-row shape the live case that surfaced this bug showed
(integration, no live Neo4j needed).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from typing import Any, Dict, List
from unittest import mock


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

from reasoning_layer import rules_fired  # noqa: E402
from reasoning_layer.rejection import decode_match_id  # noqa: E402


# --------------------------------------------------------------------
# _dedupe_rows — unit level
# --------------------------------------------------------------------


def test_dedupe_rows_collapses_identical_duplicate_instance():
    """The exact shape reported in production: two rows, same subject_id/
    related_subject_id, same everything else — must collapse to one."""
    row = {
        "subject_id": "658636801",
        "related_subject_id": "658653191",
        "confidence": "High",
        "corroborated": False,
        "status": "rejected",
    }
    rows = [dict(row), dict(row)]  # two separate dict objects, identical content

    deduped = rules_fired._dedupe_rows(rows)

    assert len(deduped) == 1
    assert deduped[0]["subject_id"] == "658636801"
    assert deduped[0]["related_subject_id"] == "658653191"


def test_dedupe_rows_keeps_distinct_instances():
    """Two genuinely different matches for the same rule (A-B and A-C)
    must NOT be collapsed into one."""
    rows = [
        {"subject_id": "A", "related_subject_id": "B", "status": "active"},
        {"subject_id": "A", "related_subject_id": "C", "status": "active"},
    ]

    deduped = rules_fired._dedupe_rows(rows)

    assert len(deduped) == 2
    assert {r["related_subject_id"] for r in deduped} == {"B", "C"}


def test_dedupe_rows_keeps_first_occurrence_deterministically():
    """When rows differ (e.g. stale property drift between the two
    physical duplicates), the FIRST row wins — deterministic given every
    _REL_RULES/_PROP_RULES query orders its results."""
    first = {"subject_id": "A", "related_subject_id": "B", "confidence": "High"}
    second = {"subject_id": "A", "related_subject_id": "B", "confidence": "Medium"}

    deduped = rules_fired._dedupe_rows([first, second])

    assert len(deduped) == 1
    assert deduped[0]["confidence"] == "High"


def test_dedupe_rows_empty_list():
    assert rules_fired._dedupe_rows([]) == []


def test_case_flag_family_produces_match_id_matching_rule_audit():
    """
    Regression test: Rule_08/13 (case-flag family) originally shipped
    their rules_fired.py read queries returning no `subject_id` at all
    (only `related_case_id`), so _build_instance_match_id had nothing
    to build a token from and /intake's graph_findings.rules_fired
    always showed match_id: null for these two rules — even though
    /rule_audit's separate, differently-shaped query for the SAME two
    rules DID return a subject_id (c.risk_escalation_subject_id for
    Rule_08, the $subject_id param for Rule_13) and so DID produce a
    match_id. Fixed by adding subject_id to both RETURN clauses (and
    threading $subject_id, from scope.primary_subject_id, into
    build_rules_fired's query params for Rule_13's query to read).

    This locks in that both rules now produce a match_id, AND that it
    is byte-identical to the token rule_audit.py would build for the
    same fact (verified against a real production response pair).
    """
    row8 = {
        "subject_id": "658636801",
        "related_case_id": "658407433",
        "confidence": "High",
        "corroborated": False,
        "status": "active",
        "detail": {"complaint_no": "101685", "fraud_amount": 47850},
    }
    inst8 = rules_fired._instance("Rule_08_Recidivist_Escalation", row8)
    assert inst8["match_id"] is not None
    assert rules_fired.build_match_id("Rule_08_Recidivist_Escalation", "658636801", "658407433") == inst8[
        "match_id"
    ]

    row13 = {
        "subject_id": "658636801",
        "related_case_id": "658407433",
        "confidence": "High",
        "corroborated": False,
        "status": "active",
        "detail": {"complaint_no": "101685", "fraud_amount": 47850},
    }
    inst13 = rules_fired._instance("Rule_13_FastTrack_Escalation", row13)
    assert inst13["match_id"] is not None
    assert rules_fired.build_match_id("Rule_13_FastTrack_Escalation", "658636801", "658407433") == inst13[
        "match_id"
    ]


def test_case_flag_family_match_id_is_none_without_a_resolved_primary_subject():
    """If scope never resolved a primary subject, $subject_id is null in
    Neo4j and the row's subject_id comes back None — match_id must
    degrade to None gracefully, not raise or build a garbage token."""
    row = {
        "subject_id": None,
        "related_case_id": "658407433",
        "confidence": "High",
        "corroborated": False,
        "status": "active",
    }
    inst = rules_fired._instance("Rule_13_FastTrack_Escalation", row)
    assert inst.get("match_id") is None
    assert "subject_id" not in inst, "subject_id key itself must be omitted, not present as None"


def test_network_family_stamps_a_subject_specific_match_id_per_member():
    """
    AI-28 completeness gap: the network family (Rule_02/04/06/09)
    collapses every member into ONE instance row for a single readable
    narrative — but that meant the row's single top-level match_id
    could only ever target the anchor member, unlike rule_audit.py /
    fraud_network.py which return one row per member and so already let
    an investigator reject ANY specific member. Every member in
    detail["members"] must now carry its own match_id, decodable back
    to (rule_id, that member's own subject_id, the network composite
    key) — byte-identical to what rule_audit.py would build for that
    same member.
    """
    row = {
        "subject_id": "658636801",
        "related_network_key": "FEIN:047821334",
        "confidence": "High",
        "corroborated": False,
        "status": "active",
        "detail": {
            "formed_by_rule": "Rule_02_Employer_Fraud_Network",
            "network_key": "FEIN:047821334",
            "network_type": "Employer",
            "members": [
                {
                    "subject_id": "658636801",
                    "first_name": "John",
                    "last_name": "Smith",
                    "complaint_no": "101685",
                    "allegation_type": "Employment",
                    "status": "active",
                },
                {
                    "subject_id": "658653191",
                    "first_name": "Kevin",
                    "last_name": "Nunes",
                    "complaint_no": "101692",
                    "allegation_type": "Employment",
                    "status": "active",
                },
            ],
        },
    }

    inst = rules_fired._instance("Rule_02_Employer_Fraud_Network", row)
    members = inst["detail"]["members"]
    assert len(members) == 2

    by_subject = {m["subject_id"]: m for m in members}
    assert by_subject["658636801"]["match_id"] == inst["match_id"], (
        "the anchor member's own match_id must equal the row's top-level match_id"
    )

    kevin_match_id = by_subject["658653191"]["match_id"]
    assert kevin_match_id is not None
    assert kevin_match_id != inst["match_id"], "a non-anchor member must get its OWN distinct match_id"
    assert kevin_match_id == rules_fired.build_match_id(
        "Rule_02_Employer_Fraud_Network", "658653191", "Employer:FEIN:047821334"
    )

    decoded = decode_match_id(kevin_match_id)
    assert decoded == ("Rule_02_Employer_Fraud_Network", "658653191", "Employer:FEIN:047821334")


def test_network_family_member_stamping_never_mutates_the_input_row():
    """_stamp_member_match_ids must return fresh copies, never mutate the
    row's own member dicts/list — a row can be reused elsewhere in the
    same request (e.g. re-summarised) and must not carry a stamp it
    wasn't asked for the second time."""
    original_members = [{"subject_id": "658636801", "status": "active"}]
    row = {
        "subject_id": "658636801",
        "related_network_key": "FEIN:047821334",
        "status": "active",
        "detail": {
            "network_key": "FEIN:047821334",
            "network_type": "Employer",
            "members": original_members,
        },
    }

    rules_fired._instance("Rule_02_Employer_Fraud_Network", row)

    assert "match_id" not in original_members[0], "the original member dict passed in must be untouched"
    assert original_members is not row["detail"]["members"] or "match_id" not in original_members[0]


def test_network_family_without_members_or_network_keys_is_a_no_op():
    """A malformed/partial detail (missing members, network_type, or
    network_key) must not raise — _stamp_member_match_ids degrades to
    doing nothing rather than crashing the whole rules_fired build."""
    row_no_members = {
        "subject_id": "658636801",
        "related_network_key": "FEIN:047821334",
        "status": "active",
        "detail": {"network_key": "FEIN:047821334", "network_type": "Employer"},
    }
    inst = rules_fired._instance("Rule_02_Employer_Fraud_Network", row_no_members)
    assert "members" not in inst.get("detail", {})

    row_no_network_type = {
        "subject_id": "658636801",
        "related_network_key": "FEIN:047821334",
        "status": "active",
        "detail": {"network_key": "FEIN:047821334", "members": [{"subject_id": "658636801"}]},
    }
    inst2 = rules_fired._instance("Rule_02_Employer_Fraud_Network", row_no_network_type)
    assert inst2["detail"]["members"][0].get("match_id") is None


# --------------------------------------------------------------------
# build_rules_fired — integration level, fake session
# --------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def data(self):
        return self._rows


class FakeSession:
    """Returns the SAME duplicate-row pair for whichever rule_id's query
    is run, and an empty result for every other rule — mirrors the
    production case exactly: only Rule_01 has the duplicate."""

    def __init__(self, duplicated_rule_id: str, duplicate_rows: List[Dict[str, Any]]):
        self._duplicated_rule_id = duplicated_rule_id
        self._duplicate_rows = duplicate_rows

    def run(self, query, **params):
        # rules_fired.py's _REL_RULES/_PROP_RULES don't take rule_id as a
        # bind param — the query text itself IS rule-specific — so match
        # on a distinctive substring instead of a rule_id parameter.
        if "SHARES_EMPLOYER_WITH" in query and self._duplicated_rule_id == "Rule_01_Shared_Employer":
            return FakeResult(self._duplicate_rows)
        return FakeResult([])

    def close(self):
        pass


def fake_session_cm(session):
    @contextmanager
    def _cm(*args, **kwargs):
        yield session

    return _cm


def test_build_rules_fired_deduplicates_a_real_duplicate_relationship():
    """End-to-end: Rule_01 fires with a duplicate row pair (identical to
    the production case) — the final block must show exactly ONE
    instance, evidence_count 1, not 2."""
    duplicate_row = {
        "subject_id": "658636801",
        "first_name": "John",
        "last_name": "Smith",
        "related_subject_id": "658653191",
        "related_first_name": "Kevin",
        "related_last_name": "Nunes",
        "confidence": "High",
        "corroborated": False,
        "status": "active",
        "rejection": {},
        "detail": {"fein": "047821334"},
    }
    session = FakeSession("Rule_01_Shared_Employer", [dict(duplicate_row), dict(duplicate_row)])

    scope = {
        "case_id": "658407433",
        "primary_subject_id": "658636801",
        "scope_subject_ids": ["658636801", "658653191"],
        "scope_case_ids": ["658407433"],
    }

    with mock.patch.object(rules_fired, "get_session", fake_session_cm(session)):
        block = rules_fired.build_rules_fired(scope, execution_records=[])

    rule_01 = next(e for e in block if e["rule_id"] == "Rule_01_Shared_Employer")
    assert rule_01["evidence_count"] == 1, "duplicate physical relationship must collapse to one instance"
    assert len(rule_01["instances"]) == 1
    assert rule_01["instances"][0]["related_subject_id"] == "658653191"
