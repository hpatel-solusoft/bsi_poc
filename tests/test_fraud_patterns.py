"""
Tests for Agent 8 — the Fraud Patterns tab (taxonomy, ETL linking, the
reasoning module, the pipeline functions and the service).

Everything runs without Neo4j, Postgres or an LLM, in the same mocked style
as the rest of this suite. What this PROVES: the taxonomy file is valid, the
loader and ETL hand Neo4j the right parameters in the right order, the
sorting / grouping / counting / classification logic is correct, a case with
no category degrades to a note, and the service never persists the graph
result. What it CANNOT prove: that the Cypher text is valid or returns the
right rows. Run the WI-3 and WI-4 checks in Neo4j Browser for that.

Run:  python -m pytest tests/test_fraud_patterns.py
"""



from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

import pytest
import yaml
from neo4j.exceptions import Neo4jError

os.environ.setdefault("OPENAI_API_KEY", "test-key-for-fraud-patterns-tests")

from api import pipeline_execution  # noqa: E402
from api import server  # noqa: E402
from api.services import fraud_patterns_service  # noqa: E402
from core.narrative_staleness import StalenessCheck  # noqa: E402
from etl import graph_sync  # noqa: E402
from reasoning_layer import taxonomy_registry  # noqa: E402
from reasoning_layer.network import fraud_patterns  # noqa: E402
from reasoning_layer.network import fraud_patterns_queries as queries  # noqa: E402

GUILTY = ["guilty", "convicted", "substantiated", "guilty plea", "found guilty"]
EXPECTED_CATEGORY_SIZES = [9, 5, 1, 2, 4, 2]


# --------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------


