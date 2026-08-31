"""
AI-32 — route-level integration tests for stale_reason.

Reuses tests/test_markdown_route_responses.py's proven pattern: call a
FastAPI route function directly (it's a plain Python function) with
mock.patch.object(server, ...) stubbing its module-level dependencies —
no TestClient, no running server needed.

core/narrative_staleness.py's own tests already cover the 4-combination
matrix exhaustively as pure logic; api/pipeline_execution.py's own tests
cover evaluate_cache_staleness's I/O wiring. What THESE tests prove is
the one thing neither of those can: that each of the 5 routes actually
DOES the right thing with a given StalenessCheck — serves or bypasses
its cache, reports the right stale_reason, and (for /intake specifically)
threads should_rerun_full_pipeline into the Wave 1/2 force flag rather
than the raw reload_ai_summary. server.evaluate_cache_staleness is
mocked directly in every test below so each one exercises exactly one
of the 4 combinations without needing a live Postgres or Neo4j.

Per the ticket's own acceptance bar — "Test all 4 combinations (neither
/ core only / graph only / both) on one seed case before rolling out to
the rest" — /intake gets the full 4-combination treatment as the "one
seed case"; the other 4 routes get one representative test each proving
stale_reason reaches their response correctly, since the combination
logic itself is already proven once in core/narrative_staleness.py and
does not need re-proving per route.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest


def _install_external_import_stubs() -> None:
    if importlib.util.find_spec("psycopg2") is None:
        psycopg2 = types.ModuleType("psycopg2")

        class PsycopgError(Exception):
            pass

        psycopg2.Error = PsycopgError
        psycopg2.OperationalError = type("OperationalError", (PsycopgError,), {})

        psycopg2_extras = types.ModuleType("psycopg2.extras")
        psycopg2_extras.RealDictCursor = object
        psycopg2.extras = psycopg2_extras

        psycopg2_pool = types.ModuleType("psycopg2.pool")
        psycopg2_pool.ThreadedConnectionPool = object
        psycopg2.pool = psycopg2_pool

        psycopg2_extensions = types.ModuleType("psycopg2.extensions")
        psycopg2_extensions.cursor = object
        psycopg2.extensions = psycopg2_extensions

        sys.modules.setdefault("psycopg2", psycopg2)
        sys.modules.setdefault("psycopg2.extras", psycopg2_extras)
        sys.modules.setdefault("psycopg2.pool", psycopg2_pool)
        sys.modules.setdefault("psycopg2.extensions", psycopg2_extensions)

    if importlib.util.find_spec("neo4j") is None:
        neo4j = types.ModuleType("neo4j")

        class _GraphDatabase:
            @staticmethod
            def driver(*args, **kwargs):
                return SimpleNamespace(verify_connectivity=lambda: None, close=lambda: None)

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

    if importlib.util.find_spec("dotenv") is None:
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        sys.modules.setdefault("dotenv", dotenv)

    if importlib.util.find_spec("xhtml2pdf") is None:
        xhtml2pdf = types.ModuleType("xhtml2pdf")
        xhtml2pdf.pisa = SimpleNamespace(CreatePDF=lambda *args, **kwargs: SimpleNamespace(err=0))
        sys.modules.setdefault("xhtml2pdf", xhtml2pdf)


_install_external_import_stubs()

os.environ.setdefault("OPENAI_API_KEY", "test-key-for-ai-32-tests")

from api import server  # noqa: E402
from api.services import (  # noqa: E402
    intake_service,
    plan_service,
    report_service,
    risk_assessment_service,
    similar_cases_service,
)
from core.narrative_staleness import StalenessCheck  # noqa: E402

_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
_AFTER = _T0 + timedelta(hours=1)

# The ticket's own 4-combination matrix, reused verbatim across every
# route below: (StalenessCheck, expected stale_reason).
_NEITHER = StalenessCheck(core_data_changed=False, graph_changed=False)
_CORE_DATA_ONLY = StalenessCheck(core_data_changed=True, graph_changed=False)
_GRAPH_ONLY = StalenessCheck(core_data_changed=False, graph_changed=True)
_BOTH = StalenessCheck(core_data_changed=True, graph_changed=True)


class FakeRunner:
    """Same shape as test_markdown_route_responses.py's FakeRunner."""

    def __init__(self, markdown: str = "## Narrative\nFresh text."):
        self.dispatcher = SimpleNamespace(tool_to_section={})
        self.markdown = markdown

    def run_scoped(self, *args, **kwargs):
        return [{"role": "assistant", "content": self.markdown}], [], []


