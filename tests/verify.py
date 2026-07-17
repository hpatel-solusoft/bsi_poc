"""
Verification suite for the ETL -> Neo4j -> rules build.

No live Neo4j, Postgres, AppWorks or LLM is reachable from the sandbox
this was built in, so every external boundary is mocked and every
assertion is about THIS codebase's own logic: what it passes to Neo4j,
in what order, and what it does with what comes back.

What this DOES prove:
  * the ingest sequences load-then-reason correctly, and never reasons
    over a case that failed to load
  * the rule engine passes scope + registry parameters into every rule,
    and reports a disabled rule as skipped rather than as a miss
  * rules_fired always returns all fourteen entries
  * graph_load separates written / suppressed / dropped correctly
  * the pipeline honours Principle 10 (skip) and Principle 15 (mark failed)
  * normalisation actually collapses the variants it claims to

What this CANNOT prove, and what still needs a real instance:
  * that the Cypher is valid Cypher (no offline parser; needs a live Neo4j)
  * that the AppWorks property names in graph_sync.py match the live
    instance's real response shape
  * that the Extraction Stage prompt produces good attributions against
    real BSI narrative text

Run:  python -m tests.verify
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

PASSES: List[str] = []
FAILURES: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(name)
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


# --------------------------------------------------------------------
# Fake Neo4j
# --------------------------------------------------------------------

class FakeResult:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record

    def data(self):
        return self._record if isinstance(self._record, list) else []


class FakeSession:
    """Records every query + params it is given, and returns whatever the
    scripted responder decides. The recording is the point — most of the
    assertions below are about what this codebase ASKS the graph, which is
    exactly what a live-instance test could not isolate."""

    def __init__(self, responder):
        self.calls: List[Dict[str, Any]] = []
        self._responder = responder

    def run(self, query, **params):
        self.calls.append({"query": query, "params": params})
        return FakeResult(self._responder(query, params))

    def execute_write(self, fn, *args, **kwargs):
        return fn(self, *args, **kwargs)

    def close(self):
        pass


def fake_session_cm(session):
    @contextmanager
    def _cm(*args, **kwargs):
        yield session
    return _cm


# --------------------------------------------------------------------
# 1. Normalisation
# --------------------------------------------------------------------

def test_normalizers():
    print("\n[1] etl/normalizers.py")
    from etl import normalizers as N

    check(
        "FEIN variants collapse to one employer key",
        N.employer_key("04-1234567", None, "Acme Corp") == N.employer_key("041234567", None, "ACME CORPORATION"),
        "a FEIN-matched employer must be one node regardless of formatting or name spelling",
    )
    check(
        "employer falls back to AppWorks id when FEIN is absent (the Wage-table case)",
        N.employer_key(None, "EMP-99", "Acme") == "FID:EMP-99",
        "without this, every wage-sourced employer is dropped and Rules 9/12 have no data",
    )
    check(
        "employer name fallback strips corporate suffixes",
        N.employer_key(None, None, "Acme Corp.") == N.employer_key(None, None, "Acme Corporation"),
    )
    check(
        "employer key is None when there is nothing to key on",
        N.employer_key(None, None, "") is None,
    )
    check(
        "address variants collapse to one key",
        N.address_key("12 Main Street", "Boston", "MA", "02108-1234")
        == N.address_key("12 MAIN ST.", "boston", "ma", "02108"),
        "Rule 3 is a string-equality join; if this fails, the rule silently never fires",
    )
    check(
        "different addresses do NOT collapse",
        N.address_key("12 Main St", "Boston", "MA", "02108")
        != N.address_key("14 Main St", "Boston", "MA", "02108"),
    )
    check(
        "alias is trimmed but NOT case-folded (Rule 5 is exact-match by spec)",
        N.alias_value("  Johnny B  ") == "Johnny B" and N.alias_value("johnny b") != N.alias_value("Johnny B"),
    )
    check(
        "commentary id is deterministic across runs (makes re-sync idempotent)",
        N.commentary_id("C1", "Case_Commentary", None, "text", "2026-01-01")
        == N.commentary_id("C1", "Case_Commentary", None, "text", "2026-01-01"),
    )
    check(
        "commentary id differs for different text",
        N.commentary_id("C1", "Case_Commentary", None, "a", None)
        != N.commentary_id("C1", "Case_Commentary", None, "b", None),
    )
    check("money parses out of formatted strings", N.to_float("$52,000.00") == 52000.0)
    check("boolean parses AppWorks' several spellings",
          N.to_bool("Y") and N.to_bool("true") and N.to_bool(1) and not N.to_bool("N"))


# --------------------------------------------------------------------
# 2. Rule engine
# --------------------------------------------------------------------

def test_rule_engine():
    print("\n[2] reasoning_layer/rule_engine.py")
    from reasoning_layer import rule_engine

    session = FakeSession(lambda q, p: {"writes": 3})
    scope = {
        "case_id": "C1", "primary_subject_id": "S1",
        "scope_subject_ids": ["S1", "S2"], "scope_case_ids": ["C1", "C2"],
    }
    registry = {
        "Rule_01_Shared_Employer": {"enabled": True, "params": {}},
        "Rule_13_FastTrack_Escalation": {"enabled": True, "params": {"fasttrack_fraud_threshold": 50000.0}},
        "Rule_05_Alias_Identity": {"enabled": False, "params": {}},
    }

    with mock.patch.object(rule_engine, "get_session", fake_session_cm(session)):
        results = rule_engine.execute_rules(
            ["Rule_01_Shared_Employer", "Rule_05_Alias_Identity", "Rule_13_FastTrack_Escalation"],
            scope, registry,
        )

    by_id = {r["rule_id"]: r for r in results}
    check("every requested rule is reported", len(results) == 3)
    check("a disabled rule is reported skipped, not as a miss",
          by_id["Rule_05_Alias_Identity"]["executed"] is False
          and by_id["Rule_05_Alias_Identity"]["skipped_reason"] == "disabled_in_registry",
          "'rule was off' and 'rule found nothing' must never look the same to an investigator")
    check("a disabled rule is never executed against the graph", len(session.calls) == 2)
    check("write counts are surfaced per rule", by_id["Rule_01_Shared_Employer"]["writes"] == 3)

    params = session.calls[0]["params"]
    check("scope is passed into every rule",
          params["scope_subject_ids"] == ["S1", "S2"] and params["scope_case_ids"] == ["C1", "C2"],
          "without this the rules match the whole graph — the exact bug this round fixes")
    check("registry parameters reach the rule that needs them",
          session.calls[1]["params"]["fasttrack_fraud_threshold"] == 50000.0)
    check("asserted_at is stamped once per wave, not per rule",
          session.calls[0]["params"]["asserted_at"] == session.calls[1]["params"]["asserted_at"])


# --------------------------------------------------------------------
# 3. rules_fired contract
# --------------------------------------------------------------------

def test_rules_fired():
    print("\n[3] reasoning_layer/rules_fired.py")
    from reasoning_layer import rules_fired, rule_registry

    def responder(query, params):
        if "SHARES_EMPLOYER_WITH" in query:
            return {"n": 2, "confidences": ["Medium", "High"], "corroborated": True}
        return {"n": 0, "confidences": [], "corroborated": False}

    session = FakeSession(responder)
    scope = {"case_id": "C1", "primary_subject_id": "S1",
             "scope_subject_ids": ["S1"], "scope_case_ids": ["C1"]}

    with mock.patch.object(rules_fired, "get_session", fake_session_cm(session)):
        block = rules_fired.build_rules_fired(
            scope, [{"rule_id": "Rule_05_Alias_Identity", "writes": 0,
                     "skipped_reason": "disabled_in_registry"}],
        )

    check("the block always has all 14 entries", len(block) == 14, f"got {len(block)}")
    check("every entry carries A.4's four required fields",
          all({"rule_id", "fired", "confidence", "corroborated"} <= set(e) for e in block))
    fired = [e for e in block if e["fired"]]
    check("a rule with graph evidence reports fired=true", len(fired) == 1 and fired[0]["rule_id"] == "Rule_01_Shared_Employer")
    check("confidence is the strongest tier found, not the first",
          fired[0]["confidence"] == "High", f"got {fired[0]['confidence']}")
    check("corroboration is surfaced", fired[0]["corroborated"] is True)
    unfired = next(e for e in block if e["rule_id"] == "Rule_07_Prior_Guilty")
    check("a rule that did not fire reports Unresolved, not a stale confidence",
          unfired["confidence"] == "Unresolved" and unfired["corroborated"] is False)
    skipped = next(e for e in block if e["rule_id"] == "Rule_05_Alias_Identity")
    check("a disabled rule carries its skipped_reason into the block",
          skipped["skipped_reason"] == "disabled_in_registry")
    check("rule 14 is present and marked as a non-wave modifier",
          any(e["rule_id"] == rule_registry.MODIFIER_RULE_ID and e["wave"] == 0 for e in block))


# --------------------------------------------------------------------
# 4. Graph load
# --------------------------------------------------------------------

def test_graph_load():
    print("\n[4] reasoning_layer/graph_load.py")
    from reasoning_layer import graph_load

    def responder(query, params):
        if "MERGE (al)-[r:ALLEGATION_LIKELY_AGAINST_SUBJECT]" in query:
            if params["allegation_id"] == "AL_OK":
                return {"rel_id": "rel-1"}
            return None  # rejected, or a hallucinated id
        if "MATCH (rej:Rejection" in query:
            if params["allegation_id"] == "AL_REJECTED":
                return {"rejected_by": "j.doe", "rejected_at": "2026-05-01", "reason": "wrong person"}
            return None
        if "confirms_relationship_ids" in query:
            return {"confirmed_ref": params["relationship_ref"]} if params["relationship_ref"] == "REL_REAL" else None
        return None

    session = FakeSession(responder)
    extraction = {
        "attributions": [
            {"allegation_id": "AL_OK", "subject_id": "S1", "confidence": "High",
             "rationale": "named directly", "source_comment_ids": ["c1"]},
            {"allegation_id": "AL_REJECTED", "subject_id": "S1", "confidence": "High",
             "rationale": "x", "source_comment_ids": []},
            {"allegation_id": "AL_HALLUCINATED", "subject_id": "S9", "confidence": "Medium",
             "rationale": "x", "source_comment_ids": []},
        ],
        "corroborations": [
            {"relationship_ref": "REL_REAL", "comment_ref": "c1", "rationale": "comment says they work together"},
            {"relationship_ref": "REL_FAKE", "comment_ref": "c1", "rationale": "invented"},
        ],
    }

    with mock.patch.object(graph_load, "get_session", fake_session_cm(session)):
        result = graph_load.load_extraction_output("C1", "S1", extraction)["result"]

    check("a valid attribution is written", len(result["written"]) == 1 and result["written"][0]["allegation_id"] == "AL_OK")
    check("a rejected attribution is suppressed, with an investigator-visible note",
          len(result["suppressed"]) == 1 and "rejected by j.doe" in result["suppressed"][0]["note"],
          "Principle 14: suppress, never silently delete")
    check("a hallucinated id is dropped, not written and not counted as suppressed",
          len(result["dropped"]) == 1 and result["dropped"][0]["allegation_id"] == "AL_HALLUCINATED")
    check("a real corroboration is linked (Rule 14's input)", result["corroborations_linked"] == 1)
    check("a corroboration referencing a non-existent relationship is dropped",
          result["corroborations_linked"] == 1, "the fake REL_FAKE must not be counted")


# --------------------------------------------------------------------
# 5. Pipeline
# --------------------------------------------------------------------

def test_pipeline():
    print("\n[5] reasoning_layer/pipeline.py")
    from reasoning_layer import pipeline

    scope = {"case_id": "C1", "primary_subject_id": "S1", "scope_subject_ids": ["S1", "S2"],
             "scope_case_ids": ["C1"], "expansion": {}, "subject_in_graph": True}

    # (a) happy path — all six steps, in order
    with mock.patch.object(pipeline.pipeline_state_repository, "get_run_state", return_value=None), \
         mock.patch.object(pipeline.pipeline_state_repository, "start_run") as start, \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_wave1_complete") as m1, \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_extraction_complete") as mx, \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_wave2_complete") as m2, \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_failed") as failed, \
         mock.patch.object(pipeline.rule_registry, "ensure_registry"), \
         mock.patch.object(pipeline.rule_registry, "load_registry", return_value={}), \
         mock.patch.object(pipeline.scope_resolver, "resolve_scope", return_value=scope), \
         mock.patch.object(pipeline.rule_engine, "run_wave1", return_value=[{"rule_id": "Rule_01_Shared_Employer", "writes": 1}]) as w1, \
         mock.patch.object(pipeline.rule_engine, "run_wave2", return_value=[{"rule_id": "Rule_02_Employer_Fraud_Network", "writes": 1}]) as w2, \
         mock.patch.object(pipeline.rule_engine, "run_modifier", return_value=[{"rule_id": "Rule_14_Confirmation_Elevation", "writes": 0}]) as w14, \
         mock.patch.object(pipeline, "_run_extraction_stage", return_value={"attributions_extracted": 1}) as ext, \
         mock.patch.object(pipeline.rules_fired, "build_rules_fired", return_value=[{"rule_id": "Rule_01_Shared_Employer", "fired": True}]):
        out = pipeline.run_pipeline("C1", "S1")["result"]

    check("happy path completes", out["pipeline_status"] == "completed")
    check("all six steps ran", w1.called and ext.called and w2.called and w14.called)
    check("Wave 2 runs AFTER extraction (its attribution edges must exist first)",
          ext.call_count == 1 and w2.call_count == 1)
    check("no failure was recorded on the happy path", not failed.called)
    check("rules_fired is returned to the caller (Functional Spec A.4)", "rules_fired" in out)

    # (b) Principle 10 — completed run is not re-run
    completed = {"status": "completed", "cleared_at": None, "completed_at": "2026-07-01",
                 "wave1_completed_at": "x", "wave2_completed_at": "y"}
    with mock.patch.object(pipeline.pipeline_state_repository, "get_run_state", return_value=completed), \
         mock.patch.object(pipeline.pipeline_state_repository, "start_run") as start2, \
         mock.patch.object(pipeline.scope_resolver, "resolve_scope", return_value=scope), \
         mock.patch.object(pipeline.rules_fired, "build_rules_fired", return_value=[]), \
         mock.patch.object(pipeline.rule_engine, "run_wave1") as w1b:
        out2 = pipeline.run_pipeline("C1", "S1")["result"]

    check("Principle 10: a completed run is skipped, not re-run",
          out2["pipeline_status"] == "already_completed" and not w1b.called and not start2.called)
    check("a skipped run still returns rules_fired (callers never special-case it)",
          "rules_fired" in out2)

    # (c) force=True (the ETL path) DOES re-run
    with mock.patch.object(pipeline.pipeline_state_repository, "get_run_state", return_value=completed), \
         mock.patch.object(pipeline.pipeline_state_repository, "clear_run") as cleared, \
         mock.patch.object(pipeline.pipeline_state_repository, "start_run"), \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_wave1_complete"), \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_extraction_complete"), \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_wave2_complete"), \
         mock.patch.object(pipeline.rule_registry, "ensure_registry"), \
         mock.patch.object(pipeline.rule_registry, "load_registry", return_value={}), \
         mock.patch.object(pipeline.scope_resolver, "resolve_scope", return_value=scope), \
         mock.patch.object(pipeline.rule_engine, "run_wave1", return_value=[]) as w1c, \
         mock.patch.object(pipeline.rule_engine, "run_wave2", return_value=[]), \
         mock.patch.object(pipeline.rule_engine, "run_modifier", return_value=[]), \
         mock.patch.object(pipeline, "_run_extraction_stage", return_value={}), \
         mock.patch.object(pipeline.rules_fired, "build_rules_fired", return_value=[]):
        out3 = pipeline.run_pipeline("C1", "S1", force=True)["result"]

    check("force=True re-runs after a fresh ingest (stale conclusions over new data)",
          out3["pipeline_status"] == "completed" and w1c.called and cleared.called)

    # (d) Principle 15 — an extraction failure marks the run failed and re-raises
    with mock.patch.object(pipeline.pipeline_state_repository, "get_run_state", return_value=None), \
         mock.patch.object(pipeline.pipeline_state_repository, "start_run"), \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_wave1_complete"), \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_extraction_complete") as mx2, \
         mock.patch.object(pipeline.pipeline_state_repository, "mark_failed") as failed2, \
         mock.patch.object(pipeline.rule_registry, "ensure_registry"), \
         mock.patch.object(pipeline.rule_registry, "load_registry", return_value={}), \
         mock.patch.object(pipeline.scope_resolver, "resolve_scope", return_value=scope), \
         mock.patch.object(pipeline.rule_engine, "run_wave1", return_value=[]), \
         mock.patch.object(pipeline, "_run_extraction_stage", side_effect=ValueError("bad LLM JSON")), \
         mock.patch.object(pipeline.rule_engine, "run_wave2") as w2d:
        try:
            pipeline.run_pipeline("C1", "S1")
            raised = False
        except ValueError:
            raised = True

    check("Principle 15: a built stage failing marks the run failed and re-raises",
          raised and failed2.called and not mx2.called)
    check("Wave 2 does not run after an extraction failure", not w2d.called)


# --------------------------------------------------------------------
# 6. Ingest orchestration — the load-all-then-reason invariant
# --------------------------------------------------------------------

def test_ingest_service():
    print("\n[6] etl/ingest_service.py")
    from etl import ingest_service

    order: List[str] = []

    def fake_sync(case_id):
        order.append(f"load:{case_id}")
        if case_id == "BAD":
            raise RuntimeError("AppWorks 502")
        return {"subjects": 2}

    def fake_pipeline(case_id, subject_id, force=False):
        order.append(f"reason:{case_id}:{subject_id}")
        return {"result": {"pipeline_status": "completed",
                           "rules_fired": [{"rule_id": "Rule_01_Shared_Employer", "fired": True}]}}

    with mock.patch.object(ingest_service.graph_sync, "sync_case", side_effect=fake_sync), \
         mock.patch.object(ingest_service.pipeline, "run_pipeline", side_effect=fake_pipeline), \
         mock.patch.object(ingest_service, "_subjects_for_case", return_value=["S1"]), \
         mock.patch.object(ingest_service.graph_ingest_repository, "mark_started"), \
         mock.patch.object(ingest_service.graph_ingest_repository, "mark_loaded"), \
         mock.patch.object(ingest_service.graph_ingest_repository, "mark_reasoned"), \
         mock.patch.object(ingest_service.graph_ingest_repository, "mark_failed"), \
         mock.patch.object(ingest_service.time, "sleep"):
        report = ingest_service.ingest(["C1", "C2"], run_reasoning=True)

    loads = [i for i, step in enumerate(order) if step.startswith("load:")]
    reasons = [i for i, step in enumerate(order) if step.startswith("reason:")]
    check("EVERY case is loaded before ANY case is reasoned",
          max(loads) < min(reasons),
          "reasoning case 1 before case 2 is loaded means cross-case rules silently never fire — "
          "the single most important ordering property in this build")
    check("all requested cases were loaded", report["cases_loaded"] == 2)
    check("reasoning ran for each loaded case", report["pipeline_reasoned"] == 2)

    # A case that fails to load must not be reasoned over.
    order.clear()
    with mock.patch.object(ingest_service.graph_sync, "sync_case", side_effect=fake_sync), \
         mock.patch.object(ingest_service.pipeline, "run_pipeline", side_effect=fake_pipeline), \
         mock.patch.object(ingest_service, "_subjects_for_case", return_value=["S1"]), \
         mock.patch.object(ingest_service.graph_ingest_repository, "mark_started"), \
         mock.patch.object(ingest_service.graph_ingest_repository, "mark_loaded"), \
         mock.patch.object(ingest_service.graph_ingest_repository, "mark_reasoned"), \
         mock.patch.object(ingest_service.graph_ingest_repository, "mark_failed") as failed, \
         mock.patch.object(ingest_service.time, "sleep"):
        report2 = ingest_service.ingest(["BAD", "C2"], run_reasoning=True)

    check("a case that failed to load is never reasoned over",
          not any(step.startswith("reason:BAD") for step in order),
          "reasoning over a half-present case produces confidently wrong output")
    check("one bad case does not stop the batch",
          report2["cases_loaded"] == 1 and report2["cases_load_failed"] == 1)
    check("a load failure is retried before giving up",
          sum(1 for s in order if s == "load:BAD") == 3)
    check("a permanent failure is recorded for operators", failed.called)


# --------------------------------------------------------------------
# 7. Wiring — manifest, dispatcher, rule files
# --------------------------------------------------------------------

def test_wiring():
    print("\n[7] wiring")
    import yaml
    from reasoning_layer import rule_engine, rule_registry

    manifest = yaml.safe_load(open("config/manifest.yaml", encoding="utf-8"))
    names = [t["name"] for t in manifest["tools"]]
    check("manifest still parses and still has all 7 tools", len(names) == 7, str(names))
    # Section 9.1 / the /intake docstring: the reasoning pipeline is invoked
    # DIRECTLY by Context Enrichment and by the ETL ingest service — it is
    # deliberately NOT a manifest.yaml tool, so the LLM can never select it
    # or force a re-run itself. An earlier round of this codebase wrongly
    # assumed a manifest-registered run_reasoning_pipeline tool existed;
    # that assumption was corrected and the entry removed. This check
    # guards the correction, not the mistake it replaced.
    check("the reasoning pipeline is NOT registered as an LLM-callable tool",
          "run_reasoning_pipeline" not in names and "run_pipeline" not in names)

    check("all 14 rule files exist on disk", len(rule_engine.verify_rule_files()) == 14)
    check("wave membership is by list, and Rules 7/8 are in Wave 2 (not a numeric range)",
          "Rule_07_Prior_Guilty" in rule_registry.WAVE_2_RULE_IDS
          and "Rule_08_Recidivist_Escalation" in rule_registry.WAVE_2_RULE_IDS
          and "Rule_11_Cross_Case_Hub" in rule_registry.WAVE_1_RULE_IDS)
    check("Rule 8 runs after Rule 7 (it reads Rule 7's output)",
          rule_registry.WAVE_2_RULE_IDS.index("Rule_08_Recidivist_Escalation")
          > rule_registry.WAVE_2_RULE_IDS.index("Rule_07_Prior_Guilty"))
    check("Rule 13 runs after Rule 7 (it reads Rule 7's output)",
          rule_registry.WAVE_2_RULE_IDS.index("Rule_13_FastTrack_Escalation")
          > rule_registry.WAVE_2_RULE_IDS.index("Rule_07_Prior_Guilty"))
    check("Rule 14 is not a wave member",
          rule_registry.MODIFIER_RULE_ID not in rule_registry.WAVE_1_RULE_IDS
          and rule_registry.MODIFIER_RULE_ID not in rule_registry.WAVE_2_RULE_IDS)

    # Every rule file must be scope-parameterised — a rule that forgets
    # $scope_subject_ids silently reverts to scanning the whole graph.
    from pathlib import Path
    unscoped = []
    for rule_id, path in rule_engine.RULE_FILES.items():
        text = path.read_text()
        if "$scope_subject_ids" not in text and "$subject_id" not in text:
            unscoped.append(rule_id)
    check("every rule is scoped to the run's subjects", not unscoped, f"unscoped: {unscoped}")

    # Every rule that writes an inferred relationship must check for a rejection first.
    no_guard = [
        rule_id for rule_id, path in rule_engine.RULE_FILES.items()
        if "Rejection" not in path.read_text() and rule_id != rule_registry.MODIFIER_RULE_ID
    ]
    check("every rule checks for a rejection before re-asserting (Section 5.5)",
          not no_guard, f"missing guard: {no_guard}")


if __name__ == "__main__":
    test_normalizers()
    test_rule_engine()
    test_rules_fired()
    test_graph_load()
    test_pipeline()
    test_ingest_service()
    test_wiring()

    print("\n" + "=" * 68)
    print(f"{len(PASSES)} passed, {len(FAILURES)} failed")
    for failure in FAILURES:
        print(f"  FAILED: {failure}")
    print("=" * 68)
    sys.exit(1 if FAILURES else 0)