class FakeResult:
    """Stands in for a Neo4j result: .data() for row lists, .single() for one record."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def data(self) -> List[Dict[str, Any]]:
        return self._rows

    def single(self) -> Dict[str, Any]:
        return {"n": len(self._rows) or 1}


class FakeSession:
    """Records every (query, params) and answers from a {query_text: rows} map."""

    def __init__(self, rows_by_query: Dict[str, List[Dict[str, Any]]] | None = None):
        self.calls: List[Dict[str, Any]] = []
        self._rows_by_query = rows_by_query or {}

    def run(self, query: str, **params: Any) -> FakeResult:
        self.calls.append({"query": query, "params": params})
        return FakeResult(self._rows_by_query.get(query, []))


def session_factory(session: FakeSession):
    """A get_session replacement that yields `session` as a context manager."""

    @contextmanager
    def _get_session(*args: Any, **kwargs: Any):
        yield session

    return _get_session


# --------------------------------------------------------------------
# 1. Taxonomy file
# --------------------------------------------------------------------


def test_taxonomy_file_has_23_types_in_the_expected_categories():
    specs = taxonomy_registry.load_taxonomy()
    assert len(specs) == 23
    sizes = [sum(1 for s in specs if s.category == name) for name in dict.fromkeys(s.category for s in specs)]
    assert sizes == EXPECTED_CATEGORY_SIZES


def test_taxonomy_file_has_no_duplicate_codes_and_leaves_oth_and_eec_out():
    codes = [s.code for s in taxonomy_registry.load_taxonomy()]
    assert len(codes) == len(set(codes))
    assert "OTH" not in codes and "EEC" not in codes


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Employment ", "employment"),
        ("Dependent   Not  in Home", "dependent not in home"),
        ("DTA\t- Employment", "dta - employment"),
        (None, ""),
    ],
)
def test_normalize_match_key(raw, expected):
    assert taxonomy_registry.normalize_match_key(raw) == expected


def test_match_keys_hold_normalized_code_description_and_aliases():
    raw = {
        "categories": [
            {
                "name": "Income",
                "types": [{"code": "EMP", "description": "Employment", "aliases": [" DTA -  Employment "]}],
            }
        ]
    }
    (spec,) = taxonomy_registry.parse_taxonomy(raw)
    assert spec.match_keys == ("emp", "employment", "dta - employment")


@pytest.mark.parametrize("raw", [None, {}, {"categories": []}, "text"])
def test_empty_taxonomy_fails_with_a_clear_error(raw):
    with pytest.raises(ValueError, match="is empty"):
        taxonomy_registry.parse_taxonomy(raw)


def test_duplicate_code_fails_with_a_clear_error():
    raw = {
        "categories": [
            {"name": "A", "types": [{"code": "X", "description": "One"}]},
            {"name": "B", "types": [{"code": "X", "description": "Two"}]},
        ]
    }
    with pytest.raises(ValueError, match="duplicate allegation type code 'X'"):
        taxonomy_registry.parse_taxonomy(raw)


def test_two_types_sharing_a_match_key_fail():
    raw = {
        "categories": [
            {
                "name": "A",
                "types": [
                    {"code": "X", "description": "Same"},
                    {"code": "Y", "description": "Other", "aliases": ["same"]},
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="matches both X and Y"):
        taxonomy_registry.parse_taxonomy(raw)


def test_category_without_types_fails():
    with pytest.raises(ValueError, match="has no types"):
        taxonomy_registry.parse_taxonomy({"categories": [{"name": "A", "types": []}]})


# --------------------------------------------------------------------
# 2. Taxonomy loader
# --------------------------------------------------------------------


def _ensure_with(raw_yaml: str) -> FakeSession:
    """Run ensure_taxonomy against a fake session using the given YAML."""
    session = FakeSession()
    specs = taxonomy_registry.parse_taxonomy(yaml.safe_load(raw_yaml))
    with (
        mock.patch.object(taxonomy_registry, "load_taxonomy", return_value=specs),
        mock.patch.object(taxonomy_registry, "get_session", session_factory(session)),
    ):
        taxonomy_registry.ensure_taxonomy()
    return session


def _taxonomy_yaml(move_chk_to: str = "Duplicate Benefit Fraud", drop_mul: bool = False) -> str:
    types = [] if drop_mul else [{"code": "MUL", "description": "Multiple Assistance"}]
    raw = {
        "categories": [
            {"name": "Duplicate Benefit Fraud", "types": [{"code": "CHK", "description": "Check"}, *types]},
            {"name": "Identity Fraud", "types": [{"code": "FID", "description": "False Identity"}]},
        ]
    }
    if move_chk_to != "Duplicate Benefit Fraud":
        raw["categories"][0]["types"].pop(0)
        raw["categories"][1]["types"].append({"code": "CHK", "description": "Check"})
    return yaml.safe_dump(raw)


def test_loader_runs_sync_then_remove_types_then_remove_empty_categories():
    session = _ensure_with(_taxonomy_yaml())
    queries_run = [call["query"] for call in session.calls]
    assert queries_run == [
        taxonomy_registry._SYNC_QUERY,
        taxonomy_registry._REMOVE_TYPES_QUERY,
        taxonomy_registry._REMOVE_EMPTY_CATEGORIES_QUERY,
    ]


def test_loader_sends_the_changed_category_for_a_moved_type():
    session = _ensure_with(_taxonomy_yaml(move_chk_to="Identity Fraud"))
    rows = {row["code"]: row for row in session.calls[0]["params"]["types"]}
    assert rows["CHK"]["category"] == "Identity Fraud"
    assert rows["CHK"]["match_keys"] == ["chk", "check"]


def test_sync_query_retires_the_old_belongs_to_link_when_a_type_moves():
    sync = taxonomy_registry._SYNC_QUERY
    assert "OPTIONAL MATCH (t)-[stale:BELONGS_TO]->(other:Category)" in sync
    assert "WHERE other <> cat" in sync and "DELETE stale" in sync


def test_loader_passes_only_codes_still_in_the_file_to_the_remove_query():
    session = _ensure_with(_taxonomy_yaml(drop_mul=True))
    assert sorted(session.calls[1]["params"]["codes"]) == ["CHK", "FID"]


def test_remove_queries_delete_types_not_in_file_and_only_empty_categories():
    assert "NOT t.code IN $codes" in taxonomy_registry._REMOVE_TYPES_QUERY
    assert "NOT EXISTS" in taxonomy_registry._REMOVE_EMPTY_CATEGORIES_QUERY


# --------------------------------------------------------------------
# 3. ETL linking
# --------------------------------------------------------------------


class RecordingTx:
    """Records every query a transaction is asked to run; every record answers n=1."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run(self, query: str, **params: Any):
        self.calls.append({"query": query, "params": params})
        return SimpleNamespace(
            single=lambda: {"n": 1, "deleted": 0, "flagged": 0, "retired_candidate_ids": []}
        )


