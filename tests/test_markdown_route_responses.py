from __future__ import annotations

import inspect
import importlib.util
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest


def _install_external_import_stubs() -> None:
    if importlib.util.find_spec("psycopg2") is not None:
        psycopg2 = None
    else:
        psycopg2 = types.ModuleType("psycopg2")

        class PsycopgError(Exception):
            pass

        class OperationalError(PsycopgError):
            pass

        psycopg2.Error = PsycopgError
        psycopg2.OperationalError = OperationalError

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

        class AuthError(Neo4jError):
            pass

        class ServiceUnavailable(Neo4jError):
            pass

        neo4j_exceptions.Neo4jError = Neo4jError
        neo4j_exceptions.AuthError = AuthError
        neo4j_exceptions.ServiceUnavailable = ServiceUnavailable
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

from api import server


MARKDOWN = "## Heading\nRaw **markdown** body."


class FakeRunner:
    def __init__(self, markdown: str = MARKDOWN, provenance: list[dict] | None = None):
        self.dispatcher = SimpleNamespace(tool_to_section={})
        self.markdown = markdown
        self.provenance = provenance or []

    def run_scoped(self, *args, **kwargs):
        return [{"role": "assistant", "content": self.markdown}], list(self.provenance), []


@pytest.fixture(autouse=True)
def clean_case_store():
    yield
    for case_id in (
        "CASE-MD-INTAKE",
        "CASE-MD-SIMILAR",
        "CASE-MD-RISK",
        "CASE-MD-PLAN",
        "CASE-MD-REPORT",
        "CASE-MD-COPILOT",
    ):
        server.CASE_STORE.evict(case_id)


def assert_raw_markdown(value: object, expected: str = MARKDOWN) -> None:
    assert isinstance(value, str)
    assert value == expected
    assert "<style" not in value.lower()
    assert "bsi-content" not in value


def test_intake_cache_returns_raw_markdown_agent_summary():
    case_data = {"provenance_trail": [], "rules_fired": []}

    with mock.patch.object(
        server,
        "get_cached_route_summary",
        return_value=(case_data, MARKDOWN),
    ), mock.patch.object(server, "log_agent_call"):
        response = server.intake(server.intakeRequest(case_id="CASE-MD-INTAKE"))

    assert_raw_markdown(response["details"]["agent_summary"])


def test_similar_cases_cache_returns_raw_markdown_agent_summary():
    case_data = {
        server.AGENT_SUMMARY_CACHE_KEY: {"similar_cases": MARKDOWN},
        "similar_cases": {"matches": []},
        "provenance_trail": [],
    }

    with mock.patch.object(
        server,
        "_resolve_case_store",
        return_value=(case_data, "mock"),
    ), mock.patch.object(server, "log_agent_call"):
        response = server.similar_cases(server.SimilarCasesRequest(case_id="CASE-MD-SIMILAR"))

    assert_raw_markdown(response["details"]["agent_summary"])


def test_risk_assessment_fresh_returns_raw_markdown_agent_summary_not_tuple():
    case_id = "CASE-MD-RISK"
    server.CASE_STORE[case_id] = {}
    risk_payload = {"risk_score": 25, "risk_tier": "Medium", "neo4j_signals": {}}

    with mock.patch.object(
        server,
        "_resolve_case_store",
        return_value=({}, "mock"),
    ), mock.patch.object(
        server,
        "_get_runner",
        return_value=FakeRunner(),
    ), mock.patch.object(
        server,
        "run_risk_assessment_pipeline",
        return_value=(risk_payload, {"risk_assessment": risk_payload}, []),
    ), mock.patch.object(server, "persist_case_session"), mock.patch.object(server, "log_agent_call"):
        response = server.risk_assessment(server.RiskAssessmentRequest(case_id=case_id))

    assert_raw_markdown(response["details"]["agent_summary"])


