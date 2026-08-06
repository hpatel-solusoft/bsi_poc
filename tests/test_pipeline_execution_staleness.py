"""
Tests for api/pipeline_execution.evaluate_cache_staleness — the AI-32
glue that fetches the two timestamps a route needs and hands them to
core/narrative_staleness.check_staleness. core/narrative_staleness.py's
own test file covers the combining logic exhaustively; these tests only
need to prove the wiring: the right sources get read, the right
fallback is used when cache_generated_at isn't pre-supplied, and a Neo4j
outage degrades safely instead of raising.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
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

        sys.modules.setdefault("psycopg2", psycopg2)
        sys.modules.setdefault("psycopg2.extras", psycopg2_extras)
        sys.modules.setdefault("psycopg2.pool", psycopg2_pool)


_install_external_import_stubs()

from api import pipeline_execution  # noqa: E402
from reasoning_layer.neo4j_client import GraphUnavailableError  # noqa: E402

_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
_AFTER = _T0 + timedelta(hours=1)
_BEFORE = _T0 - timedelta(hours=1)


def test_uses_supplied_cache_generated_at_without_a_second_lookup():
    """When a route (e.g. /plan) already fetched
    case_ai_summary_store.updated_at for its own purposes, this must
    reuse it rather than reading Postgres a second time."""
    with mock.patch.object(
        pipeline_execution, "get_case_ai_summary_cache_updated_at"
    ) as mock_get_cache_ts, mock.patch.object(
        pipeline_execution, "get_last_inference_change_at", return_value=_AFTER
    ):
        check = pipeline_execution.evaluate_cache_staleness(
            "CASE-1", reload_ai_summary_requested=False, cache_generated_at=_T0
        )

    mock_get_cache_ts.assert_not_called()
    assert check.stale_reason == "graph"


def test_fetches_cache_generated_at_when_not_supplied():
    with mock.patch.object(
        pipeline_execution, "get_case_ai_summary_cache_updated_at", return_value=_T0
    ) as mock_get_cache_ts, mock.patch.object(
        pipeline_execution, "get_last_inference_change_at", return_value=_AFTER
    ):
        check = pipeline_execution.evaluate_cache_staleness("CASE-1", reload_ai_summary_requested=False)

    mock_get_cache_ts.assert_called_once_with("CASE-1")
    assert check.stale_reason == "graph"


def test_reload_ai_summary_true_is_core_data_regardless_of_graph_state():
    with mock.patch.object(
        pipeline_execution, "get_case_ai_summary_cache_updated_at", return_value=_T0
    ), mock.patch.object(pipeline_execution, "get_last_inference_change_at", return_value=_BEFORE):
        check = pipeline_execution.evaluate_cache_staleness("CASE-1", reload_ai_summary_requested=True)

    assert check.stale_reason == "core_data"
    assert check.should_rerun_full_pipeline is True


def test_neo4j_outage_degrades_to_no_graph_signal_not_a_raise():
    with mock.patch.object(
        pipeline_execution, "get_case_ai_summary_cache_updated_at", return_value=_T0
    ), mock.patch.object(
        pipeline_execution,
        "get_last_inference_change_at",
        side_effect=GraphUnavailableError("neo4j down"),
    ):
        check = pipeline_execution.evaluate_cache_staleness("CASE-1", reload_ai_summary_requested=False)

    assert check.stale_reason is None
    assert check.should_refresh is False


def test_neo4j_outage_with_reload_requested_still_reports_core_data():
    """A graph outage must never mask a genuine core_data-driven refresh
    request — it only removes the independent graph signal."""
    with mock.patch.object(
        pipeline_execution, "get_case_ai_summary_cache_updated_at", return_value=_T0
    ), mock.patch.object(
        pipeline_execution,
        "get_last_inference_change_at",
        side_effect=GraphUnavailableError("neo4j down"),
    ):
        check = pipeline_execution.evaluate_cache_staleness("CASE-1", reload_ai_summary_requested=True)

    assert check.stale_reason == "core_data"


def test_all_four_combinations_end_to_end_through_the_glue_function():
    """Same 4-combination matrix core/narrative_staleness.py's own tests
    cover, but exercised through the actual I/O-fetching function every
    route calls — not just the pure combiner."""
    cases = [
        (False, _BEFORE, None),
        (True, _BEFORE, "core_data"),
        (False, _AFTER, "graph"),
        (True, _AFTER, "both"),
    ]
    for reload_requested, last_inference_change_at, expected_reason in cases:
        with mock.patch.object(
            pipeline_execution, "get_case_ai_summary_cache_updated_at", return_value=_T0
        ), mock.patch.object(
            pipeline_execution,
            "get_last_inference_change_at",
            return_value=last_inference_change_at,
        ):
            check = pipeline_execution.evaluate_cache_staleness(
                "CASE-1", reload_ai_summary_requested=reload_requested
            )
        assert check.stale_reason == expected_reason, (
            f"reload_requested={reload_requested}, "
            f"last_inference_change_at={last_inference_change_at}: "
            f"expected {expected_reason!r}, got {check.stale_reason!r}"
        )