def _payload(allegation_types: List[Any]) -> Dict[str, Any]:
    return {
        "case": {"case_id": "CASE-1", "merge_target_case_ids": []},
        "retrieved_at": "2026-10-26T00:00:00Z",
        "subjects": [],
        "commentary": [],
        "allegations": [
            {"allegation_id": f"A{i}", "allegation_type": text} for i, text in enumerate(allegation_types)
        ],
    }


def _load(allegation_types: List[Any]) -> RecordingTx:
    tx = RecordingTx()
    graph_sync._tx_load(tx, _payload(allegation_types))
    return tx


def test_type_keys_are_normalized_deduplicated_and_sorted():
    allegations = [
        {"allegation_type": " Employment "},
        {"allegation_type": "EMPLOYMENT"},
        {"allegation_type": "Check"},
    ]
    allegations += [{"allegation_type": None}, {"allegation_type": "  "}]
    assert graph_sync._allegation_type_keys(allegations) == ["check", "employment"]


def test_etl_removes_old_links_before_creating_new_ones_right_after_allegations():
    queries_run = [call["query"] for call in _load(["Employment"]).calls]
    unlink = queries_run.index(graph_sync.UNLINK_ALLEGATION_TYPES)
    link = queries_run.index(graph_sync.Q_ALLEGATION_TYPE_LINKS)
    assert queries_run.index(graph_sync.Q_ALLEGATIONS) < unlink < link


def test_etl_passes_the_normalized_allegation_text_to_the_link_query():
    tx = _load(["  Dependent  Not in Home ", "Check"])
    link_call = next(c for c in tx.calls if c["query"] == graph_sync.Q_ALLEGATION_TYPE_LINKS)
    assert link_call["params"]["type_keys"] == ["check", "dependent not in home"]


def test_etl_runs_both_link_queries_for_a_case_with_no_allegations():
    queries_run = [call["query"] for call in _load([]).calls]
    assert graph_sync.UNLINK_ALLEGATION_TYPES in queries_run
    assert graph_sync.Q_ALLEGATION_TYPE_LINKS in queries_run
    assert graph_sync.Q_ALLEGATIONS not in queries_run


def test_link_query_is_merge_based_and_the_unlink_query_is_not_a_q_constant():
    assert "MERGE (c)-[r:HAS_ALLEGATION_TYPE]->(t)" in graph_sync._Q_ALLEGATION_TYPE_LINKS
    assert "DELETE r" in graph_sync.UNLINK_ALLEGATION_TYPES
    assert not hasattr(graph_sync, "_Q_UNLINK_ALLEGATION_TYPES")


# --------------------------------------------------------------------
# 4. Related cases ordering
# --------------------------------------------------------------------


def _case(case_id: str, opened: Any = "2024-01-01", same_type: bool = False) -> Dict[str, Any]:
    return {"case_id": case_id, "date_opened": opened, "also_same_type": same_type}


def test_different_type_cases_come_before_same_type_cases():
    ordered = fraud_patterns.order_related_cases([_case("A", same_type=True), _case("B", same_type=False)])
    assert [c["case_id"] for c in ordered] == ["B", "A"]


def test_within_a_group_newest_first_and_undated_last():
    cases = [_case("A", "2021-05-01"), _case("B", None), _case("C", "2023-02-01"), _case("D", "")]
    assert [c["case_id"] for c in fraud_patterns.order_related_cases(cases)] == ["C", "A", "B", "D"]


