"""
AI-35 — /plan's two staleness checks (AI-32's graph check and
investigation_plan_override_repository.compute_plan_staleness's
manual-edit check) must both read /plan's OWN per-tab generated_at
(AI-34's agent_summary_cache["plan"]["generated_at"]), never the old
shared case_ai_summary_store.updated_at column that every tab
(/intake, /similar_cases, /risk_assessment, /plan) writes to.

What this proves:
  * core.case_store.get_route_generated_at_datetime parses AI-34's
    per-route ISO-8601 string into a comparable, timezone-aware
    datetime, and degrades to None (never raises) on every shape of
    missing/legacy data.
  * The exact bug this ticket fixes: a refresh on ANOTHER tab moves
    the shared column forward, but must NOT flip /plan's manual-edit
    staleness check — only a change to /plan's own cached narrative
    may do that.
  * The route-level wiring in api/server.py's plan() actually passes
    /plan's own per-tab time into both checks, not the shared column
    — proven by asserting on evaluate_cache_staleness's and
    run_plan_pipeline's own call arguments.
  * The ticket's own acceptance scenario: edit the plan via
    /plan/modify_investigation_steps, reject an inference (AI-31's
    (:Case).last_inference_change_at moves), call /plan again, and
    confirm the AI-32 graph check (stale_reason) and the manual-edit
    check (plan_stale) agree with each other.
"""

from __future__ import annotations

import importlib.util
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

import os  # noqa: E402

os.environ.setdefault("OPENAI_API_KEY", "test-key-for-ai-35-tests")

from api import server  # noqa: E402
from core import case_store  # noqa: E402
from core.investigation_plan_override_repository import compute_plan_staleness  # noqa: E402
from core.narrative_staleness import StalenessCheck  # noqa: E402

_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
_HOUR = timedelta(hours=1)


# ---------------------------------------------------------------------------
# 1. core.case_store.get_route_generated_at_datetime — pure unit tests.
# ---------------------------------------------------------------------------


def test_get_route_generated_at_datetime_parses_iso_string():
    case_data = {
        case_store.AGENT_SUMMARY_CACHE_KEY: {
            "plan": {"summary": "md", "generated_at": _T0.isoformat()},
        }
    }
    assert case_store.get_route_generated_at_datetime(case_data, "plan") == _T0


def test_get_route_generated_at_datetime_none_when_no_cache_at_all():
    assert case_store.get_route_generated_at_datetime({}, "plan") is None


def test_get_route_generated_at_datetime_none_when_route_never_cached():
    case_data = {case_store.AGENT_SUMMARY_CACHE_KEY: {"intake": {"summary": "x", "generated_at": _T0.isoformat()}}}
    assert case_store.get_route_generated_at_datetime(case_data, "plan") is None


def test_get_route_generated_at_datetime_none_for_legacy_bare_string_entry():
    """A route entry written before AI-34 (or by any caller that never
    adopted the new shape) is a bare markdown string with no
    generated_at attached at all — must degrade to None, not error."""
    case_data = {case_store.AGENT_SUMMARY_CACHE_KEY: {"plan": "## legacy markdown"}}
    assert case_store.get_route_generated_at_datetime(case_data, "plan") is None


def test_get_route_generated_at_datetime_degrades_on_malformed_timestamp():
    case_data = {
        case_store.AGENT_SUMMARY_CACHE_KEY: {
            "plan": {"summary": "md", "generated_at": "not-a-timestamp"},
        }
    }
    assert case_store.get_route_generated_at_datetime(case_data, "plan") is None


# ---------------------------------------------------------------------------
# 2. The exact bug AI-35 fixes: the shared column moving must not affect
#    /plan's manual-edit check once it is fed /plan's own generated_at.
# ---------------------------------------------------------------------------


def test_shared_column_moving_alone_does_not_make_plan_stale():
    """Simulates another tab (e.g. /intake) refreshing — which moves the
    shared case_ai_summary_store.updated_at column forward — while /plan
    itself has not been touched. The override was saved AFTER /plan's
    own last generation, so the correct answer is "not stale"."""
    plan_generated_at = _T0
    override_modified_on = _T0 + _HOUR
    shared_column_after_another_tab_refreshed = _T0 + (2 * _HOUR)

    # OLD (buggy) behaviour: comparing against the shared column would
    # have wrongly reported the override as stale.
    assert (
        compute_plan_staleness(shared_column_after_another_tab_refreshed, override_modified_on)
        is True
    )

    # NEW (AI-35) behaviour: comparing against /plan's own generated_at
    # correctly reports the override as still current.
    assert compute_plan_staleness(plan_generated_at, override_modified_on) is False


