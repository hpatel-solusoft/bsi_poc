"""
Service layer for POST /plan (ON-DEMAND — Plan Route, Step 4 in flow).

Owns: the entire /plan business logic — prerequisite resolution
(auto-running /risk_assessment when needed), the AI-35 per-tab
generated_at read, staleness/override checks, the LLM agent call, the
plan pipeline (parsing + human-override application), persistence, and
response shaping.

Does NOT own: HTTP routing itself (api/server.py just calls run_plan(req)
and returns/raises whatever it does), or the underlying pipeline/
staleness primitives (api/pipeline_execution.py, core/narrative_staleness.py,
core/investigation_plan_override_repository.py).

`_resolve_case_store` and `risk_assessment` are deliberately imported
LATE (inside run_plan, not at module level) rather than at the top of
this file: both still live in api/server.py (neither has been extracted
to a service module — risk_assessment's own extraction is a separate,
future refactor), and api/server.py imports THIS module at its own
top level to wire up the /plan route. A top-level `from api.server
import ...` here would therefore be a circular import at module load
time. Deferring the import into the function body sidesteps the cycle
(both modules are fully loaded by the time run_plan actually executes)
and, as a bonus, means existing tests that patch
`api.server._resolve_case_store` / `api.server.risk_assessment` before
calling `server.plan(...)` keep working unmodified — the deferred
import re-resolves those names from api.server's current state at call
time, after any patch has already been applied.

Extracted verbatim from api/server.py's `/plan` route body during the
service-layer refactor — same behavior, same log lines, same response
shape; only the module boundary changed. Tests that used to patch
api.server.<name> for this route's OWN internals (as opposed to the two
server.py-native names above) now patch
api.services.plan_service.<name> instead, since that's where the call
sites actually live now.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import HTTPException

# See api/server.py's import of the same symbol for why this is a
# deliberate, narrow exception to "no file in api/ imports directly from
# appworks/" — an exception TYPE only, for HTTP error-boundary translation.
from appworks.appworks_auth import AppworksSessionExpiredError

from agent_service.prompt_builders import build_plan_prompt
from agent_service.runner_provider import get_runner
from api.message_utils import build_ai_summary, extract_tool_results
from api.models import PlanRequest, RiskAssessmentRequest
from api.pipeline_execution import (
    evaluate_cache_staleness,
    fetch_live_graph_findings,
    prepare_plan_context,
    resolve_prerequisite_case_data,
    run_plan_pipeline,
)
from api.response_builders import (
    apply_step_override_to_summary,
    fired_rules_only,
    format_provenance_lines,
    resolve_plan_agent_summary,
)
from core.agent_audit_repository import log_agent_call
from core.case_store import (
    AGENT_SUMMARY_CACHE_KEY,
    CASE_STORE,
    get_complaint_number,
    get_route_generated_at,
    get_route_generated_at_datetime,
    get_route_summary_text,
    get_route_username,
    merge_agent_summary_cache,
    persist_case_session,
)
from core.investigation_plan_override_repository import compute_plan_staleness, get_override
from reasoning_layer.investigation_tasks import build_rule_aware_tasks

logger = logging.getLogger(__name__)


def run_plan(req: PlanRequest, username: str, token: str) -> Dict[str, Any]:
    """
    ON-DEMAND — Plan Route (Step 4 in flow).
    Calls get_investigation_plan only.
    Requires risk_tier from prior /risk_assessment run (via CS-4 or ai_summary body).
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    Flow: /intake → /similar_cases → /risk_assessment → /plan → /copilot |
    """
    # Deferred — see module docstring for why.
    from api.server import _resolve_case_store, risk_assessment

    start = time.time()
    try:

        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        # AI-33: an investigator can open Plan without ever having opened
        # Risk Assessment. Auto-run /risk_assessment's own logic internally
        # the moment risk_assessment (that route's output) isn't already
        # resolvable, instead of the hard 400 this used to raise. Chains
        # backward one hop at a time: /risk_assessment's own entry check
        # (similar_cases) fires /similar_cases if that is missing too, and
        # /similar_cases' own entry check fires /intake if THAT is
        # missing — this route only needs to know about the one route
        # immediately before it.
        case_data, data_source = resolve_prerequisite_case_data(
            req.case_id,
            req.ai_summary,
            required_field="risk_assessment",
            run_prerequisite_route=lambda: risk_assessment(
                RiskAssessmentRequest(case_id=req.case_id), username=username, token=token
            ),
            resolve_case_store=_resolve_case_store,
            route_name="plan",
        )
        logger.info("case_id=%s data_source=%s", req.case_id, data_source)

        # AI-35: /plan's OWN per-tab save-time (AI-34), read from the
        # already-resolved case_data BEFORE this route's own
        # merge_agent_summary_cache/persist_case_session calls below
        # overwrite this exact "plan" cache entry — reading it late would
        # make every override look stale (Section E.5), and would make
        # the AI-32 graph check below compare against the run that is
        # about to happen instead of the one actually cached.
        #
        # This replaces the old case-wide case_ai_summary_store.updated_at
        # column (shared by every tab — /intake, /similar_cases,
        # /risk_assessment, /plan all touch it) for BOTH of /plan's
        # staleness checks: the AI-32 graph check immediately below, and
        # compute_plan_staleness's manual-edit check further down. Reading
        # the shared column here made /plan look "fresh" whenever ANY
        # other tab refreshed, even though nothing about /plan's own
        # narrative or override had changed — after this ticket, nothing
        # in /plan reads that shared column anymore.
        plan_generated_at_before_call = get_route_generated_at_datetime(case_data, "plan")

        # AI-32: the two independent staleness triggers, combined — see
        # core.narrative_staleness's module docstring. Reuses the SAME
        # plan_generated_at_before_call read just above (rather than a
        # second lookup) so plan_stale's own comparison and this one are
        # guaranteed to agree on which snapshot of /plan's own
        # generated_at they judged staleness against.
        # Like /similar_cases and /risk_assessment, /plan has no separate
        # "pipeline" stage of its own (rule_aware_tasks/rules_fired are
        # already a live, always-fresh Neo4j read regardless of cache
        # state — see fetch_live_rule_aware_tasks/prepare_plan_context).
        # staleness.stale reports the graph trigger only to the caller
        # (see StalenessCheck.stale's docstring for why core_data is
        # excluded); should_auto_refresh (the gate below) only fires on an
        # explicit reload_ai_summary=True — a graph-only change no longer
        # forces this route to regenerate its narrative on its own (see
        # StalenessCheck.should_auto_refresh's docstring).
        staleness = evaluate_cache_staleness(
            req.case_id, req.reload_ai_summary, cache_generated_at=plan_generated_at_before_call
        )

        # Agent-summary cache: if /plan has already produced and persisted
        # an agent_summary for this case_id, answer from it WITHOUT calling
        # the LLM again.
        #
        # The investigation_plan_override (Section D.6) is checked FIRST,
        # regardless of staleness: once an investigator has modified the
        # plan, that modification is authoritative, so a cache hit is
        # served even when a refresh was otherwise indicated — a fresh LLM
        # run would only be discarded in favour of override["modified_steps"]
        # anyway (see run_plan_pipeline below), so skipping straight to it
        # here saves the wasted LLM call. Only an explicit
        # reload_ai_summary=True bypasses the cache when NO override
        # exists; it still falls through to a fresh agent run below in
        # that case, same as before.
        override = get_override(req.case_id)
        if not staleness.should_auto_refresh or override is not None:
            cached_summary = get_route_summary_text(case_data, "plan")
            cached_plan = case_data.get("investigation_plan")
            if cached_summary is not None and isinstance(cached_plan, dict):
                if override is not None:
                    cached_plan = {**cached_plan, "investigation_steps": override["modified_steps"]}
                    plan_source, modified_by, modified_on = (
                        "User Modified",
                        override["modified_by"],
                        override["modified_on"],
                    )
                    plan_stale = compute_plan_staleness(plan_generated_at_before_call, modified_on)
                    # The structured investigation_plan above now carries the
                    # override, but cached_summary is still the pre-override
                    # LLM markdown pulled straight from case_ai_summary_store —
                    # without this, the investigator reads AI-generated steps
                    # while graph_findings/meta already say User Modified.
                    cached_summary = apply_step_override_to_summary(
                        cached_summary,
                        override["modified_steps"],
                    )
                else:
                    plan_source, modified_by, modified_on, plan_stale = "AI Summerized", None, None, False

                duration_seconds = round(time.time() - start, 1)
                logger.info(
                    "plan CACHE HIT for case_id=%s — answering from "
                    "case_ai_summary_store, no LLM call made",
                    req.case_id,
                )
                log_agent_call(
                    case_id=req.case_id,
                    agent_name="investigation_plan",
                    endpoint="/plan",
                    latency_ms=int(duration_seconds * 1000),
                    status="success",
                    username=username,
                )
                # rule_aware_tasks / rules_fired are ALWAYS a live Neo4j
                # read, cache hit or not — cached_plan never carries
                # rule_aware_tasks and case_data never carries rules_fired
                # at all (see core.persistence_filters.strip_graph_derived_fields),
                # so a rejected inference removes the task it justified
                # immediately instead of waiting for a full case reload.
                subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
                live_findings = fetch_live_graph_findings(req.case_id, subject_id)
                live_rule_aware_tasks = build_rule_aware_tasks(
                    live_findings.get("rules_fired", []),
                    live_findings.get("graph_context") or {},
                )
                return {
                    "complaint_number": get_complaint_number(case_data) or req.case_id,
                    "status": "completed",
                    "details": {
                        "agent_summary": cached_summary,
                        "provenance_trail": format_provenance_lines(case_data.get("provenance_trail", [])),
                        "graph_findings": {
                            "rule_aware_tasks": live_rule_aware_tasks,
                            "rules_fired": fired_rules_only(live_findings.get("rules_fired")),
                        },
                        "meta": {
                            "data_source": data_source,
                            "plan_source": plan_source,
                            "modified_by": modified_by,
                            "modified_on": modified_on.isoformat() if modified_on else None,
                            "plan_stale": plan_stale,
                            "agent_summary_source": "db_cache",
                            # AI-32: independent of plan_stale above —
                            # plan_stale is specifically about a saved
                            # HUMAN OVERRIDE lagging behind case data
                            # (Section E.5); stale is about the
                            # underlying AI-GENERATED narrative itself.
                            # Both can be true at once and mean
                            # different things.
                            "stale": staleness.stale,
                            "generated_at": get_route_generated_at(case_data, "plan"),
                            "username": get_route_username(case_data, "plan"),
                        },
                    },
                }

        # token: caller's AppWorks SAMLart, consumed by
        # semantic_layer/dispatcher.py before any tool function sees it —
        # see appworks/appworks_auth.py's set_request_token.
        execution_context = {"ai_summary": req.ai_summary, "token": token}
        runner = get_runner()
        # Scope to plan retrieval only (Step 4)

        # AI-16 (Section 8.5): build the rule-aware task recommendations from
        # the rules_fired already in context and hand them to the prompt, so
        # the agent selects investigation steps from both the rule-derived
        # tasks and the BSI catalogue tasks its scoped tool returns.
        case_data_for_prompt, rule_aware_tasks = prepare_plan_context(req.case_id, case_data)

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_plan_prompt(case_data_for_prompt),
            user_message=(
                f"Review the investigation context for case {req.case_id} and execute the "
                "appropriate on-demand tools to assemble the investigation plan."
            ),
            scope="INVESTIGATION_PLAN",  # ← this scope includes intake + enrichment tools only
            execution_context=execution_context,
        )

        sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)

        # Parse/validate the LLM's plan output and apply any human override
        # (Section D.6) — factored out to api/pipeline_execution.py.
        (
            assistant_text,
            investigation_plan,
            plan_section,
            merged_provenance,
            plan_source,
            modified_by,
            modified_on,
            plan_stale,
        ) = run_plan_pipeline(
            req.case_id,
            case_data,
            sections,
            messages,
            new_provenance,
            plan_generated_at_before_call,
            rule_aware_tasks,
        )

        # Update CS-4 warm store but return only the route-specific section.
        # plan_section/investigation_plan here are the PURE, un-overridden
        # LLM output (see run_plan_pipeline) — this is intentional; the
        # warm store and Postgres must only ever hold the AI baseline so
        # POST /plan/revert_to_ai has something real to revert to.
        CASE_STORE[req.case_id].update(plan_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        # ai_summary: updated contract — investigation sections separate from plan.
        # Persisted server-side (Postgres case_ai_summary_store); /copilot falls
        # back to it via CS-4 resolution rather than receiving it directly.
        ai_summary = build_ai_summary(
            case_data,
            {"investigation_plan": investigation_plan},
            merged_provenance,
        )
        # Cache this run's RESOLVED agent_summary markdown — the LLM's own
        # markdown, PURE, with no override spliced in. This is what makes
        # POST /plan/revert_to_ai correct: the cached "AI" text must never
        # be the override's own text, or reverting has nothing genuine to
        # fall back to. The override is spliced into a SEPARATE
        # response-only copy below (response_agent_summary) — never into
        # what gets cached here. Carries forward any other route's
        # already-cached entry for this case_id.
        resolved_agent_summary = resolve_plan_agent_summary(
            assistant_text,
            investigation_plan,
            req.case_id,
            case_data,
            merged_provenance,
        )
        ai_summary["investigation"][AGENT_SUMMARY_CACHE_KEY] = merge_agent_summary_cache(
            case_data,
            "plan",
            resolved_agent_summary,
            username=username,
        )
        plan_generated_at = get_route_generated_at(ai_summary["investigation"], "plan")
        plan_username = get_route_username(ai_summary["investigation"], "plan")
        CASE_STORE[req.case_id][AGENT_SUMMARY_CACHE_KEY] = ai_summary["investigation"][
            AGENT_SUMMARY_CACHE_KEY
        ]
        persist_case_session(req.case_id, ai_summary)

        # Response-only override splice — mirrors the /plan CACHE-HIT
        # branch above exactly: the persisted/cached text stays the pure
        # AI baseline (just written above); only what THIS response
        # returns to the caller reflects the override, the same way a
        # cache hit never mutates cached_summary/cached_plan before
        # persisting anything.
        response_agent_summary = resolved_agent_summary
        if plan_source == "User Modified":
            response_agent_summary = apply_step_override_to_summary(
                resolved_agent_summary,
                override["modified_steps"] if override is not None else None,
            )
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan",
            endpoint="/plan",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
            username=username,
        )

        # rules_fired for the response is a live Neo4j read, same as the
        # rule_aware_tasks above — case_data (resolved before this
        # request) never carries rules_fired at all (see
        # core.persistence_filters.strip_graph_derived_fields).
        plan_subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
        live_plan_rules_fired = fetch_live_graph_findings(req.case_id, plan_subject_id).get(
            "rules_fired", []
        )

        return {
            "complaint_number": get_complaint_number(case_data) or req.case_id,
            "status": "completed",
            "details": {
                "agent_summary": response_agent_summary,
                "provenance_trail": format_provenance_lines(merged_provenance),
                # Graph-derived plan output (AI-16 —
                # reasoning_layer.investigation_tasks.build_rule_aware_tasks):
                # the rule-aware task recommendations and the rules_fired
                # block they were derived from. Section 8.5 requires these to
                # be displayed SEPARATELY from the generic steps, which the UI
                # can only do if it receives them as data — the rendered
                # agent_summary alone cannot be split reliably. catalog_tasks
                # is deliberately not here: it is the AppWorks task catalogue,
                # not a graph finding, and it already travels on the plan.
                "graph_findings": {
                    "rule_aware_tasks": investigation_plan.get("rule_aware_tasks"),
                    "rules_fired": fired_rules_only(live_plan_rules_fired),
                },
                "meta": {
                    "data_source": data_source,
                    "plan_source": plan_source,
                    "modified_by": modified_by,
                    "modified_on": modified_on.isoformat() if modified_on else None,
                    "plan_stale": plan_stale,
                    "agent_summary_source": "llm",
                    "stale": staleness.stale,
                    "generated_at": plan_generated_at,
                    "username": plan_username,
                },
            },
        }
    except AppworksSessionExpiredError as exc:
        logger.warning("plan route: AppWorks session expired for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan",
            endpoint="/plan",
            latency_ms=int((time.time() - start) * 1000),
            status="auth_error",
            username=username,
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Plan route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan",
            endpoint="/plan",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
            username=username,
        )
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {exc}") from exc
    finally:
        logger.info("POST /plan completed for case_id=%s", req.case_id)