def test_ties_break_on_case_id():
    cases = [_case("C9"), _case("C1"), _case("C5")]
    assert [c["case_id"] for c in fraud_patterns.order_related_cases(cases)] == ["C1", "C5", "C9"]


def test_undated_different_type_case_still_beats_dated_same_type_case():
    cases = [_case("A", "2024-01-01", same_type=True), _case("B", None, same_type=False)]
    assert [c["case_id"] for c in fraud_patterns.order_related_cases(cases)] == ["B", "A"]


def _related_row(case_id: str, same_type: bool) -> Dict[str, Any]:
    return {
        "case_id": case_id,
        "complaint_no": f"CMP-{case_id}",
        "status": "Closed",
        "fraud_amount": 100.0,
        "date_opened": "2024-03-01",
        "case_types": ["Check", "Assets"],
        "shared_categories": ["Income / Asset Concealment"],
        "also_same_type": same_type,
        "also_linked_through_people": not same_type,
    }


def test_precedents_cap_the_list_but_report_true_totals():
    rows = [_related_row(f"C{i}", same_type=i % 2 == 0) for i in range(7)]
    precedents = fraud_patterns.build_precedents(rows, limit=3)
    assert len(precedents["matches"]) == 3
    assert precedents["total_matches"] == 7
    assert precedents["same_type_count"] == 4
    assert precedents["category_only_count"] == 3


def test_precedent_match_has_exactly_the_contract_fields_with_sorted_lists():
    (match,) = fraud_patterns.build_precedents([_related_row("C1", False)], limit=5)["matches"]
    assert set(match) == {
        "case_id",
        "complaint_no",
        "status",
        "fraud_amount",
        "date_opened",
        "case_types",
        "shared_categories",
        "also_same_type",
        "also_linked_through_people",
    }
    assert match["case_types"] == ["Assets", "Check"]


# --------------------------------------------------------------------
# 5. Financial exposure
# --------------------------------------------------------------------


def test_financial_exposure_totals_and_counts_missing_amounts():
    rows = [
        {"category": "B", "case_id": "1", "fraud_amount": 100.5},
        {"category": "B", "case_id": "2", "fraud_amount": None},
        {"category": "B", "case_id": "3", "fraud_amount": "not a number"},
        {"category": "A", "case_id": "4", "fraud_amount": 200},
    ]
    assert fraud_patterns.build_financial_exposure(rows) == [
        {"category": "A", "case_count": 1, "total_exposure": 200.0, "cases_missing_amount": 0},
        {"category": "B", "case_count": 3, "total_exposure": 100.5, "cases_missing_amount": 2},
    ]


# --------------------------------------------------------------------
# 6. Outcome classification
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcomes, statuses, disposition, expected",
    [
        (["Guilty"], [], None, "substantiated"),
        (["Found Guilty - Plea"], [], None, "substantiated"),
        ([], ["Substantiated"], None, "substantiated"),
        ([], [], "Convicted", "substantiated"),
        (["Dismissed"], ["Close"], None, "other"),
        ([], [], "Unfounded", "other"),
        ([], ["Close"], None, "not_recorded"),
        ([], [], None, "not_recorded"),
        (["  "], [None], "", "not_recorded"),
        (["Dismissed"], ["Substantiated"], None, "substantiated"),
    ],
)
def test_classify_outcome(outcomes, statuses, disposition, expected):
    assert fraud_patterns.classify_outcome(outcomes, statuses, disposition, GUILTY) == expected


def test_build_outcomes_counts_per_category_and_totals_add_up():
    rows = [
        {"category": "A", "case_id": "1", "outcomes": ["Guilty"], "statuses": [], "disposition": None},
        {"category": "A", "case_id": "2", "outcomes": ["Dismissed"], "statuses": [], "disposition": None},
        {"category": "A", "case_id": "3", "outcomes": [], "statuses": [], "disposition": None},
        {"category": "B", "case_id": "4", "outcomes": [], "statuses": [], "disposition": "Convicted"},
    ]
    assert fraud_patterns.build_outcomes(rows, GUILTY) == [
        {
            "category": "A",
            "closed_count": 3,
            "substantiated_count": 1,
            "other_count": 1,
            "not_recorded_count": 1,
        },
        {
            "category": "B",
            "closed_count": 1,
            "substantiated_count": 1,
            "other_count": 0,
            "not_recorded_count": 0,
        },
    ]