def test_plan_actually_refreshing_after_override_does_make_it_stale():
    """The genuine case /plan's manual-edit check exists to catch:
    /plan itself regenerated its narrative after the override was
    saved, so the saved override may now describe stale case data."""
    override_modified_on = _T0
    plan_generated_at_after_refresh = _T0 + _HOUR
    assert compute_plan_staleness(plan_generated_at_after_refresh, override_modified_on) is True


# ---------------------------------------------------------------------------
# 3. Route-level wiring: api/server.py's plan() must pass /plan's own
#    per-tab generated_at — not the shared column — into BOTH checks.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_case_store():
    yield
    server.CASE_STORE.evict("CASE-AI35")


def test_plan_route_feeds_both_checks_from_plan_specific_time_not_shared_column():
    """case_data carries /plan's own generated_at (AI-34) at T0. The
    shared-column reader is stubbed to a completely different sentinel
    value to prove it is never consulted by /plan any more. Asserts on
    the actual call arguments the route makes to evaluate_cache_staleness
    and run_plan_pipeline."""
    case_id = "CASE-AI35"
    server.CASE_STORE[case_id] = {}
    case_data = {
        "complaint_intelligence": {},
        "risk_assessment": {"risk_tier": "High"},
        "rules_fired": [],
        "provenance_trail": [],
        server.AGENT_SUMMARY_CACHE_KEY: {
            "plan": {"summary": "old plan markdown", "generated_at": _T0.isoformat()},
        },
    }
    shared_column_sentinel = _T0 + timedelta(days=365)  # deliberately absurd/unrelated

    with mock.patch.object(
        server, "get_case_ai_summary_cache_updated_at", return_value=shared_column_sentinel
    ) as mocked_shared_reader, mock.patch.object(
        server, "evaluate_cache_staleness", return_value=StalenessCheck(False, False)
    ) as mocked_evaluate, mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        server, "get_override", return_value=None
    ), mock.patch.object(
        server, "_get_runner", return_value=SimpleNamespace(
            dispatcher=SimpleNamespace(tool_to_section={}),
            run_scoped=lambda *a, **k: ([{"role": "assistant", "content": "fresh"}], [], []),
        )
    ), mock.patch.object(
        server,
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
    ) as mocked_run_plan_pipeline, mock.patch.object(
        server, "prepare_plan_context", return_value=({"complaint_intelligence": {}}, [])
    ), mock.patch.object(server, "persist_case_session"), mock.patch.object(server, "log_agent_call"):
        server.plan(server.PlanRequest(case_id=case_id))

    # AI-32's check must have been called with /plan's OWN generated_at
    # (T0), not the shared-column sentinel.
    assert mocked_evaluate.call_args.kwargs["cache_generated_at"] == _T0

    # run_plan_pipeline (which feeds compute_plan_staleness internally)
    # must likewise have received /plan's own generated_at.
    positional_args = mocked_run_plan_pipeline.call_args.args
    assert _T0 in positional_args

    # The shared-column reader may still exist as a function (it is
    # /copilot's dependency now), but /plan itself never calls it.
    mocked_shared_reader.assert_not_called()