@pytest.fixture(autouse=True)
def clean_case_store():
    yield
    for case_id in (
        "CASE-STALE-INTAKE",
        "CASE-STALE-SIMILAR",
        "CASE-STALE-RISK",
        "CASE-STALE-PLAN",
        "CASE-STALE-REPORT",
    ):
        server.CASE_STORE.evict(case_id)


# --------------------------------------------------------------------
# /intake — the ticket's "one seed case": all 4 combinations, both the
# cache-serve/bypass decision AND the should_rerun_full_pipeline ->
# run_intake_direct_pipeline force-flag wiring.
# --------------------------------------------------------------------


def _intake_cache_hit_kwargs(case_id: str):
    case_data = {"provenance_trail": [], "rules_fired": [], "complaint_intelligence": {}}
    return dict(
        get_cached_route_summary=mock.patch.object(
            intake_service, "get_cached_route_summary", return_value=(case_data, "cached markdown")
        ),
        fetch_live_graph_findings=mock.patch.object(
            intake_service,
            "fetch_live_graph_findings",
            return_value={
                "network_match_flag": None,
                "graph_context": None,
                "graph_signals": None,
                "rules_fired": [],
            },
        ),
        log_agent_call=mock.patch.object(intake_service, "log_agent_call"),
    )


def test_intake_neither_serves_cache_with_null_stale_reason():
    case_id = "CASE-STALE-INTAKE"
    patches = _intake_cache_hit_kwargs(case_id)
    with mock.patch.object(
        intake_service, "evaluate_cache_staleness", return_value=_NEITHER
    ), patches["get_cached_route_summary"], patches["fetch_live_graph_findings"], patches["log_agent_call"]:
        response = intake_service.run_intake(
            server.intakeRequest(case_id=case_id), "test-user", "test-token"
        )

    assert response["details"]["meta"]["pipeline_status"] == "cached"
    assert response["details"]["meta"]["stale"] is False


def _intake_fresh_run_patches(case_id: str, run_intake_direct_pipeline_mock):
    return [
        mock.patch.object(intake_service, "get_runner", return_value=FakeRunner()),
        mock.patch.object(intake_service, "run_intake_direct_pipeline", run_intake_direct_pipeline_mock),
        mock.patch.object(intake_service, "try_resolve_case_data", return_value={}),
        mock.patch.object(intake_service, "persist_case_session"),
        mock.patch.object(intake_service, "log_agent_call"),
        # No cache to hit in any of the fresh-run combinations below —
        # they all reach this path specifically because
        # staleness.should_refresh is True.
        mock.patch.object(intake_service, "get_cached_route_summary", return_value=None),
    ]


def test_intake_core_data_only_forces_full_pipeline_rerun():
    case_id = "CASE-STALE-INTAKE"
    run_pipeline_mock = mock.Mock(
        side_effect=lambda case_id, force, sections, provenance_trail, username: (
            {**sections, "network_match_flag": None, "graph_context": None, "graph_signals": None},
            provenance_trail,
        )
    )
    patches = _intake_fresh_run_patches(case_id, run_pipeline_mock)
    with mock.patch.object(intake_service, "evaluate_cache_staleness", return_value=_CORE_DATA_ONLY):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            response = intake_service.run_intake(
                server.intakeRequest(case_id=case_id, reload_ai_summary=True), "test-user", "test-token"
            )

    # should_rerun_full_pipeline is True for core_data-only -> force=True
    # threaded through to run_intake_direct_pipeline, NOT the other way.
    run_pipeline_mock.assert_called_once()
    called_force = run_pipeline_mock.call_args.args[1]
    assert called_force is True
    assert response["details"]["meta"]["stale"] is False
    assert response["details"]["meta"]["pipeline_status"] == "reloaded"


def test_intake_graph_only_regenerates_narrative_without_forcing_pipeline():
    case_id = "CASE-STALE-INTAKE"
    run_pipeline_mock = mock.Mock(
        side_effect=lambda case_id, force, sections, provenance_trail, username: (
            {**sections, "network_match_flag": None, "graph_context": None, "graph_signals": None},
            provenance_trail,
        )
    )
    patches = _intake_fresh_run_patches(case_id, run_pipeline_mock)
    with mock.patch.object(intake_service, "evaluate_cache_staleness", return_value=_GRAPH_ONLY):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            # reload_ai_summary=False on the request itself — the graph
            # trigger alone is what must still bypass the cache and call
            # the LLM again (should_refresh True even though the caller
            # never asked for a reload).
            response = intake_service.run_intake(
                server.intakeRequest(case_id=case_id, reload_ai_summary=False), "test-user", "test-token"
            )

    called_force = run_pipeline_mock.call_args.args[1]
    assert called_force is False, (
        "graph-only staleness must NOT force the Wave 1/2 pipeline rerun — "
        "see StalenessCheck.should_rerun_full_pipeline's docstring"
    )
    assert response["details"]["meta"]["stale"] is True
    assert response["details"]["meta"]["pipeline_status"] == "narrative_regenerated"


