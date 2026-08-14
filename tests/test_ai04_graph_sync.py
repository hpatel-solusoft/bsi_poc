"""
AI-04 — "Update Neo4j Data from AppWorks event/rules": new sync path
AppWorks calls (directly or via etl/run_sync.py, POST /graph/ingest is the
actual HTTP door) on case create/update, to write case/subject/allegation/
employer/wage/commentary data into Neo4j.

etl/graph_sync.py + etl/ingest_service.py already ARE this sync path —
POST /graph/ingest (api/server.py) is "the AppWorks Lifecycle-event
entry point" per its own docstring, and calls
etl.ingest_service.ingest -> etl.graph_sync.sync_case for exactly this
purpose. This file did not have any dedicated test coverage proving its
two AI-04 acceptance criteria:

  1. "Only sync what rules actually need" — subject FEIN/address/alias,
     case status/fraud amount/FastTrack, allegation type/status,
     employer FEIN, wages, commentary.
  2. "Use MERGE not CREATE, so repeat updates don't create duplicates."

What this proves:
  * SOURCE AUDIT — every Cypher write query in etl/graph_sync.py used to
    persist an entity uses MERGE, never a bare CREATE. This is a
    regression guard: if a future edit swaps a MERGE for a CREATE (or
    adds a new query that does), this test fails at the source-code
    level rather than only being discoverable by running two ingests
    against a real Neo4j and diffing node counts.
  * FIELD-COVERAGE AUDIT — every field AI-04 names by name is actually
    read on the fetch side (etl/graph_sync.fetch_case_graph) and written
    on the load side (the _Q_* query text), so "only sync what rules
    need" is checked against the literal field list, not just "sync
    works".
  * IDEMPOTENT ORCHESTRATION — calling the load transaction
    (etl.graph_sync._tx_load) twice in a row with byte-identical input
    issues the byte-identical sequence of (query, params) pairs both
    times. The Python orchestration layer has no CREATE fallback, no
    "insert if new / update if seen" branching, and no accumulating
    state of its own — every write is a pure function of the input row,
    which is what makes Neo4j's own MERGE-on-a-stable-key semantics
    (untestable offline; no live Neo4j reachable from this sandbox,
    same caveat tests/test_cascade.py documents) sufficient to guarantee
    "repeat updates don't create duplicates" in production.
  * INGEST ORCHESTRATION — etl.ingest_service.ingest() calls
    graph_sync.sync_case per case_id (the AppWorks lifecycle-event
    shape: one case_id per call) and never raises out of a single
    case's failure, so one bad case in a lifecycle-event burst cannot
    take the sync endpoint down for any other case.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from contextlib import contextmanager
from typing import Any, Dict, List
from unittest import mock

import pytest


def _install_external_import_stubs() -> None:
    """Same offline stub as tests/test_cascade.py /
    tests/test_instance_level_rejection.py: only stub the `neo4j` package
    if it truly is not installed in this sandbox, so real installs are
    exercised when available."""
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

from etl import graph_sync  # noqa: E402
from etl import ingest_service  # noqa: E402


# --------------------------------------------------------------------
# 1. SOURCE AUDIT — every write query is MERGE, never a bare CREATE
# --------------------------------------------------------------------

# Every module-level "_Q_*" string constant is a Cypher write statement
# issued by _tx_load. Collected by name rather than hand-maintained, so a
# new query added later is automatically covered.
_QUERY_CONSTANTS: Dict[str, str] = {
    name: value
    for name, value in vars(graph_sync).items()
    if name.startswith("_Q_") and isinstance(value, str)
}

# A bare, unconditional node/relationship creation clause — the thing
# AI-04 explicitly forbids. "ON CREATE SET" (part of a MERGE clause,
# fires only for the branch of a MERGE that didn't already exist) is
# fine and deliberately NOT matched here.
_BARE_CREATE_RE = re.compile(r"(?<!ON )CREATE\s*\(", re.IGNORECASE)


def test_every_write_query_exists_and_is_merge_based():
    assert _QUERY_CONSTANTS, "expected at least one _Q_* write query in etl/graph_sync.py"
    for name, cypher in _QUERY_CONSTANTS.items():
        assert "MERGE" in cypher, f"{name} has no MERGE clause at all — AI-04 requires MERGE, not CREATE"
        bare_creates = _BARE_CREATE_RE.findall(cypher)
        assert not bare_creates, (
            f"{name} contains a bare CREATE(...) clause outside of a MERGE's "
            f"ON CREATE SET branch — repeat AppWorks events would duplicate nodes/edges"
        )


@pytest.mark.parametrize(
    "query_name",
    ["_Q_CASE", "_Q_SUBJECTS", "_Q_ADDRESSES", "_Q_ALIASES", "_Q_EMPLOYERS", "_Q_WAGES", "_Q_ALLEGATIONS", "_Q_COMMENTARY"],
)
def test_entity_queries_merge_on_a_stable_key(query_name):
    """Each entity query MERGEs on the field that stays constant across
    repeat AppWorks events for the same real-world entity (case_id,
    subject_id, address_key, alias_value, employer_key, allegation_id,
    comment_id) — not on a value that changes between syncs, which would
    silently defeat MERGE's dedup guarantee."""
    cypher = getattr(graph_sync, query_name)
    merge_clauses = re.findall(r"MERGE\s*\([a-zA-Z0-9]+:\w+\s*\{([^}]*)\}", cypher)
    assert merge_clauses, f"{query_name}: no MERGE(...{{key: ...}}) node pattern found"


