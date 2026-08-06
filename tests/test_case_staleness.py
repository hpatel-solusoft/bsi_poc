"""
Tests for reasoning_layer/case_staleness.py — the shared reader for
AI-31's (:Case).last_inference_change_at, used by both
reasoning_layer/rule_audit.py (raw string) and AI-32's
core/narrative_staleness.py check (parsed datetime, via
api/pipeline_execution.py).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone
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

from reasoning_layer import case_staleness  # noqa: E402


class FakeResult:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record


class FakeSession:
    def __init__(self, record):
        self._record = record
        self.calls = []

    def run(self, query, **params):
        self.calls.append({"query": query, "params": params})
        return FakeResult(self._record)

    def close(self):
        pass


def fake_session_cm(session):
    @contextmanager
    def _cm(*args, **kwargs):
        yield session

    return _cm


def test_get_raw_returns_the_exact_stored_string():
    session = FakeSession({"last_inference_change_at": "2026-08-05T12:00:00+00:00"})
    with mock.patch.object(case_staleness, "get_session", fake_session_cm(session)):
        value = case_staleness.get_last_inference_change_at_raw("CASE-1")
    assert value == "2026-08-05T12:00:00+00:00"
    assert session.calls[0]["params"] == {"case_id": "CASE-1"}


def test_get_raw_returns_none_when_case_node_missing():
    session = FakeSession(None)
    with mock.patch.object(case_staleness, "get_session", fake_session_cm(session)):
        value = case_staleness.get_last_inference_change_at_raw("CASE-1")
    assert value is None


def test_get_raw_returns_none_when_field_never_set():
    """The Case node exists but has never had a reject/revert against it
    — the property key legitimately does not exist, so Neo4j returns
    null for it rather than omitting the row."""
    session = FakeSession({"last_inference_change_at": None})
    with mock.patch.object(case_staleness, "get_session", fake_session_cm(session)):
        value = case_staleness.get_last_inference_change_at_raw("CASE-1")
    assert value is None


def test_get_parsed_returns_timezone_aware_datetime():
    session = FakeSession({"last_inference_change_at": "2026-08-05T12:00:00+00:00"})
    with mock.patch.object(case_staleness, "get_session", fake_session_cm(session)):
        value = case_staleness.get_last_inference_change_at("CASE-1")
    assert value == datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert value.tzinfo is not None


def test_get_parsed_returns_none_on_missing_case():
    session = FakeSession(None)
    with mock.patch.object(case_staleness, "get_session", fake_session_cm(session)):
        assert case_staleness.get_last_inference_change_at("CASE-1") is None


def test_get_parsed_degrades_to_none_on_malformed_timestamp():
    """A value that fails ISO-8601 parsing must never raise — every
    cached-narrative endpoint calling this needs a safe degrade, not a
    500 caused by one bad property value."""
    session = FakeSession({"last_inference_change_at": "not-a-timestamp"})
    with mock.patch.object(case_staleness, "get_session", fake_session_cm(session)):
        value = case_staleness.get_last_inference_change_at("CASE-1")
    assert value is None


def test_get_parsed_round_trips_what_rejection_py_actually_writes():
    """End-to-end sanity: the exact format
    reasoning_layer.rejection._touch_case_last_inference_change writes
    (datetime.now(timezone.utc).isoformat()) must parse back cleanly."""
    written_at = datetime.now(timezone.utc).isoformat()
    session = FakeSession({"last_inference_change_at": written_at})
    with mock.patch.object(case_staleness, "get_session", fake_session_cm(session)):
        value = case_staleness.get_last_inference_change_at("CASE-1")
    assert value is not None
    assert value.isoformat() == written_at
