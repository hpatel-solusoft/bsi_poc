"""
RECONCILE — etl/graph_sync.py's stale-edge pruning section.

tests/test_ai04_graph_sync.py already proves the ORIGINAL AI-04
acceptance criteria (every write is MERGE, only named fields sync,
_tx_load is a pure function of its input, one write transaction, ingest
isolates one bad case). It does not cover the gap those MERGE-only
writes leave open: a record deleted at the AppWorks source stays in
Neo4j forever, because MERGE only ever creates or updates. This file
covers the RECONCILE section (etl/graph_sync.py, between _Q_CO_SUBJECTS
and _flatten) that closes that gap.

Same offline-fake-Neo4j approach as tests/test_ai04_graph_sync.py,
tests/test_cascade.py, tests/verify.py — no live Neo4j is reachable
from this sandbox, so what this file CAN prove is: the Python
orchestration (_tx_prune_stale's aggregation, GC gating, _tx_load's
wiring) behaves correctly against a scripted fake tx, and the Cypher
TEXT itself satisfies the specific safety properties RECONCILE's own
module docstring promises. What it CANNOT prove is that the Cypher
executes as intended against a real Neo4j — same caveat every other
offline test file in this suite documents.

What this proves:
  * SCOPE AUDIT — none of the ten _RECONCILE_* query constants ever
    reference a rule-inferred relationship type (SHARES_EMPLOYER_WITH,
    SHARES_ADDRESS_WITH, SHARES_ALIAS_PATTERN_WITH,
    MEMBER_OF_FRAUD_NETWORK, HAS_PRIOR_GUILTY_CASE,
    ALLEGATION_LIKELY_AGAINST_SUBJECT) or a :Rejection / :FraudNetwork /
    :InferenceRule node. This is the regression guard for "never a
    full-case wipe, never touches reject/revert history" — a future
    edit that widened a MATCH pattern to catch one of these would fail
    here at the source-code level.
  * NAMING AUDIT — every _RECONCILE_* constant is named outside the
    _Q_ prefix tests/test_ai04_graph_sync.py's MERGE-only audit sweeps
    up by convention, so that audit stays meaningful for what it
    actually checks (write queries) instead of needing a RECONCILE
    special-case.
  * TWO-MISS AUDIT — every relationship-level _RECONCILE_* query
    contains exactly one DELETE r, and that DELETE sits inside the
    already_flagged-gated FOREACH branch. A relationship is never
    deleted on its first missing sync.
  * SELF-HEAL AUDIT — every one of the 8 original MERGE-based write
    queries clears _prune_pending / _prune_flagged_at in its own SET
    clause, so a flag raised by a transient AppWorks failure clears
    itself the next time that row is genuinely re-fetched.
  * SHARED-NODE AUDIT — the address/alias/employer/wage RECONCILE
    queries never DETACH DELETE or otherwise remove the :Address /
    :Alias / :Employer node itself, only the edge; only the two GC
    queries (owned, single-parent :Allegation / :Commentary) ever
    remove a node, and only after re-checking it has zero remaining
    relationships.
  * SCOPING AUDIT — the case-scoped queries key off $case_id, the
    subject-scoped queries key off $subject_ids (this run's actually-
    fetched subjects, not a blanket case sweep), and the undirected
    IS_CO_SUBJECT_WITH query de-duplicates so it never attempts to
    delete the same physical relationship twice.
  * ORCHESTRATION — _tx_prune_stale aggregates each query's
    deleted/flagged counts, only fires the GC queries when something
    was actually confirmed-deleted this pass, and degrades safely
    (flags/deletes nothing) when handed empty subject_ids/
    allegation_ids rather than treating an empty list as "match
    everything". _tx_load passes it exactly this run's fetched subject
    and allegation ids, not the previous run's.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from typing import Any, Dict, List

import pytest


def _install_external_import_stubs() -> None:
    """Same offline stub as tests/test_ai04_graph_sync.py: only stub the
    `neo4j` package if it truly is not installed in this sandbox, so a
    real install is exercised when available."""
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

# --------------------------------------------------------------------
# Shared fixtures
# --------------------------------------------------------------------

_RECONCILE_RELATIONSHIP_QUERY_NAMES = [
    "_RECONCILE_APPEARS_IN_CASE",
    "_RECONCILE_ALLEGATIONS",
    "_RECONCILE_ADDRESSES",
    "_RECONCILE_ALIASES",
    "_RECONCILE_EMPLOYERS",
    "_RECONCILE_WAGES",
    "_RECONCILE_COMMENTARY",
    "_RECONCILE_CO_SUBJECTS",
]

_RECONCILE_GC_QUERY_NAMES = ["_RECONCILE_GC_ALLEGATIONS", "_RECONCILE_GC_COMMENTARY"]

_ALL_RECONCILE_QUERY_NAMES = _RECONCILE_RELATIONSHIP_QUERY_NAMES + _RECONCILE_GC_QUERY_NAMES

_ENTITY_WRITE_QUERY_NAMES = [
    "_Q_SUBJECTS",
    "_Q_ALLEGATIONS",
    "_Q_ADDRESSES",
    "_Q_ALIASES",
    "_Q_EMPLOYERS",
    "_Q_WAGES",
    "_Q_COMMENTARY",
    "_Q_CO_SUBJECTS",
]

# The exact set of relationship types a rule ever WRITES
# (reasoning_layer/rules/**/*.cypher), plus the labels/records Principle
# 14 makes permanent. RECONCILE must never reference any of these.
_FORBIDDEN_SUBSTRINGS = (
    "SHARES_EMPLOYER_WITH",
    "SHARES_ADDRESS_WITH",
    "SHARES_ALIAS_PATTERN_WITH",
    "MEMBER_OF_FRAUD_NETWORK",
    "HAS_PRIOR_GUILTY_CASE",
    "ALLEGATION_LIKELY_AGAINST_SUBJECT",
    ":Rejection",
    ":FraudNetwork",
    ":InferenceRule",
)


# --------------------------------------------------------------------
# 1. SCOPE AUDIT — never a rule-inferred type, never :Rejection/:FraudNetwork
# --------------------------------------------------------------------


@pytest.mark.parametrize("name", _ALL_RECONCILE_QUERY_NAMES)
def test_reconcile_query_never_references_a_rule_inferred_or_rejection_type(name):
    cypher = getattr(graph_sync, name)
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in cypher, (
            f"{name} references {forbidden!r} — RECONCILE must never touch rule-inferred "
            f"relationships or investigator rejection records"
        )


# --------------------------------------------------------------------
# 2. NAMING AUDIT — excluded from the AI-04 MERGE-only audit by construction
# --------------------------------------------------------------------


def test_reconcile_query_constants_are_named_outside_the_q_prefix():
    """tests/test_ai04_graph_sync.py collects every module-level name
    starting with '_Q_' and asserts it contains MERGE. _RECONCILE_*
    queries are legitimate DELETE/SET-flag queries, not MERGE writes —
    naming them outside that prefix keeps that audit meaningful instead
    of forcing it to special-case a whole different query category."""
    for name in _ALL_RECONCILE_QUERY_NAMES:
        assert not name.startswith("_Q_"), f"{name} would be swept into the MERGE-only _Q_* audit"
        assert hasattr(graph_sync, name), f"expected etl.graph_sync.{name} to exist"


# --------------------------------------------------------------------
# 3. TWO-MISS AUDIT — never delete on a bare, unconfirmed miss
# --------------------------------------------------------------------

_DELETE_R_RE = re.compile(r"\bDELETE\s+r\b")
_GUARDED_DELETE_RE = re.compile(
    r"FOREACH\s*\(\s*_\s+IN\s+CASE\s+WHEN\s+already_flagged\s+THEN\s+\[1\]\s+ELSE\s+\[\]\s+END\s*\|\s*DELETE\s+r\s*\)"
)


@pytest.mark.parametrize("name", _RECONCILE_RELATIONSHIP_QUERY_NAMES)
def test_reconcile_relationship_query_only_deletes_inside_the_already_flagged_branch(name):
    """A relationship missing from this run's fetch must be FLAGGED,
    not deleted, unless it was already flagged from a prior run. Every
    DELETE r in these queries must be the single one inside the
    already_flagged-gated FOREACH; a second, unconditional DELETE r
    anywhere else in the same query would erase data on the very first
    miss."""
    cypher = getattr(graph_sync, name)
    occurrences = _DELETE_R_RE.findall(cypher)
    assert len(occurrences) == 1, (
        f"{name}: expected exactly one 'DELETE r', found {len(occurrences)} — an "
        f"unconditional or duplicated delete would erase data on the first miss "
        f"or attempt to delete the same relationship twice"
    )
    assert _GUARDED_DELETE_RE.search(cypher), (
        f"{name}: the DELETE r must sit inside the already_flagged-gated FOREACH "
        f"branch, not a bare/unconditional DELETE"
    )


@pytest.mark.parametrize("name", _RECONCILE_RELATIONSHIP_QUERY_NAMES)
def test_reconcile_relationship_query_flags_the_not_yet_flagged_branch(name):
    """The other side of the same FOREACH pair: a first-time miss must
    set _prune_pending, not silently do nothing."""
    cypher = getattr(graph_sync, name)
    assert "SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at" in cypher
    assert "NOT already_flagged" in cypher


# --------------------------------------------------------------------
# 4. SELF-HEAL AUDIT — every ordinary write clears the flag on refresh
# --------------------------------------------------------------------


@pytest.mark.parametrize("name", _ENTITY_WRITE_QUERY_NAMES)
def test_entity_write_query_self_heals_the_prune_flag(name):
    """If a relationship was flagged stale by a transient AppWorks
    failure and then genuinely re-fetched on a later run, the ordinary
    MERGE write for it must clear the flag — otherwise a false alarm
    from one bad sync could still lead to deletion on its NEXT
    unrelated miss, defeating the two-miss confirmation entirely."""
    cypher = getattr(graph_sync, name)
    assert "r._prune_pending = null" in cypher, f"{name} never clears _prune_pending on a fresh MERGE"
    assert "r._prune_flagged_at = null" in cypher, f"{name} never clears _prune_flagged_at on a fresh MERGE"


# --------------------------------------------------------------------
# 5. SHARED-NODE AUDIT — Address/Alias/Employer nodes are never deleted
# --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["_RECONCILE_ADDRESSES", "_RECONCILE_ALIASES", "_RECONCILE_EMPLOYERS", "_RECONCILE_WAGES"])
def test_reconcile_shared_reference_queries_never_delete_the_node(name):
    """:Address / :Alias / :Employer can legitimately be referenced by
    other subjects or other cases. RECONCILE may only ever unlink the
    one stale edge, never remove the shared node itself."""
    cypher = getattr(graph_sync, name)
    assert "DETACH DELETE" not in cypher
    for forbidden_node_delete in ("DELETE addr", "DELETE al)", "DELETE e"):
        assert forbidden_node_delete not in cypher


@pytest.mark.parametrize("name", _RECONCILE_GC_QUERY_NAMES)
def test_gc_query_checks_zero_remaining_relationships_before_deleting(name):
    """Owned, single-parent child records (:Allegation, :Commentary)
    are the only node types RECONCILE ever deletes, and only once
    confirmed to have no other live relationship — never an
    unconditional label-wide sweep."""
    cypher = getattr(graph_sync, name)
    assert "WHERE NOT (" in cypher and ")--()" in cypher, f"{name} must gate on zero remaining relationships"
    assert "DETACH DELETE" in cypher
    assert "UNWIND $ids AS" in cypher, f"{name} must scope to specific candidate ids, not a label-wide scan"


def test_gc_queries_only_ever_target_owned_child_labels():
    assert ":Allegation" in graph_sync._RECONCILE_GC_ALLEGATIONS
    assert ":Commentary" in graph_sync._RECONCILE_GC_COMMENTARY
    for forbidden_label in (":Address", ":Alias", ":Employer", ":Subject", ":Case"):
        assert forbidden_label not in graph_sync._RECONCILE_GC_ALLEGATIONS
        assert forbidden_label not in graph_sync._RECONCILE_GC_COMMENTARY


# --------------------------------------------------------------------
# 6. SCOPING AUDIT — case-scoped, subject-scoped, and undirected dedup
# --------------------------------------------------------------------


def test_case_scoped_reconcile_queries_key_off_case_id():
    assert "case_id: $case_id" in graph_sync._RECONCILE_APPEARS_IN_CASE
    assert "case_id: $case_id" in graph_sync._RECONCILE_ALLEGATIONS


@pytest.mark.parametrize("name", ["_RECONCILE_ADDRESSES", "_RECONCILE_ALIASES", "_RECONCILE_EMPLOYERS", "_RECONCILE_WAGES"])
def test_subject_scoped_reconcile_queries_key_off_this_runs_subject_ids(name):
    """Scoped to $subject_ids (this run's actually-fetched subjects),
    never a blanket case-wide sweep — a subject who fell off the case
    entirely this run was never re-fetched at all, so their edges are
    correctly left untouched rather than guessed at."""
    cypher = getattr(graph_sync, name)
    assert "s.subject_id IN $subject_ids" in cypher


def test_commentary_reconcile_query_scopes_to_case_subjects_and_allegations_only():
    cypher = graph_sync._RECONCILE_COMMENTARY
    assert "parent.case_id = $case_id" in cypher
    assert "parent.subject_id IN $subject_ids" in cypher
    assert "parent.allegation_id IN $allegation_ids" in cypher


def test_co_subject_reconcile_query_scopes_by_case_and_dedupes_the_undirected_match():
    """IS_CO_SUBJECT_WITH is stored undirected (_Q_CO_SUBJECTS: MERGE
    (a)-[r]-(b), no arrow). An undirected MATCH on the same pattern
    would otherwise visit the one stored relationship from both
    endpoints and attempt to DELETE it twice in the same query."""
    cypher = graph_sync._RECONCILE_CO_SUBJECTS
    assert "r.case_id = $case_id" in cypher
    assert "id(a) < id(b)" in cypher


# --------------------------------------------------------------------
# 7. ORCHESTRATION — _tx_prune_stale's aggregation and GC gating
# --------------------------------------------------------------------


class _FakeRecord:
    def __init__(self, **values):
        self._values = values

    def __getitem__(self, key):
        assert key in self._values, f"unexpected key {key!r} requested from a stubbed Neo4j record"
        return self._values[key]


class _FakeResult:
    def __init__(self, record: _FakeRecord):
        self._record = record

    def single(self):
        return self._record


class _ScriptedTx:
    """A tx.run() stub whose return value per query is looked up by a
    caller-supplied {query_constant: {...values...}} script, so a
    single reconcile pass can be driven through a specific scenario
    (e.g. "this relationship's second consecutive miss — already
    flagged, and should now be reported as deleted") without a live
    Neo4j. Any query not explicitly scripted falls back to the
    all-zero/all-empty default a run with nothing stale would produce."""

    _DEFAULTS = {"n": 1, "deleted": 0, "flagged": 0, "retired_candidate_ids": []}

    def __init__(self, script: Dict[str, Dict[str, Any]] | None = None):
        self.calls: List[Dict[str, Any]] = []
        self._script = script or {}

    def run(self, query, **params):
        self.calls.append({"query": query, "params": params})
        values = {**self._DEFAULTS, **self._script.get(query, {})}
        return _FakeResult(_FakeRecord(**values))


def test_tx_prune_stale_reports_zero_everywhere_and_skips_gc_when_nothing_is_stale():
    tx = _ScriptedTx()
    result = graph_sync._tx_prune_stale(tx, "CASE-1", ["SUBJ-1"], ["ALLEG-1"], "2026-08-14T00:00:00Z")

    for key in (
        "appears_in_case",
        "allegations",
        "addresses",
        "aliases",
        "employers",
        "wage_records",
        "commentary",
        "co_subject_pairs",
    ):
        assert result[key] == {"deleted": 0, "flagged": 0}
    assert result["allegations_removed"] == 0
    assert result["commentary_removed"] == 0

    gc_queries_called = [
        c["query"]
        for c in tx.calls
        if c["query"] in (graph_sync._RECONCILE_GC_ALLEGATIONS, graph_sync._RECONCILE_GC_COMMENTARY)
    ]
    assert gc_queries_called == [], "GC must not run when nothing was confirmed-deleted this pass"


def test_tx_prune_stale_garbage_collects_only_the_confirmed_deleted_candidates():
    script = {
        graph_sync._RECONCILE_ALLEGATIONS: {
            "deleted": 1,
            "flagged": 0,
            "retired_candidate_ids": ["ALLEG-9"],
        },
        graph_sync._RECONCILE_COMMENTARY: {
            "deleted": 2,
            "flagged": 1,
            "retired_candidate_ids": ["COMMENT-9", "COMMENT-10"],
        },
        graph_sync._RECONCILE_GC_ALLEGATIONS: {"n": 1},
        graph_sync._RECONCILE_GC_COMMENTARY: {"n": 2},
    }
    tx = _ScriptedTx(script)

    result = graph_sync._tx_prune_stale(tx, "CASE-1", ["SUBJ-1"], ["ALLEG-1", "ALLEG-9"], "2026-08-14T00:00:00Z")

    assert result["allegations"] == {"deleted": 1, "flagged": 0}
    assert result["commentary"] == {"deleted": 2, "flagged": 1}
    assert result["allegations_removed"] == 1
    assert result["commentary_removed"] == 2

    gc_alleg_calls = [c for c in tx.calls if c["query"] == graph_sync._RECONCILE_GC_ALLEGATIONS]
    assert len(gc_alleg_calls) == 1
    assert gc_alleg_calls[0]["params"]["ids"] == ["ALLEG-9"]

    gc_comm_calls = [c for c in tx.calls if c["query"] == graph_sync._RECONCILE_GC_COMMENTARY]
    assert len(gc_comm_calls) == 1
    assert gc_comm_calls[0]["params"]["ids"] == ["COMMENT-9", "COMMENT-10"]


def test_tx_prune_stale_is_degenerate_safe_on_empty_subject_and_allegation_ids():
    """A transient failure at the very top of fetch_case_graph (not one
    sub-fetch) can leave subject_ids/allegation_ids empty for a run.
    The orchestration must pass that empty list straight through to
    every query rather than substituting or omitting it — the
    subject/allegation-scoped IN-list predicates proven in section 6
    above then correctly match nothing on their own."""
    tx = _ScriptedTx()
    graph_sync._tx_prune_stale(tx, "CASE-1", [], [], "2026-08-14T00:00:00Z")

    assert tx.calls, "expected every RECONCILE query to still be issued, just scoped to nothing"
    for call in tx.calls:
        if "subject_ids" in call["params"]:
            assert call["params"]["subject_ids"] == []
        if "allegation_ids" in call["params"]:
            assert call["params"]["allegation_ids"] == []


def test_tx_prune_stale_passes_case_id_and_retrieved_at_to_every_call():
    tx = _ScriptedTx()
    graph_sync._tx_prune_stale(tx, "CASE-42", ["SUBJ-1", "SUBJ-2"], [], "2026-08-14T12:00:00Z")

    for call in tx.calls:
        assert call["params"]["case_id"] == "CASE-42"
        assert call["params"]["retrieved_at"] == "2026-08-14T12:00:00Z"
        assert call["params"]["subject_ids"] == ["SUBJ-1", "SUBJ-2"]


# --------------------------------------------------------------------
# 8. _tx_load WIRING — this run's fetched ids, not a stale/previous set
# --------------------------------------------------------------------


def _minimal_payload() -> Dict[str, Any]:
    """Deliberately smaller than tests/test_ai04_graph_sync.py's
    _sample_case_payload — this file only needs enough shape for
    _tx_load to run to completion and hand subject/allegation ids to
    _tx_prune_stale, not full field coverage (that's the other file's
    job)."""
    return {
        "case": {
            "case_id": "CASE-2002",
            "complaint_number": "CMP-2",
            "status": "Open",
            "is_fasttrack": False,
            "fraud_amount": 0.0,
            "fraud_start_date": None,
            "fraud_end_date": None,
            "is_dta_case": False,
            "disposition": None,
            "opened_date": None,
            "closed_date": None,
            "merge_target_case_ids": [],
            "source_table": "Workfolder",
            "retrieved_at": "2026-08-14T00:00:00Z",
        },
        "subjects": [
            {
                "subject_id": "SUBJ-A",
                "first_name": "A",
                "last_name": "One",
                "company_name": None,
                "fein": None,
                "subject_type": "Individual",
                "subject_role": "Primary Subject",
                "is_primary": True,
                "addresses": [],
                "aliases": [],
                "employers": [],
                "wages": [],
                "source_table": "Subject",
                "retrieved_at": "2026-08-14T00:00:00Z",
            },
            {
                "subject_id": "SUBJ-B",
                "first_name": "B",
                "last_name": "Two",
                "company_name": None,
                "fein": None,
                "subject_type": "Individual",
                "subject_role": "Co-Subject",
                "is_primary": False,
                "addresses": [],
                "aliases": [],
                "employers": [],
                "wages": [],
                "source_table": "Subject",
                "retrieved_at": "2026-08-14T00:00:00Z",
            },
        ],
        "allegations": [
            {
                "allegation_id": "ALLEG-77",
                "allegation_type": "Concealed Employment",
                "status": "Open",
                "record_status": "Active",
                "norris_code": None,
                "outcome": None,
                "date_closed": None,
                "comment_text": None,
                "source_table": "Allegations",
                "retrieved_at": "2026-08-14T00:00:00Z",
            }
        ],
        "commentary": [],
        "retrieved_at": "2026-08-14T00:00:00Z",
    }


class _RecordingTx:
    """Bare-minimum tx stub for driving _tx_load's ordinary write
    queries through to the RECONCILE call — every _Q_* write here just
    needs SOME record back, never a real Neo4j."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run(self, query, **params):
        self.calls.append({"query": query, "params": params})
        return _FakeResult(_FakeRecord(n=1))


def test_tx_load_calls_prune_stale_with_this_runs_subject_and_allegation_ids(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_prune_stale(tx, case_id, subject_ids, allegation_ids, retrieved_at):
        captured["case_id"] = case_id
        captured["subject_ids"] = list(subject_ids)
        captured["allegation_ids"] = list(allegation_ids)
        captured["retrieved_at"] = retrieved_at
        return {"stub": True}

    monkeypatch.setattr(graph_sync, "_tx_prune_stale", fake_prune_stale)

    payload = _minimal_payload()
    counts = graph_sync._tx_load(_RecordingTx(), payload)

    assert captured["case_id"] == "CASE-2002"
    assert captured["subject_ids"] == ["SUBJ-A", "SUBJ-B"]
    assert captured["allegation_ids"] == ["ALLEG-77"]
    assert captured["retrieved_at"] == "2026-08-14T00:00:00Z"
    assert counts["pruned"] == {"stub": True}


def test_tx_load_prunes_with_an_empty_case_correctly_too(monkeypatch):
    """A case with no subjects/allegations at all (e.g. a brand-new
    Workfolder with nothing attached yet) must still call
    _tx_prune_stale with empty lists, not skip the call — an earlier
    sync's now-orphaned edges for this case_id still need a chance to
    be reconciled even if this run's fetch came back empty."""
    captured: Dict[str, Any] = {}

    def fake_prune_stale(tx, case_id, subject_ids, allegation_ids, retrieved_at):
        captured["called"] = True
        captured["subject_ids"] = list(subject_ids)
        captured["allegation_ids"] = list(allegation_ids)
        return {}

    monkeypatch.setattr(graph_sync, "_tx_prune_stale", fake_prune_stale)

    payload = _minimal_payload()
    payload["subjects"] = []
    payload["allegations"] = []

    graph_sync._tx_load(_RecordingTx(), payload)

    assert captured.get("called") is True
    assert captured["subject_ids"] == []
    assert captured["allegation_ids"] == []