def test_guilty_values_come_from_the_rule_7_registry_entry():
    registry = {"Rule_07_Prior_Guilty": {"params": {"guilty_outcome_values": [" Guilty ", "Convicted"]}}}
    with mock.patch.object(fraud_patterns.rule_registry, "load_registry", return_value=registry):
        assert fraud_patterns._guilty_values() == ["guilty", "convicted"]


def test_missing_guilty_vocabulary_is_an_error_not_a_silent_zero():
    with mock.patch.object(fraud_patterns.rule_registry, "load_registry", return_value={}):
        with pytest.raises(ValueError, match="guilty_outcome_values"):
            fraud_patterns._guilty_values()


# --------------------------------------------------------------------
# 7. Trend
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2024-05-17", 2024),
        ("2019", 2019),
        (SimpleNamespace(year=2022), 2022),
        (None, None),
        ("", None),
        ("n/a", None),
    ],
)
def test_year_of(value, expected):
    assert fraud_patterns.year_of(value) == expected


def test_trend_counts_per_year_oldest_first_and_unknown_dates_separately():
    rows = [
        {"category": "A", "opened_date": "2023-01-01"},
        {"category": "A", "opened_date": "2021-06-01"},
        {"category": "A", "opened_date": "2023-09-09"},
        {"category": "A", "opened_date": None},
        {"category": "A", "opened_date": "garbage"},
    ]
    assert fraud_patterns.build_trend(rows) == [
        {
            "category": "A",
            "by_year": [{"year": 2021, "case_count": 1}, {"year": 2023, "case_count": 2}],
            "unknown_date_count": 2,
        }
    ]


# --------------------------------------------------------------------
# 8. find_fraud_patterns
# --------------------------------------------------------------------


def _own_type_rows() -> List[Dict[str, Any]]:
    return [
        {"code": "EMP", "description": "Employment", "category": "Income / Asset Concealment"},
        {"code": "ATS", "description": "Assets", "category": "Income / Asset Concealment"},
    ]


def _full_session() -> FakeSession:
    category_cases = [
        {
            "category": "Income / Asset Concealment",
            "case_id": "1",
            "fraud_amount": 500.0,
            "opened_date": "2022-02-02",
        },
        {"category": "Income / Asset Concealment", "case_id": "2", "fraud_amount": None, "opened_date": None},
    ]
    closed = [
        {
            "category": "Income / Asset Concealment",
            "case_id": "2",
            "disposition": None,
            "outcomes": ["Guilty"],
            "statuses": [],
        }
    ]
    return FakeSession(
        {
            queries.OWN_TYPES: _own_type_rows(),
            queries.RELATED_CASES: [_related_row("R1", True), _related_row("R2", False)],
            queries.CATEGORY_CASES: category_cases,
            queries.CLOSED_CASE_OUTCOMES: closed,
        }
    )


def _find(session: FakeSession, **kwargs: Any) -> Dict[str, Any]:
    registry = {
        "Rule_07_Prior_Guilty": {
            "params": {"guilty_outcome_values": GUILTY, "closed_case_statuses": ["closed", "close"]}
        }
    }
    with (
        mock.patch.object(fraud_patterns, "get_session", session_factory(session)),
        mock.patch.object(fraud_patterns.rule_registry, "load_registry", return_value=registry),
    ):
        return fraud_patterns.find_fraud_patterns("CASE-1", **kwargs)