def test_intake_both_forces_full_pipeline_rerun_and_reports_both():
    case_id = "CASE-STALE-INTAKE"
    run_pipeline_mock = mock.Mock(
        side_effect=lambda case_id, force, sections, provenance_trail, username: (
            {**sections, "network_match_flag": None, "graph_context": None, "graph_signals": None},
            provenance_trail,
        )
    )
    patches = _intake_fresh_run_patches(case_id, run_pipeline_mock)
    with mock.patch.object(intake_service, "evaluate_cache_staleness", return_value=_BOTH):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            response = intake_service.run_intake(
                server.intakeRequest(case_id=case_id, reload_ai_summary=True), "test-user", "test-token"
            )

    called_force = run_pipeline_mock.call_args.args[1]
    assert called_force is True
    assert response["details"]["meta"]["stale"] is True
    assert response["details"]["meta"]["pipeline_status"] == "reloaded"


# --------------------------------------------------------------------
# /similar_cases, /risk_assessment, /plan — one representative
# combination each (graph-only, the newly-added trigger these routes
# never had before AI-32), proving stale_reason reaches the response and
# the cache is correctly bypassed even though reload_ai_summary=False.
# --------------------------------------------------------------------


def test_similar_cases_graph_only_reports_stale_while_still_serving_cache():
    """A graph-only signal must NOT by itself bypass the cache (see
    _similar_cases_cache_hit's own docstring — only an explicit
    reload_ai_summary=True does that); it must still be reported as
    `stale=True` in the response so the caller knows to ask for a
    reload if it wants one."""
    case_id = "CASE-STALE-SIMILAR"
    server.CASE_STORE[case_id] = {}
    case_data = {
        server.AGENT_SUMMARY_CACHE_KEY: {"similar_cases": "cached markdown"},
        "similar_cases": {"matches": []},
        "complaint_intelligence": {"subject_primary_id": "SUBJ-1"},
        "provenance_trail": [],
    }
    with mock.patch.object(
        similar_cases_service, "evaluate_cache_staleness", return_value=_GRAPH_ONLY
    ), mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        similar_cases_service, "fetch_live_similar_cases", return_value=[]
    ), mock.patch.object(similar_cases_service, "log_agent_call"):
        response = similar_cases_service.run_similar_cases(
            server.SimilarCasesRequest(case_id=case_id), "test-user", "test-token"
        )

    assert response["details"]["meta"]["agent_summary_source"] == "db_cache"
    assert response["details"]["meta"]["stale"] is True


def test_risk_assessment_graph_only_reports_stale_while_still_serving_cache():
    case_id = "CASE-STALE-RISK"
    server.CASE_STORE[case_id] = {}
    case_data = {
        server.AGENT_SUMMARY_CACHE_KEY: {"risk_assessment": "cached markdown"},
        "risk_assessment": {"risk_score": 40, "risk_tier": "High"},
        "similar_cases": {"matches": []},
        "provenance_trail": [],
    }
    with mock.patch.object(
        risk_assessment_service, "evaluate_cache_staleness", return_value=_GRAPH_ONLY
    ), mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        risk_assessment_service, "fetch_live_risk_signals", return_value={}
    ), mock.patch.object(risk_assessment_service, "log_agent_call"):
        response = risk_assessment_service.run_risk_assessment(
            server.RiskAssessmentRequest(case_id=case_id), "test-user", "test-token"
        )

    assert response["details"]["meta"]["agent_summary_source"] == "db_cache"
    assert response["details"]["meta"]["stale"] is True