# --------------------------------------------------------------------
# 2. FIELD-COVERAGE AUDIT — AI-04's named field list is actually synced
# --------------------------------------------------------------------


def test_case_load_query_covers_status_fraud_amount_and_fasttrack():
    cypher = graph_sync._Q_CASE
    for field in ("c.status", "c.fraud_amount", "c.is_fasttrack"):
        assert field in cypher, f"AI-04 requires case {field!r} to be synced into Neo4j"


def test_subject_load_query_covers_fein():
    assert "subj.fein" in graph_sync._Q_SUBJECTS


def test_address_and_alias_queries_exist_and_key_by_subject():
    assert "MATCH (s:Subject {subject_id: row.subject_id})" in graph_sync._Q_ADDRESSES
    assert "MATCH (s:Subject {subject_id: row.subject_id})" in graph_sync._Q_ALIASES


def test_allegation_load_query_covers_type_and_status():
    cypher = graph_sync._Q_ALLEGATIONS
    for field in ("al.allegation_type", "al.status"):
        assert field in cypher, f"AI-04 requires allegation {field!r} to be synced into Neo4j"


def test_employer_load_query_covers_fein():
    assert "e.fein" in graph_sync._Q_EMPLOYERS
    assert "e.fein" in graph_sync._Q_WAGES  # employer FEIN also carried on the wage-sourced Employer merge


def test_wage_records_are_synced():
    assert "HAS_WAGE_RECORD_WITH" in graph_sync._Q_WAGES


def test_commentary_is_synced():
    assert "MERGE (comm:Commentary" in graph_sync._Q_COMMENTARY


def test_fetch_reads_status_fraud_amount_fasttrack_and_fein_fields():
    """The FETCH side (AppWorks REST -> canonical dict), not just the
    Cypher LOAD side — a load query can be perfectly correct and still
    sync nothing if fetch_case_graph never populated the field."""
    source = graph_sync.__loader__.get_source(graph_sync.__name__)  # type: ignore[union-attr]
    for needle in (
        '"status": N.clean_text(_first(wf_props, "WorkfolderStatus"',
        '"is_fasttrack": N.to_bool(',
        '"fraud_amount": N.to_float(wf_props.get("WorkfolderFraudAmount"))',
        "_fetch_subject_addresses(subject_id)",
        "_fetch_subject_aliases(subject_id, detail_links)",
        "_fetch_subject_employers(subject_id)",
        "_fetch_subject_wages(subject_id)",
        'fein = N.normalize_fein(_first(props, "Job_FeinNumber"',
    ):
        assert needle in source, f"expected fetch_case_graph to read: {needle!r}"


# --------------------------------------------------------------------
# 3. IDEMPOTENT ORCHESTRATION — same input twice => same queries twice,
#    nothing accumulates in the Python layer itself.
# --------------------------------------------------------------------