def test_find_fraud_patterns_returns_all_sections_in_the_envelope():
    envelope = _find(_full_session())
    result = envelope["result"]
    assert envelope["provenance"]["computed_by"] == "reasoning_layer.network.fraud_patterns"
    assert result["source"] == "structural_graph"
    assert result["case_allegation_types"] == ["Assets", "Employment"]
    assert result["case_categories"] == ["Income / Asset Concealment"]
    assert "note" not in result and "unavailable_reason" not in result
    assert [m["case_id"] for m in result["precedents"]["matches"]] == ["R2", "R1"]
    assert result["financial_exposure"][0]["cases_missing_amount"] == 1
    assert result["outcomes"][0]["substantiated_count"] == 1
    assert result["trend"][0]["unknown_date_count"] == 1


def test_find_fraud_patterns_applies_the_cap_but_not_to_the_totals():
    result = _find(_full_session(), limit=1)["result"]
    assert len(result["precedents"]["matches"]) == 1
    assert result["precedents"]["total_matches"] == 2


def test_case_with_no_category_returns_the_note_and_empty_sections_without_error():
    session = FakeSession({queries.OWN_TYPES: []})
    result = _find(session)["result"]
    assert result["note"] == "This case is not assigned to a category."
    assert result["case_categories"] == []
    assert result["precedents"] == {
        "matches": [],
        "total_matches": 0,
        "same_type_count": 0,
        "category_only_count": 0,
    }
    assert result["financial_exposure"] == [] and result["outcomes"] == [] and result["trend"] == []
    assert len(session.calls) == 1, "no section query may run for a case without a category"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_case_id_raises_value_error(blank):
    with pytest.raises(ValueError):
        fraud_patterns.find_fraud_patterns(blank)


def test_every_query_is_read_only_and_takes_only_the_case_id():
    session = _full_session()
    _find(session)
    allowed = {"case_id", "closed_statuses"}
    assert all({"case_id"} <= set(call["params"]) <= allowed for call in session.calls)
    for text in (
        queries.OWN_TYPES,
        queries.RELATED_CASES,
        queries.CATEGORY_CASES,
        queries.CLOSED_CASE_OUTCOMES,
    ):
        assert not any(word in text for word in ("MERGE", "CREATE", "DELETE", "SET "))


def test_related_cases_query_uses_the_similar_cases_exclusion_and_ignores_rejected_links():
    text = queries.RELATED_CASES
    assert "ap1.is_primary = true AND ap2.is_primary = true" in text
    assert "toString(c2.case_id) <> toString($case_id)" in text
    assert 'coalesce(link.status, "active") <> "rejected"' in text
    for relationship in (
        "SHARES_EMPLOYER_WITH",
        "SHARES_ADDRESS_WITH",
        "SHARES_ALIAS_PATTERN_WITH",
        "IS_CO_SUBJECT_WITH",
    ):
        assert relationship in text


# --------------------------------------------------------------------
# 9. Pipeline functions
# --------------------------------------------------------------------


def test_fetch_live_fraud_patterns_degrades_to_an_unavailable_result():
    with mock.patch.object(pipeline_execution, "find_fraud_patterns", side_effect=Neo4jError("down")):
        result = pipeline_execution.fetch_live_fraud_patterns("CASE-1")
    assert result["unavailable_reason"] == "down"
    assert result["source"] == "structural_graph"
    assert result["precedents"]["matches"] == []


def test_fetch_live_fraud_patterns_treats_a_blank_case_id_as_unavailable_not_a_crash():
    assert "unavailable_reason" in pipeline_execution.fetch_live_fraud_patterns("")


class FakeRunner:
    """Records the prompt and scope the pipeline hands the LLM."""

    def __init__(self):
        self.kwargs: Dict[str, Any] = {}

    def run_scoped(self, **kwargs: Any):
        self.kwargs = kwargs
        return [{"role": "assistant", "content": "## Summary\nText."}], [], None


def test_pipeline_injects_the_graph_result_and_uses_the_fraud_patterns_scope():
    graph = {"source": "structural_graph", "precedents": {"matches": []}}
    runner = FakeRunner()
    prompts: List[Dict[str, Any]] = []
    with mock.patch.object(pipeline_execution, "fetch_live_fraud_patterns", return_value=graph):
        summary, data, provenance = pipeline_execution.run_fraud_patterns_pipeline(
            "CASE-1", {"provenance_trail": []}, runner, lambda ctx: prompts.append(ctx) or "PROMPT"
        )
    assert runner.kwargs["scope"] == "FRAUD_PATTERNS"
    assert prompts[0]["fraud_patterns"] == graph
    assert data == graph
    assert provenance[-1]["computed_by"] == "reasoning_layer.network.fraud_patterns"
    assert summary