def test_plan_cache_with_override_returns_spliced_raw_markdown_agent_summary():
    case_id = "CASE-MD-PLAN"
    cached_summary = (
        "## Investigation Steps\n"
        "- **Step 1:** Old step\n\n"
        "## Evidence Checklist\n"
        "- Existing evidence item"
    )
    case_data = {
        server.AGENT_SUMMARY_CACHE_KEY: {"plan": cached_summary},
        "investigation_plan": {"investigation_steps": [{"step": 1, "action": "Old step"}]},
        "rules_fired": [],
        "provenance_trail": [],
    }
    override = {
        "modified_steps": [{"step": 1, "action": "Review bank records"}],
        "modified_by": "analyst",
        "modified_on": datetime(2026, 7, 31, tzinfo=timezone.utc),
    }

    with mock.patch.object(
        server,
        "_resolve_case_store",
        return_value=(case_data, "mock"),
    ), mock.patch.object(
        server,
        "get_case_ai_summary_cache_updated_at",
        return_value=None,
    ), mock.patch.object(
        server,
        "get_override",
        return_value=override,
    ), mock.patch.object(server, "log_agent_call"):
        response = server.plan(server.PlanRequest(case_id=case_id))

    summary = response["details"]["agent_summary"]
    assert isinstance(summary, str)
    assert "Review bank records" in summary
    assert "Old step" not in summary
    assert "<style" not in summary.lower()
    assert "bsi-content" not in summary


def test_generate_report_fresh_returns_raw_markdown_agent_summary():
    case_id = "CASE-MD-REPORT"
    case_data = {
        "complaint_intelligence": {"subject_primary_id": "SUBJ-1"},
        "provenance_trail": [],
        "rules_fired": [],
    }
    related = {
        "related_network": [],
        "confidence_summary": {"high": 0, "medium": 0, "unresolved": 0},
        "rejected_count": 0,
    }
    related_envelope = {
        "result": related,
        "provenance": {"sources": ["Neo4j graph query"], "retrieved_at": "", "computed_by": "test"},
    }
    decision_log_envelope = {
        "result": {"decision_log_markdown": "- None", "decision_log": []},
        "provenance": {"sources": ["Decision log"], "retrieved_at": "", "computed_by": "test"},
    }

    with mock.patch.object(
        server,
        "_resolve_case_store",
        return_value=(case_data, "mock"),
    ), mock.patch.object(
        server,
        "_get_runner",
        return_value=FakeRunner(),
    ), mock.patch.object(
        server,
        "assemble_related_network",
        return_value=related_envelope,
    ), mock.patch.object(
        server,
        "get_override",
        return_value=None,
    ), mock.patch.object(
        server,
        "build_decision_log",
        return_value=decision_log_envelope,
    ), mock.patch.object(
        server,
        "build_report_llm_context",
        return_value={},
    ), mock.patch.object(server, "save_report", return_value={"id": 1}), mock.patch.object(
        server, "log_agent_call"
    ):
        response = server.generate_report(
            server.ReportGenerationRequest(case_id=case_id, reload_ai_summary=True)
        )

    assert_raw_markdown(response["details"]["agent_summary"])


def test_copilot_returns_raw_markdown_answer_and_keeps_structured_sources():
    case_id = "CASE-MD-COPILOT"
    case_data = {
        "provenance_trail": [
            {"sources": ["Case store"], "retrieved_at": "2026-07-31T00:00:00Z", "computed_by": "test"}
        ]
    }

    with mock.patch.object(
        server,
        "_resolve_case_store",
        return_value=(case_data, "mock"),
    ), mock.patch.object(
        server,
        "get_case_ai_summary_cache_updated_at",
        return_value=None,
    ), mock.patch.object(
        server,
        "get_override",
        return_value=None,
    ), mock.patch.object(
        server,
        "resolve_copilot_history",
        return_value=([], "mock"),
    ), mock.patch.object(
        server,
        "_get_runner",
        return_value=FakeRunner(),
    ), mock.patch.object(server, "store_copilot_turn"), mock.patch.object(server, "log_agent_call"):
        response = server.copilot(server.CopilotRequest(case_id=case_id, question="What happened?"))

    assert_raw_markdown(response["answer"])
    assert response["sources_cited_details"] == [
        {"sources": ["Case store"], "retrieved_at": "2026-07-31T00:00:00Z", "computed_by": "test"}
    ]


def test_generate_report_pdf_still_returns_pdf_response():
    source = inspect.getsource(server.generate_report_pdf)

    assert "render_report_pdf(" in source
    assert 'media_type="application/pdf"' in source