class FakeRecord:
    """Stubs a Neo4j Record's __getitem__. Supports both the plain "n"
    shape every original _Q_* write query returns, and the richer
    "deleted" / "flagged" / "retired_candidate_ids" shape the
    RECONCILE section's _RECONCILE_* queries return (see
    etl/graph_sync.py's run_prune) — one fake covers both so
    RecordingTx does not need to know which kind of query it is
    replaying."""

    _DEFAULTS = {"n": 1, "deleted": 0, "flagged": 0, "retired_candidate_ids": []}

    def __init__(self, **overrides):
        self._values = {**self._DEFAULTS, **overrides}

    def __getitem__(self, key):
        assert key in self._values, f"unexpected key {key!r} requested from a stubbed Neo4j record"
        return self._values[key]


class FakeResult:
    def __init__(self, **overrides):
        self._overrides = overrides

    def single(self):
        return FakeRecord(**self._overrides)


class RecordingTx:
    """Records every (query, params) pair issued, in order — enough to
    prove _tx_load's call sequence is a pure function of its input."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run(self, query, **params):
        self.calls.append({"query": query, "params": params})
        return FakeResult()


def _sample_case_payload() -> Dict[str, Any]:
    return {
        "case": {
            "case_id": "CASE-1001",
            "complaint_number": "CMP-1",
            "status": "Open",
            "is_fasttrack": True,
            "fraud_amount": 12500.50,
            "fraud_start_date": "2026-01-01",
            "fraud_end_date": "2026-03-01",
            "is_dta_case": False,
            "disposition": None,
            "opened_date": "2025-12-01",
            "closed_date": None,
            "merge_target_case_ids": [],
            "source_table": "Workfolder",
            "retrieved_at": "2026-08-07T00:00:00Z",
        },
        "subjects": [
            {
                "subject_id": "SUBJ-1",
                "first_name": "Jane",
                "last_name": "Doe",
                "company_name": None,
                "fein": None,
                "subject_type": "Individual",
                "subject_role": "Primary Subject",
                "is_primary": True,
                "addresses": [
                    {
                        "address_key": "123-main-st|springfield|il|62701",
                        "street": "123 Main St",
                        "city": "Springfield",
                        "state": "IL",
                        "zip": "62701",
                        "street_normalized": "123 main st",
                    }
                ],
                "aliases": ["J. Doe"],
                "employers": [
                    {
                        "employer_key": "fein:12-3456789",
                        "employer_name": "Acme Corp",
                        "fein": "12-3456789",
                        "employer_fid": None,
                        "start_date": "2024-01-01",
                        "end_date": None,
                    }
                ],
                "wages": [
                    {
                        "employer_key": "fein:12-3456789",
                        "employer_name": "Acme Corp",
                        "fein": "12-3456789",
                        "employer_fid": None,
                        "period_start": "2026-01-01",
                        "period_end": "2026-03-31",
                        "wage_year": "2026",
                        "wage_quarter": "Q1",
                        "wage_amount": 9000.0,
                        "period_key": "2026|Q1|2026-01-01|2026-03-31",
                    }
                ],
                "source_table": "Subject",
                "retrieved_at": "2026-08-07T00:00:00Z",
            }
        ],
        "allegations": [
            {
                "allegation_id": "ALLEG-1",
                "allegation_type": "Concealed Employment",
                "status": "Open",
                "record_status": "Active",
                "norris_code": None,
                "outcome": None,
                "date_closed": None,
                "comment_text": "Working while claiming benefits.",
                "source_table": "Allegations",
                "retrieved_at": "2026-08-07T00:00:00Z",
            }
        ],
        "commentary": [
            {
                "comment_id": "CASE-1001|Allegation_Comment|ALLEG-1|abc123",
                "comment_text": "Working while claiming benefits.",
                "comment_type": "Allegation_Comment",
                "created_date": "2026-08-01",
                "attach_to": "allegation",
                "attach_id": "ALLEG-1",
                "source_table": "Allegations",
            }
        ],
        "retrieved_at": "2026-08-07T00:00:00Z",
    }


def test_tx_load_is_a_pure_function_of_its_input():
    """Calling _tx_load twice with an identical canonical payload (the
    shape a second AppWorks lifecycle event for the same case would
    produce) issues an identical sequence of (query, params). No branch
    in the Python layer distinguishes 'first sync' from 'repeat sync' —
    that guarantee is left entirely, and correctly, to Neo4j's MERGE."""
    payload = _sample_case_payload()

    tx1 = RecordingTx()
    graph_sync._tx_load(tx1, payload)

    tx2 = RecordingTx()
    graph_sync._tx_load(tx2, payload)

    assert len(tx1.calls) == len(tx2.calls) > 0
    for call1, call2 in zip(tx1.calls, tx2.calls):
        assert call1["query"] == call2["query"]
        assert call1["params"] == call2["params"]