def test_pipeline_gives_the_llm_only_the_graph_result_not_other_case_data():
    graph = {"source": "structural_graph", "precedents": {"matches": []}}
    prompts: List[Dict[str, Any]] = []
    case_data = {
        "complaint_number": "101685",
        "similar_cases": [{"complaint_no": "101691"}],
        "provenance_trail": [],
    }
    with mock.patch.object(pipeline_execution, "fetch_live_fraud_patterns", return_value=graph):
        pipeline_execution.run_fraud_patterns_pipeline(
            "CASE-1", case_data, FakeRunner(), lambda ctx: prompts.append(ctx) or "PROMPT"
        )
    assert set(prompts[0]) == {"complaint_number", "fraud_patterns"}
    assert "101691" not in repr(prompts[0])


# --------------------------------------------------------------------
# 10. Prompt
# --------------------------------------------------------------------


def test_prompt_has_the_five_headings_in_order_and_the_required_rules():
    from agent_service.prompt_builders import build_fraud_patterns_prompt

    prompt = build_fraud_patterns_prompt({"fraud_patterns": {"source": "structural_graph"}})
    headings = [
        "## Summary",
        "## Related Cases",
        "## Financial Exposure by Category",
        "## Outcomes in This Category",
        "## Trend",
    ]
    positions = [prompt.index(h) for h in headings]
    assert positions == sorted(positions)
    assert '"source": "structural_graph"' in prompt and "{case_context}" not in prompt
    flat = " ".join(prompt.split())  # line breaks in the prompt text are not meaningful
    for rule in ("Do NOT add, remove or reorder cases", "not a prediction", "None found.", "Complaint #"):
        assert rule in flat


# --------------------------------------------------------------------
# 11. Service
# --------------------------------------------------------------------

_NOT_STALE = StalenessCheck(core_data_changed=False, graph_changed=False)
_GRAPH = {"source": "structural_graph", "precedents": {"matches": [{"case_id": "R1"}]}}


def _case_data(cached: bool) -> Dict[str, Any]:
    data: Dict[str, Any] = {"complaint_intelligence": {"subject_primary_id": "S1"}, "provenance_trail": []}
    if cached:
        data[server.AGENT_SUMMARY_CACHE_KEY] = {"fraud_patterns": {"summary": "cached", "generated_at": "t"}}
    return data


def _run_service(case_data: Dict[str, Any], **patches: Any):
    return _run_service_with_staleness(case_data, _NOT_STALE, **patches)


def _run_service_with_staleness(case_data: Dict[str, Any], staleness: StalenessCheck, **patches: Any):
    case_id = "CASE-FP"
    server.CASE_STORE[case_id] = case_data
    with (
        mock.patch.object(server, "_resolve_case_store", return_value=(case_data, "mock")),
        mock.patch.object(fraud_patterns_service, "evaluate_cache_staleness", return_value=staleness),
        mock.patch.object(fraud_patterns_service, "log_agent_call"),
        mock.patch.object(fraud_patterns_service, "persist_case_session") as persist,
        mock.patch.object(fraud_patterns_service, "fetch_live_fraud_patterns", return_value=_GRAPH) as live,
        mock.patch.object(
            fraud_patterns_service,
            "run_fraud_patterns_pipeline",
            return_value=("## Summary\nFresh.", _GRAPH, [{"computed_by": "x"}]),
        ) as pipeline,
        mock.patch.object(fraud_patterns_service, "get_runner"),
    ):
        response = fraud_patterns_service.run_fraud_patterns(
            server.FraudPatternsRequest(case_id=case_id, **patches), "tester", "token"
        )
    return response, persist, live, pipeline