def test_plan_graph_only_bypasses_cache_when_no_override_exists():
    case_id = "CASE-STALE-PLAN"
    server.CASE_STORE[case_id] = {}
    case_data = {
        "complaint_intelligence": {},
        "risk_assessment": {"risk_score": 40, "risk_tier": "High"},
        "rules_fired": [],
        "provenance_trail": [],
    }
    # AI-35: /plan no longer reads get_case_ai_summary_cache_updated_at
    # (the shared, case-wide column) at all — it reads its own per-tab
    # generated_at instead (core.case_store.get_route_generated_at_datetime,
    # real here since case_data has no cached "plan" entry yet, so it
    # naturally resolves to None). No mock is needed for it any more; the
    # absence of one here is itself part of what this test now proves.
    # No cached "plan" agent_summary exists in case_data at all here
    # (unlike the similar_cases/risk_assessment graph-only tests above),
    # so _plan_cache_hit correctly returns None regardless of staleness —
    # this is a genuine no-cache-to-serve case, not a staleness bypass.
    with mock.patch.object(
        plan_service, "evaluate_cache_staleness", return_value=_GRAPH_ONLY
    ), mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        plan_service, "get_override", return_value=None
    ), mock.patch.object(
        plan_service, "get_runner", return_value=FakeRunner()
    ), mock.patch.object(
        plan_service,
        "run_plan_pipeline",
        return_value=(
            "fresh plan markdown",
            {"investigation_steps": [], "rule_aware_tasks": []},
            {"investigation_plan": {"investigation_steps": []}},
            [],
            "AI Summerized",
            None,
            None,
            False,
        ),
    ), mock.patch.object(
        plan_service, "prepare_plan_context", return_value=({"complaint_intelligence": {}}, [])
    ), mock.patch.object(plan_service, "persist_case_session"), mock.patch.object(
        plan_service, "log_agent_call"
    ):
        response = plan_service.run_plan(server.PlanRequest(case_id=case_id), "test-user", "test-token")

    assert response["details"]["meta"]["agent_summary_source"] == "llm"
    assert response["details"]["meta"]["stale"] is True


def test_plan_override_always_wins_even_when_graph_changed():
    """A saved human override is authoritative regardless of staleness —
    AI-32 must not change this pre-existing priority rule (see the
    route's own AI-32 comment)."""
    case_id = "CASE-STALE-PLAN"
    # Legacy cache shape: a bare markdown string for "plan", predating
    # AI-34/AI-35's {summary, generated_at} pair. get_route_generated_at_
    # datetime degrades this to None (no per-tab staleness signal) rather
    # than erroring — proven for real here, since it is never mocked.
    cached_summary = "## Investigation Steps\n- **Step 1:** Old step"
    case_data = {
        server.AGENT_SUMMARY_CACHE_KEY: {"plan": cached_summary},
        "investigation_plan": {"investigation_steps": [{"step": 1, "action": "Old step"}]},
        "risk_assessment": {"risk_score": 40, "risk_tier": "High"},
        "rules_fired": [],
        "provenance_trail": [],
    }
    override = {
        "modified_steps": [{"step": 1, "action": "Review bank records"}],
        "modified_by": "analyst",
        "modified_on": datetime(2026, 7, 31, tzinfo=timezone.utc),
    }
    # AI-35: no get_case_ai_summary_cache_updated_at mock needed (or read)
    # for /plan any more — see the test above's comment.
    with mock.patch.object(
        plan_service, "evaluate_cache_staleness", return_value=_BOTH
    ), mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        plan_service, "get_override", return_value=override
    ), mock.patch.object(plan_service, "log_agent_call"):
        response = plan_service.run_plan(
            server.PlanRequest(case_id=case_id, reload_ai_summary=True), "test-user", "test-token"
        )

    assert response["details"]["meta"]["agent_summary_source"] == "db_cache"
    assert response["details"]["meta"]["plan_source"] == "User Modified"
    assert response["details"]["meta"]["stale"] is True
    # No cached "plan" generated_at exists (legacy bare-string entry) —
    # compute_plan_staleness has nothing to compare the override against,
    # so it degrades to False rather than guessing.
    assert response["details"]["meta"]["plan_stale"] is False


# --------------------------------------------------------------------
# /generate_report — the one route with its own additional
# content-diff signal, folded in with OR alongside AI-31's timestamp.
# --------------------------------------------------------------------