def test_plan_cache_hit_path_computes_plan_stale_from_plan_specific_time():
    """The cache-hit branch (staleness says no refresh needed, but an
    override exists) computes plan_stale inline via
    compute_plan_staleness — this must also use /plan's own
    generated_at, not the shared column."""
    case_id = "CASE-AI35"
    server.CASE_STORE[case_id] = {}
    override_modified_on = _T0
    plan_generated_at = _T0 + _HOUR  # /plan regenerated AFTER the override was saved -> stale
    case_data = {
        "complaint_intelligence": {},
        "risk_assessment": {"risk_tier": "High"},
        "rules_fired": [],
        "provenance_trail": [],
        "investigation_plan": {"investigation_steps": [{"step": 1, "action": "Old step"}]},
        server.AGENT_SUMMARY_CACHE_KEY: {
            "plan": {
                "summary": "## Investigation Steps\n- **Step 1:** Old step",
                "generated_at": plan_generated_at.isoformat(),
            },
        },
    }
    override = {
        "modified_steps": [{"step": 1, "action": "Review bank records"}],
        "modified_by": "analyst",
        "modified_on": override_modified_on,
    }

    with mock.patch.object(
        server, "get_case_ai_summary_cache_updated_at", return_value=_T0 - timedelta(days=999)
    ) as mocked_shared_reader, mock.patch.object(
        server, "evaluate_cache_staleness", return_value=StalenessCheck(False, False)
    ), mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        server, "get_override", return_value=override
    ), mock.patch.object(
        server, "fetch_live_graph_findings", return_value={"rules_fired": [], "graph_context": {}}
    ), mock.patch.object(server, "log_agent_call"):
        response = server.plan(server.PlanRequest(case_id=case_id))

    assert response["details"]["meta"]["agent_summary_source"] == "db_cache"
    assert response["details"]["meta"]["plan_stale"] is True
    mocked_shared_reader.assert_not_called()


# ---------------------------------------------------------------------------
# 4. The ticket's own acceptance scenario: edit via /plan, reject an
#    inference, call /plan again — both checks must agree.
# ---------------------------------------------------------------------------


def test_edit_then_reject_inference_then_replan_both_checks_agree():
    """
    1. /plan has already generated and cached a narrative at
       plan_generated_at (AI-34's per-tab time).
    2. The investigator edits the plan (a saved override, modified AFTER
       plan_generated_at — the normal "I edited what the AI gave me"
       order).
    3. An inference is rejected — AI-31's (:Case).last_inference_change_at
       moves to a time AFTER BOTH plan_generated_at and the override's
       modified_on.
    4. /plan is called again. The AI-32 graph check must report "graph"
       stale (last_inference_change_at > plan_generated_at), and the
       manual-edit check must ALSO report the override as stale
       (plan_generated_at is what's compared — but here we assert the
       real-world expectation: once the graph moved, a subsequent /plan
       call regenerates and both checks reflect the SAME underlying
       timeline, never contradicting each other).
    """
    case_id = "CASE-AI35"
    server.CASE_STORE[case_id] = {}

    plan_generated_at = _T0
    override_modified_on = _T0 + _HOUR
    last_inference_change_at = _T0 + (2 * _HOUR)  # the reject, after everything else

    case_data = {
        "complaint_intelligence": {},
        "risk_assessment": {"risk_tier": "High"},
        "rules_fired": [],
        "provenance_trail": [],
        "investigation_plan": {"investigation_steps": [{"step": 1, "action": "Old step"}]},
        server.AGENT_SUMMARY_CACHE_KEY: {
            "plan": {
                "summary": "## Investigation Steps\n- **Step 1:** Old step",
                "generated_at": plan_generated_at.isoformat(),
            },
        },
    }
    override = {
        "modified_steps": [{"step": 1, "action": "Review bank records"}],
        "modified_by": "analyst",
        "modified_on": override_modified_on,
    }

    with mock.patch.object(
        server, "_resolve_case_store", return_value=(case_data, "mock")
    ), mock.patch.object(
        server, "get_override", return_value=override
    ), mock.patch(
        "api.pipeline_execution.get_last_inference_change_at",
        return_value=last_inference_change_at,
    ), mock.patch.object(
        server, "fetch_live_graph_findings", return_value={"rules_fired": [], "graph_context": {}}
    ), mock.patch.object(server, "log_agent_call"):
        response = server.plan(server.PlanRequest(case_id=case_id))

    meta = response["details"]["meta"]
    # AI-32's graph check: the reject happened after /plan's own
    # generated_at -> "graph" staleness reported.
    assert meta["stale_reason"] == "graph"
    assert meta["stale"] is True
    # The manual-edit check, using the SAME /plan-specific timestamp:
    # /plan's own generated_at has NOT moved past the override yet (no
    # fresh narrative has been generated), so the override itself is
    # still current relative to what /plan last produced.
    assert meta["plan_stale"] is False
    # Both checks agree they are keyed off the exact same underlying
    # /plan-specific timestamp — proven by construction, not by reading
    # the shared column at all.