def test_cache_hit_serves_the_cached_summary_but_reads_the_graph_live():
    response, persist, live, pipeline = _run_service(_case_data(cached=True))
    details = response["details"]
    assert details["agent_summary"] == "cached"
    assert details["meta"]["agent_summary_source"] == "db_cache"
    assert details["graph_findings"]["fraud_patterns"] == _GRAPH
    live.assert_called_once_with("CASE-FP")
    pipeline.assert_not_called()
    persist.assert_not_called()


def test_fresh_run_returns_the_graph_result_under_graph_findings_fraud_patterns():
    response, _, _, pipeline = _run_service(_case_data(cached=False))
    assert response["status"] == "completed"
    assert response["details"]["agent_summary"] == "## Summary\nFresh."
    assert response["details"]["graph_findings"]["fraud_patterns"] == _GRAPH
    assert response["details"]["meta"]["agent_summary_source"] == "llm"
    pipeline.assert_called_once()


def test_fresh_run_persists_only_the_summary_never_the_graph_result():
    _, persist, _, _ = _run_service(_case_data(cached=False))
    persist.assert_called_once()
    persisted = persist.call_args.args[1]
    assert (
        persisted["investigation"][server.AGENT_SUMMARY_CACHE_KEY]["fraud_patterns"]["summary"]
        == "## Summary\nFresh."
    )
    assert "fraud_patterns" not in persisted
    assert "fraud_patterns" not in persisted["investigation"]
    assert "structural_graph" not in repr(persisted)
    assert "structural_graph" not in repr(server.CASE_STORE["CASE-FP"])


def test_reload_ai_summary_bypasses_the_cache_and_runs_the_pipeline():
    reload_requested = StalenessCheck(core_data_changed=True, graph_changed=False)
    with mock.patch.object(fraud_patterns_service, "evaluate_cache_staleness", return_value=reload_requested):
        response, persist, _, pipeline = _run_service_with_staleness(
            _case_data(cached=True), reload_requested
        )
    assert response["details"]["meta"]["agent_summary_source"] == "llm"
    pipeline.assert_called_once()
    persist.assert_called_once()


def test_graph_only_change_still_serves_the_cache_but_reports_stale():
    graph_only = StalenessCheck(core_data_changed=False, graph_changed=True)
    response, _, _, pipeline = _run_service_with_staleness(_case_data(cached=True), graph_only)
    assert response["details"]["meta"]["agent_summary_source"] == "db_cache"
    assert response["details"]["meta"]["stale"] is True
    pipeline.assert_not_called()


def test_route_is_registered_with_a_response_model():
    route = next(r for r in server.app.routes if getattr(r, "path", None) == "/fraud_patterns")
    assert route.response_model is server.FraudPatternsResponse
    assert "POST" in route.methods


def test_closed_outcomes_query_falls_back_to_case_and_allegation_status():
    """Older AppWorks cases have no closed_date; closure must also be read from status (Rule 7 fallback)."""
    text = queries.CLOSED_CASE_OUTCOMES
    assert "$closed_statuses" in text
    assert "c.closed_date" in text
    assert "al.status" in text or "statuses" in text


def test_closed_statuses_come_from_the_rule_7_registry_entry():
    registry = {"Rule_07_Prior_Guilty": {"params": {"closed_case_statuses": ["Closed", " close "]}}}
    with mock.patch.object(fraud_patterns.rule_registry, "load_registry", return_value=registry):
        assert fraud_patterns._closed_statuses() == ["closed", "close"]


def test_find_fraud_patterns_passes_closed_statuses_to_the_outcomes_query():
    session = _full_session()
    _find(session)
    outcome_calls = [c for c in session.calls if c["query"] == queries.CLOSED_CASE_OUTCOMES]
    assert outcome_calls and outcome_calls[0]["params"]["closed_statuses"] == ["closed", "close"]


def test_trend_and_related_queries_fall_back_to_allegation_date_reported():
    assert "al.date_reported" in queries.CATEGORY_CASES
    assert "a2.date_reported" in queries.RELATED_CASES