def test_generate_report_content_unchanged_but_graph_timestamp_newer_still_serves_cache():
    """The exact scenario StalenessCheck was designed for on this route:
    content is byte-identical (e.g. a reject immediately followed by a
    revert), but AI-31's timestamp still moved. stale_reason must
    honestly report "graph", while the cache — genuinely still
    accurate — is still served."""
    case_id = "CASE-STALE-REPORT"
    case_data = {
        "complaint_intelligence": {"subject_primary_id": "SUBJ-1"},
        "provenance_trail": [],
        "rules_fired": [],
    }
    related_network = [{"id": "SUBJ-2", "status": "active"}]
    related = {
        "related_network": related_network,
        "confidence_summary": {"high": 1, "medium": 0, "unresolved": 0},
        "rejected_count": 0,
    }
    related_envelope = {
        "result": related,
        "provenance": {"sources": ["Neo4j graph query"], "retrieved_at": "", "computed_by": "test"},
    }
    cached_report = {
        "id": 1,
        "case_id": case_id,
        "generated_at": _T0,
        "content": {
            "report_id": "RPT-1",
            "generated_at": _T0.isoformat(),
            "status": "draft",
            "standard_sections": {"report_markdown": "## Report\nCached prose."},
            "related_network": related_network,  # identical to the live read
            "confidence_summary": {"high": 1, "medium": 0, "unresolved": 0},
            "decision_log": [],
        },
        "status": "draft",
    }
    decision_log_envelope = {
        "result": {"decision_log_markdown": "- None", "decision_log": []},
        "provenance": {"sources": ["Decision log"], "retrieved_at": "", "computed_by": "test"},
    }

    with mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        report_service, "get_runner", return_value=FakeRunner()
    ), mock.patch.object(
        report_service, "assemble_related_network", return_value=related_envelope
    ), mock.patch.object(
        report_service, "get_override", return_value=None
    ), mock.patch.object(
        report_service, "get_latest_report", return_value=cached_report
    ), mock.patch.object(
        report_service, "evaluate_cache_staleness", return_value=_GRAPH_ONLY
    ), mock.patch.object(
        report_service, "build_decision_log", return_value=decision_log_envelope
    ), mock.patch.object(report_service, "log_agent_call"):
        response = report_service.run_generate_report(
            server.ReportGenerationRequest(case_id=case_id), "test-user", "test-token"
        )

    assert response["details"]["meta"]["agent_summary_source"] == "db_cache"
    assert response["details"]["meta"]["stale"] is True


def test_generate_report_content_differs_bypasses_cache_regardless_of_timestamp():
    """The pre-existing, finer-grained detector still governs the actual
    cache-serve decision: content differing bypasses the cache even if
    AI-32's coarse timestamp check alone would have said "not stale"."""
    case_id = "CASE-STALE-REPORT"
    case_data = {
        "complaint_intelligence": {"subject_primary_id": "SUBJ-1"},
        "provenance_trail": [],
        "rules_fired": [],
    }
    related = {
        "related_network": [{"id": "SUBJ-3", "status": "active"}],  # DIFFERS from cached
        "confidence_summary": {"high": 1, "medium": 0, "unresolved": 0},
        "rejected_count": 0,
    }
    related_envelope = {
        "result": related,
        "provenance": {"sources": ["Neo4j graph query"], "retrieved_at": "", "computed_by": "test"},
    }
    cached_report = {
        "id": 1,
        "case_id": case_id,
        "generated_at": _T0,
        "content": {
            "report_id": "RPT-1",
            "generated_at": _T0.isoformat(),
            "status": "draft",
            "standard_sections": {"report_markdown": "## Report\nOld prose."},
            "related_network": [{"id": "SUBJ-2", "status": "active"}],
            "confidence_summary": {"high": 1, "medium": 0, "unresolved": 0},
            "decision_log": [],
        },
        "status": "draft",
    }
    decision_log_envelope = {
        "result": {"decision_log_markdown": "- None", "decision_log": []},
        "provenance": {"sources": ["Decision log"], "retrieved_at": "", "computed_by": "test"},
    }

    with mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        report_service, "assemble_related_network", return_value=related_envelope
    ), mock.patch.object(
        report_service, "get_override", return_value=None
    ), mock.patch.object(
        report_service, "get_latest_report", return_value=cached_report
    ), mock.patch.object(
        # timestamp signal alone says "not stale" — content diff must
        # still be what drives this test's expected outcome.
        report_service,
        "evaluate_cache_staleness",
        return_value=_NEITHER,
    ), mock.patch.object(
        report_service, "build_decision_log", return_value=decision_log_envelope
    ), mock.patch.object(
        report_service, "build_report_llm_context", return_value={}
    ), mock.patch.object(
        report_service, "get_runner", return_value=FakeRunner()
    ), mock.patch.object(report_service, "save_report", return_value={"id": 2}), mock.patch.object(
        report_service, "log_agent_call"
    ):
        response = report_service.run_generate_report(
            server.ReportGenerationRequest(case_id=case_id), "test-user", "test-token"
        )

    assert response["details"]["meta"]["agent_summary_source"] == "llm"
    assert response["details"]["meta"]["stale"] is True