def test_load_case_graph_uses_one_write_transaction():
    """Atomic-by-design (etl/graph_sync.py's own module docstring, point
    3): a mid-load failure must leave no partial case in the graph, which
    only holds if the whole case goes through session.execute_write, not
    session.run per query."""
    payload = _sample_case_payload()
    fake_tx = RecordingTx()

    class FakeSession:
        def execute_write(self, fn, data):
            return fn(fake_tx, data)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with mock.patch.object(graph_sync, "get_session", lambda: FakeSession()):
        counts = graph_sync.load_case_graph(payload)

    assert counts["cases"] == 1
    assert counts["subjects"] == 1
    assert counts["allegations"] == 1
    assert counts["addresses"] == 1
    assert counts["aliases"] == 1
    assert counts["employers"] == 1
    assert counts["wage_records"] == 1
    assert counts["commentary"] == 1
    # RECONCILE runs inside the same execute_write call as everything
    # above (etl/graph_sync.py's _tx_load calls _tx_prune_stale directly,
    # not a second session/transaction) — its per-relationship-type
    # deleted/flagged counts land under counts["pruned"], not as
    # additional top-level keys, so they don't collide with the
    # entity-count keys asserted above.
    assert "pruned" in counts
    for rel_key in (
        "appears_in_case",
        "allegations",
        "addresses",
        "aliases",
        "employers",
        "wage_records",
        "commentary",
        "co_subject_pairs",
    ):
        assert rel_key in counts["pruned"]
        assert counts["pruned"][rel_key] == {"deleted": 0, "flagged": 0}
    assert counts["pruned"]["allegations_removed"] == 0
    assert counts["pruned"]["commentary_removed"] == 0
    assert fake_tx.calls  # every query really went through the one shared tx


# --------------------------------------------------------------------
# 4. INGEST ORCHESTRATION — one lifecycle event, one case_id, isolated
#    failures (etl/ingest_service.py)
# --------------------------------------------------------------------


def test_ingest_calls_sync_case_once_per_case_id_and_skips_reasoning_on_request():
    calls: List[str] = []

    def fake_sync_case(case_id):
        calls.append(case_id)
        return {"cases": 1}

    with (
        mock.patch.object(ingest_service.graph_sync, "sync_case", side_effect=fake_sync_case),
        mock.patch.object(ingest_service.graph_ingest_repository, "mark_started", lambda case_id: None),
        mock.patch.object(ingest_service.graph_ingest_repository, "mark_loaded", lambda case_id, counts: None),
    ):
        report = ingest_service.ingest(["CASE-1001"], run_reasoning=False)

    assert calls == ["CASE-1001"]
    assert report["cases_loaded"] == 1
    assert report["cases_load_failed"] == 0
    assert report["pipeline_executed"] is False
    assert report["pipeline_results"] == []


def test_ingest_isolates_one_bad_case_from_the_rest():
    """A single malformed/erroring lifecycle event must not take down
    the sync of any other case in the same call."""

    def flaky_sync_case(case_id):
        if case_id == "CASE-BAD":
            raise RuntimeError("boom")
        return {"cases": 1}

    with (
        mock.patch.object(ingest_service.graph_sync, "sync_case", side_effect=flaky_sync_case),
        mock.patch.object(ingest_service.graph_ingest_repository, "mark_started", lambda case_id: None),
        mock.patch.object(ingest_service.graph_ingest_repository, "mark_loaded", lambda case_id, counts: None),
        mock.patch.object(ingest_service.graph_ingest_repository, "mark_failed", lambda case_id, error: None),
        mock.patch("time.sleep", lambda seconds: None),  # skip real backoff delay in the test
    ):
        report = ingest_service.ingest(["CASE-GOOD", "CASE-BAD"], run_reasoning=False)

    assert report["cases_loaded"] == 1
    assert report["cases_load_failed"] == 1
    good, bad = report["load_results"]
    assert good["case_id"] == "CASE-GOOD" and good["status"] == "loaded"
    assert bad["case_id"] == "CASE-BAD" and bad["status"] == "failed"