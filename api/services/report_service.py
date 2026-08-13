"""
Service layer for POST /generate_report and POST /generate_report/pdf
(AI-18, Functional Spec Section 8.7, Developer Spec Section 7.5).

Owns: the entire /generate_report and /generate_report/pdf business
logic — Related Network assembly, plan-override read, the report cache
(content-diff + AI-32 graph signal), Decision & Override Log assembly,
the LLM agent call, persistence to report_artifacts, and response/PDF
shaping.

Does NOT own: HTTP routing itself (api/server.py just calls
run_generate_report(req) / run_generate_report_pdf(req) and returns/
raises whatever they do), or the underlying primitives (reasoning_layer/
report_generation.py, reasoning_layer/decision_log.py,
reasoning_layer/report_llm_context.py, core/report_artifacts_repository.py).

run_generate_report_pdf is a DELIBERATE DUPLICATE of run_generate_report's
pipeline, not a wrapper around it — this mirrors the exact architecture
decision already documented on the /generate_report/pdf route before
this refactor (see that function's own docstring below): a future
change to one must never be silently and implicitly inherited by the
other, so keeping them as two independent functions in the same module
(rather than factoring out a shared helper) is intentional, not
duplication debt.

`_resolve_case_store` is deliberately imported LATE (inside each
function, not at module level) rather than at the top of this file: it
still lives in api/server.py (a route-agnostic CS-4 helper that hasn't
been extracted to a service module of its own), and api/server.py
imports THIS module at its own top level to wire up both routes. A
top-level `from api.server import _resolve_case_store` here would
therefore be a circular import at module load time. Deferring the
import into each function body sidesteps the cycle and, as a bonus,
means existing tests that patch `api.server._resolve_case_store` before
calling `server.generate_report(...)` / `server.generate_report_pdf(...)`
keep working unmodified.

Extracted verbatim from api/server.py's two route bodies during the
service-layer refactor — same behavior, same log lines, same response/
PDF shape; only the module boundary changed. Tests that used to patch
api.server.<name> for either route's OWN internals (as opposed to
_resolve_case_store above) now patch
api.services.report_service.<name> instead, since that's where the
call sites actually live now.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import HTTPException
from fastapi.responses import Response
from neo4j.exceptions import Neo4jError

from agent_service.prompt_builders import build_report_generation_prompt
from agent_service.runner_provider import get_runner
from api.message_utils import extract_agent_summary, merge_direct_result, merge_provenance
from api.models import ReportGenerationRequest
from api.pipeline_execution import (
    evaluate_cache_staleness,
    fetch_live_graph_findings,
    fetch_live_similar_cases,
)
from api.response_builders import fired_rules_only, format_provenance_lines, replace_markdown_section
from core.agent_audit_repository import log_agent_call
from core.investigation_plan_override_repository import get_override
from core.narrative_staleness import StalenessCheck
from core.report_artifacts_repository import get_latest_report, save_report
from reasoning_layer.decision_log import build_decision_log, render_reviewed_and_excluded_markdown
from reasoning_layer.neo4j_client import GraphUnavailableError
from reasoning_layer.report_generation import assemble_related_network
from reasoning_layer.report_llm_context import build_report_llm_context
from semantic_layer.entity_contracts import GeneratedReport as GeneratedReportContract
from utils.report_pdf_renderer import render_report_pdf, report_pdf_filename

logger = logging.getLogger(__name__)


def run_generate_report(req: ReportGenerationRequest) -> Dict[str, Any]:
    """
    ON-DEMAND — Report Generation Route (AI-18, Functional Spec Section
    8.7, Developer Spec Section 7.5). Built last — depends on /intake,
    /similar_cases, /risk_assessment, and /plan already having populated
    CS-4 for this case (AI-13, AI-17).

    Assembles the Related Network section deterministically from Neo4j
    (reasoning_layer.report_generation — every active High/Medium fact
    plus every rejected fact for the Primary Subject, rejected facts
    never silently omitted), combines it with the case narrative already
    on file in CS-4, and has the LLM write the narrative prose ONLY — it
    is never asked to decide which connections belong in the report
    (Section 8.7). The result is persisted to report_artifacts (D.5) as
    a new draft.

    reload_ai_summary=False (default): the Related Network is always
    re-read fresh from Neo4j and the plan override always re-read fresh
    from Postgres (both cheap, non-LLM reads) — if a report already
    exists for this case_id in report_artifacts AND that fresh read is
    identical to what was cached, answer from the latest persisted draft
    with only the Decision & Override Log re-derived, no LLM call. If
    the Related Network has changed since (a rejection, a revert, a
    newly-active connection), OR AI-31's (:Case).last_inference_change_at
    is newer than this draft's own generated_at (AI-32), the cache is
    treated as stale regardless of reload_ai_summary and a fresh report
    is generated. reload_ai_summary=True always skips SERVING from this
    cache and persists a fresh draft row — though the report_artifacts
    lookup itself still runs even then, purely so the response's
    stale flag can still correctly report a graph-side change even on
    a request that also happens to pass reload_ai_summary=True; see the
    AI-32 comment inline below for why that one extra read is worth its
    cost here.
    """
    # Deferred — see module docstring for why.
    from api.server import _resolve_case_store

    start = time.time()
    try:
        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        case_data, data_source = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info(
            "case_id=%s data_source=%s key_count=%d",
            req.case_id,
            data_source,
            len(list(case_data.keys())),
        )

        runner = get_runner()

        subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
        if not subject_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Report generation requires a resolved primary subject for "
                    f"case_id={req.case_id}. Run /intake first."
                ),
            )

        # --- AI-18: deterministic Related Network assembly (Section 8.7) ---
        # Called DIRECTLY — not an LLM tool, not dispatcher-routed, not in
        # manifest.yaml (same governance as similar_cases/AI-14 and
        # check_network_match/AI-12: manifest holds a tool only if it is
        # LLM-called AND makes an AppWorks call; this is a Neo4j read).
        # The LLM's role is to EXPLAIN this section, never to decide its
        # contents. Non-blocking: a graph outage degrades to an empty,
        # clearly-unavailable section rather than failing the route.
        #
        # Run BEFORE the report cache check below (not just in the fresh-
        # generation path) so a cache hit can compare against the CURRENT
        # graph state — a Neo4j read is cheap relative to the LLM call the
        # cache exists to avoid, so there is no reason to trust a stale
        # snapshot here just because /generate_report has been called
        # before for this case_id.
        try:
            related_envelope = assemble_related_network(req.case_id, subject_id)
            related = related_envelope["result"]
        except (ValueError, GraphUnavailableError, Neo4jError) as exc:
            logger.warning(
                "related-network assembly unavailable for case_id=%s subject_id=%s — %s",
                req.case_id,
                subject_id,
                exc,
            )
            related = {
                "subject_id": subject_id,
                "related_network": [],
                "confidence_summary": {"high": 0, "medium": 0, "unresolved": 0},
                "rejected_count": 0,
                "unavailable_reason": str(exc),
            }
            related_envelope = {
                "result": related,
                "provenance": {
                    "sources": [],
                    "retrieved_at": "",
                    "computed_by": "reasoning_layer.report_generation.assemble_related_network",
                },
            }

        # investigation_plan_overrides (Section D.6) must be reflected
        # "regardless of which endpoint is queried" (see core/
        # investigation_plan_override_repository.py) — /plan and /copilot
        # already re-check it fresh on every call rather than trusting
        # whatever was true at generation time. Same reasoning as the
        # Related Network read above: fetch it fresh here, before the
        # cache check, so a cache hit can be judged against the current
        # override state instead of the one that was true when the
        # cached report was generated.
        try:
            plan_override = get_override(req.case_id)
        except Exception as exc:
            logger.warning(
                "investigation_plan_overrides lookup failed for case_id=%s "
                "during report generation — treating as no override: %s",
                req.case_id,
                exc,
            )
            plan_override = None

        # Report cache: if a report has already been generated and
        # persisted for this case_id AND neither staleness trigger below
        # fired, answer from the latest report_artifacts row WITHOUT
        # calling the LLM again.
        #
        # /generate_report's "graph changed" signal is the OR of TWO
        # independent detectors, deliberately kept BOTH rather than one
        # replacing the other:
        #   1. The existing, finer-grained content diff: is the live
        #      Related Network read above byte-identical to what was
        #      cached? This predates AI-32 and stays authoritative for
        #      the actual cache-serve decision — it catches the exact
        #      set of cases that matter for THIS route (a connection's
        #      status/membership actually differs) and is immune to
        #      false positives from a reject immediately followed by a
        #      revert (net content unchanged, but AI-31's timestamp
        #      would still have moved).
        #   2. AI-32's new (:Case).last_inference_change_at vs this
        #      report's own generated_at — the same coarse signal every
        #      other cached-narrative route now uses. Folded in with OR,
        #      never replacing #1: a defense-in-depth signal, not a
        #      downgrade of the existing precision. Practically this
        #      only ever fires ahead of #1 in the reject-then-revert case
        #      above, where reporting stale=True even though the content
        #      is provably unchanged is the honest answer — an
        #      investigator DID touch the graph — while this route
        #      still correctly serves the (genuinely still-accurate)
        #      cached content either way.
        # Any actual difference means the cached narrative prose
        # (Reviewed and Excluded Connections, Network Connections) can no
        # longer be trusted — that text was written by the LLM once, at
        # generation time, and there is no safe way to splice a per-
        # connection narrative sentence the way the deterministic Decision
        # & Override Log block below can be — so a real change falls
        # through to the full regeneration path and gets a fresh LLM
        # narrative, exactly as if reload_ai_summary=True had been passed.
        #
        # core_data_changed (reload_ai_summary=True) always skips SERVING
        # from this cache — a forced reload must produce fresh prose. The
        # lookup itself still runs either way (a cheap indexed Postgres
        # read next to the LLM call this cache exists to avoid), purely so
        # the response's `stale` flag still correctly reports a graph-side
        # change even on a request that also happens to pass
        # reload_ai_summary=True — `stale` reports the graph signal only
        # (core_data_changed is never folded into it; see
        # StalenessCheck.stale's docstring for why), so without this
        # lookup a caller who set reload_ai_summary=True would silently
        # lose the graph signal from the response instead of just no
        # longer needing it to decide whether to serve the cache.
        core_data_changed = bool(req.reload_ai_summary)
        cached_report = get_latest_report(req.case_id)
        if cached_report is not None:
            cached_content = cached_report.get("content") or {}
            cached_related_network = cached_content.get("related_network", [])
            live_related_network = related.get("related_network", [])
            content_unchanged = live_related_network == cached_related_network

            # cached_report["generated_at"] is report_artifacts' own
            # native Postgres timestamp column for this exact draft
            # (distinct from cached_content["generated_at"], a string
            # baked into the JSON body) — the correct cache_generated_at
            # reference point for THIS report, never
            # case_ai_summary_store.updated_at, which tracks a different
            # cache entirely.
            report_staleness = evaluate_cache_staleness(
                req.case_id,
                reload_ai_summary_requested=req.reload_ai_summary,
                cache_generated_at=cached_report.get("generated_at"),
            )
            graph_changed = (not content_unchanged) or report_staleness.graph_changed
            # StalenessCheck is frozen and takes plain booleans, so
            # folding in content_unchanged alongside AI-31's own
            # timestamp signal is a direct construction rather than a
            # second call into evaluate_cache_staleness — see this
            # block's own comment above for why both detectors matter.
            staleness = StalenessCheck(core_data_changed=core_data_changed, graph_changed=graph_changed)

            if not core_data_changed and content_unchanged:
                cached_rejected_count = sum(
                    1 for entry in cached_related_network if entry.get("status") == "rejected"
                )

                # The Related Network itself is unchanged, but the plan
                # override could still have moved (a plan modification
                # or revert doesn't touch related_network at all) — so
                # the Decision & Override Log is still re-derived fresh
                # here and spliced into the cached narrative rather
                # than trusted from cached_content["decision_log"].
                cached_rejected_connections = [
                    entry for entry in cached_related_network if entry.get("status") == "rejected"
                ]
                decision_log_envelope = build_decision_log(cached_rejected_connections, plan_override)
                decision_log_result = decision_log_envelope["result"]

                # Splice the freshly-rendered section into the cached
                # narrative markdown without re-invoking the LLM — same
                # technique apply_step_override_to_summary already uses
                # to overlay a live override onto /plan's cached prose.
                cached_report_markdown = (cached_content.get("standard_sections") or {}).get(
                    "report_markdown", ""
                )
                resolved_report_markdown = replace_markdown_section(
                    cached_report_markdown,
                    "Decision & Override Log",
                    decision_log_result["decision_log_markdown"],
                )
                # Same treatment for Reviewed and Excluded Connections —
                # deterministic Python formatting, never LLM-computed
                # per-entry fallback text. See
                # reasoning_layer.decision_log.render_reviewed_and_excluded_markdown
                # for why this section specifically needed this fix.
                resolved_report_markdown = replace_markdown_section(
                    resolved_report_markdown,
                    "Reviewed and Excluded Connections",
                    render_reviewed_and_excluded_markdown(cached_related_network),
                )

                duration_seconds = round(time.time() - start, 1)
                logger.info(
                    "generate_report CACHE HIT for case_id=%s — Related Network "
                    "unchanged since last draft, answering from report_artifacts "
                    "with a freshly-derived Decision & Override Log, no LLM call made",
                    req.case_id,
                )
                log_agent_call(
                    case_id=req.case_id,
                    agent_name="report_generation",
                    endpoint="/generate_report",
                    latency_ms=int(duration_seconds * 1000),
                    status="success",
                )
                return {
                    "case_id": req.case_id,
                    "status": "completed",
                    "report_id": cached_content.get("report_id"),
                    "generated_at": cached_content.get("generated_at"),
                    "details": {
                        "agent_summary": resolved_report_markdown,
                        "provenance_trail": format_provenance_lines(cached_report.get("provenance_trail", [])),
                        "related_network": cached_related_network,
                        "confidence_summary": cached_content.get(
                            "confidence_summary", {"high": 0, "medium": 0, "unresolved": 0}
                        ),
                        "rejected_count": cached_rejected_count,
                        "decision_log": decision_log_result.get("decision_log", []),
                        "meta": {
                            "data_source": data_source,
                            "report_status": cached_content.get("status", "draft"),
                            "agent_summary_source": "db_cache",
                            "persisted_to_postgres": True,
                            "stale": staleness.stale,
                        },
                    },
                }

            logger.info(
                "generate_report CACHE STALE for case_id=%s stale=%s core_data_changed=%s "
                "graph_changed=%s — regenerating a fresh report instead of serving "
                "report_artifacts",
                req.case_id,
                staleness.stale,
                core_data_changed,
                staleness.graph_changed,
            )
        else:
            # No report has ever been generated for this case_id — a
            # genuine first run, not staleness. graph_changed is reported
            # False here for the same reason every other cached-narrative
            # route treats a missing cache as "nothing to compare, no
            # signal" rather than guessing (see
            # core.narrative_staleness.is_graph_newer's docstring).
            staleness = StalenessCheck(core_data_changed=core_data_changed, graph_changed=False)

        # --- Decision & Override Log assembly (Report Design ACTIONS #3) ---
        # Deterministic, non-LLM formatting over two inputs /generate_report
        # already has in hand: the case's investigation-plan override
        # (fetched fresh above, before the cache check) and the rejected
        # entries out of the Related Network read just above. No graph or
        # DB call of its own — see reasoning_layer/decision_log.py.
        rejected_connections = [
            entry for entry in related.get("related_network", []) if entry.get("status") == "rejected"
        ]
        decision_log_envelope = build_decision_log(rejected_connections, plan_override)
        decision_log_result = decision_log_envelope["result"]

        # rules_fired for the report narrative is a live Neo4j read, same
        # as /intake and /plan (see fetch_live_graph_findings) — case_data
        # (resolved above via CS-4 warm lookup -> Postgres fallback ->
        # ai_summary body) NEVER carries rules_fired at all
        # (core.persistence_filters.strip_graph_derived_fields strips it
        # before every write), so case_data.get("rules_fired") was always
        # None/[] here and "Rules Fired" narrated as empty regardless of
        # what had actually fired in the graph. Reading it fresh means an
        # investigator's reject/revert, or a rule that fired since the
        # last cached draft, is reflected in this report immediately.
        live_report_rules_fired = fetch_live_graph_findings(req.case_id, subject_id).get(
            "rules_fired", []
        )

        # similar_cases for the report narrative is a live Neo4j read,
        # same pattern as rules_fired immediately above and the same
        # helper /similar_cases and /risk_assessment already use (see
        # fetch_live_similar_cases) — case_data (resolved above via CS-4
        # warm lookup -> Postgres fallback -> ai_summary body) NEVER
        # carries similar_cases at all
        # (core.persistence_filters.strip_graph_derived_fields strips the
        # whole top-level section before every write, by design, so a
        # stale match list is never served), so case_data.get(
        # "similar_cases") was always None here regardless of whether
        # /similar_cases had ever been run for this case, and the "Similar
        # Cases" section always fell through to the prompt's own "No
        # similar cases identified." fallback (config/prompts.py). Reading
        # it fresh means the report reflects the graph as it stands right
        # now, exactly like every other tab.
        live_similar_cases = fetch_live_similar_cases(req.case_id)

        # Inject the computed network and decision log into the case
        # context the prompt serialises, so the LLM narrates THESE facts
        # (never adds, removes, or reorders them — REPORT_GENERATION scope
        # carries no tools, so the LLM cannot re-query the graph or
        # Postgres itself either).
        #
        # rules_fired is also trimmed here to fired-only entries — the same
        # response-boundary filter /intake and /plan already apply via
        # fired_rules_only() (see api/response_builders.py: CASE_STORE and
        # the merge keep the full fixed 14-entry block; every reader that
        # displays it to an investigator trims to fired:true first).
        case_data_for_prompt = {
            **case_data,
            "rules_fired": fired_rules_only(live_report_rules_fired),
            "related_network": related.get("related_network", []),
            "confidence_summary": related.get("confidence_summary", {}),
            "rejected_count": related.get("rejected_count", 0),
            "decision_log": decision_log_result.get("decision_log", []),
            "similar_cases": live_similar_cases,
        }
        # case_data_for_prompt above is the FULL context (superset of what
        # the report needs — kept as-is; nothing downstream that reads
        # case_data_for_prompt itself changes). build_report_llm_context
        # derives a MINIMIZED copy of it for the LLM prompt ONLY: drops
        # agent_summary_cache / provenance_trail / network_match_flag,
        # keeps only the Primary Subject, strips rejection-audit
        # attribution (who/when) from rules_fired instances, trims
        # risk_assessment down to scores, similar_cases down to count +
        # similarity signal, investigation_plan down to steps, and
        # collapses prior-guilty case lists to a count. See
        # reasoning_layer/report_llm_context.py for the full rationale.
        llm_prompt_context = build_report_llm_context(case_data_for_prompt, case_id=req.case_id)

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_report_generation_prompt(llm_prompt_context),
            user_message=(
                f"Compose the investigation report narrative for case {req.case_id} "
                "from the case record already provided. The Related Network and "
                "Reviewed and Excluded Connections sections are already finalized "
                "in related_network — narrate every entry given, in full, without "
                "adding, removing, or reordering any of them."
            ),
            scope="REPORT_GENERATION",
        )

        # The authoritative related_network section is the DETERMINISTIC
        # graph result, not anything the LLM produced — the LLM narrates,
        # it does not decide inclusion (mirrors AI-14's similar_cases pattern).
        sections: dict = {}
        new_provenance = merge_direct_result(
            sections,
            new_provenance,
            "related_network",
            related_envelope,
        )
        # Same treatment for the Decision & Override Log — deterministic
        # Python output, never anything the LLM decided (mirrors the
        # related_network merge immediately above).
        new_provenance = merge_direct_result(
            sections,
            new_provenance,
            "decision_log",
            decision_log_envelope,
        )

        assistant_text = extract_agent_summary(messages)

        # Splice the deterministically-rendered Reviewed and Excluded
        # Connections section into the LLM's markdown, overwriting
        # whatever the LLM produced for it. The LLM has been observed
        # writing "not recorded" for notation fields it was actually
        # given real values for (see
        # reasoning_layer.decision_log.render_reviewed_and_excluded_markdown's
        # docstring) — same fix already applied to Decision & Override
        # Log via decision_log_markdown, applied here to the section
        # that was actually failing in production.
        assistant_text = replace_markdown_section(
            assistant_text,
            "Reviewed and Excluded Connections",
            render_reviewed_and_excluded_markdown(related.get("related_network", [])),
        )

        merged_provenance = merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
        )

        report_id = f"RPT-{req.case_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        generated_at = datetime.now(timezone.utc).isoformat()
        confidence_summary = related.get("confidence_summary", {"high": 0, "medium": 0, "unresolved": 0})
        report_content = {
            "report_id": report_id,
            "case_id": req.case_id,
            "generated_at": generated_at,
            "status": "draft",
            "standard_sections": {"report_markdown": assistant_text},
            "related_network": related.get("related_network", []),
            "confidence_summary": confidence_summary,
            "decision_log": decision_log_result.get("decision_log", []),
        }

        try:
            validated_report = GeneratedReportContract(**report_content)
            report_content = validated_report.model_dump(exclude_none=True)
        except Exception as e:
            logger.warning(
                f"Generated report schema validation failed for case_id={req.case_id} "
                f"— storing unvalidated: {e}"
            )

        # D.5: report_artifacts is a working/draft copy only — never the
        # authoritative one (the AppWorks-saved report is). A write
        # failure here must not fail this investigator-facing response;
        # Neo4j + CS-4 already produced the authoritative content above.
        persisted = save_report(req.case_id, report_content, status="draft")

        log_agent_call(
            case_id=req.case_id,
            agent_name="report_generation",
            endpoint="/generate_report",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

        return {
            "case_id": req.case_id,
            "status": "completed",
            "report_id": report_id,
            "generated_at": generated_at,
            "details": {
                "agent_summary": assistant_text,
                "provenance_trail": format_provenance_lines(merged_provenance),
                "related_network": related.get("related_network", []),
                "confidence_summary": confidence_summary,
                "rejected_count": related.get("rejected_count", 0),
                "decision_log": decision_log_result.get("decision_log", []),
                "meta": {
                    "data_source": data_source,
                    "report_status": "draft",
                    "agent_summary_source": "llm",
                    "persisted_to_postgres": persisted is not None,
                    "stale": staleness.stale,
                },
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Report generation route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="report_generation",
            endpoint="/generate_report",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc
    finally:
        logger.info("POST /generate_report completed for case_id=%s", req.case_id)


def run_generate_report_pdf(req: ReportGenerationRequest) -> Response:
    """
    ON-DEMAND — Report PDF Export Route (New_REPORT_Design_1.md,
    ACTIONS #6-#7 / TASKS #5-#6). Same request contract as POST
    /generate_report (ReportGenerationRequest: case_id, optional
    ai_summary, optional reload_ai_summary) and the exact same
    case-resolution, Related Network assembly, plan-override read,
    report cache, and Decision & Override Log pipeline — this route is
    a deliberate DUPLICATE of that pipeline, not a wrapper around it, so
    a future change to one route is never silently and implicitly
    inherited by the other. See run_generate_report above for the full
    rationale behind every step below (cache semantics, the
    fresh-read-before-cache-check ordering, why the Decision & Override
    Log is re-derived even on a cache hit, etc.) — none of that changes
    here.

    The only difference is the last step: instead of returning the
    generated report as JSON, the finished report markdown (the same
    `report_markdown` body /generate_report returns inside
    details.agent_summary, pre-HTML-conversion) is rendered to a
    paginated PDF via utils/report_pdf_renderer.py — a NEW, static,
    non-interactive renderer (NOT utils/html_converter.py, whose
    <details>/<summary> collapsible sections are built for on-screen
    clicking and can end up hidden entirely in a headless print render;
    see New_REPORT_Design_1.md TASKS #5) — and streamed back as a
    downloadable application/pdf response.

    Every draft this route produces or reuses is persisted to
    report_artifacts (D.5) exactly as /generate_report persists it —
    the two routes read and write the same drafts, so calling one after
    the other never triggers a redundant LLM call.
    """
    # Deferred — see module docstring for why.
    from api.server import _resolve_case_store

    start = time.time()
    try:
        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        case_data, data_source = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info(
            "case_id=%s data_source=%s key_count=%d",
            req.case_id,
            data_source,
            len(list(case_data.keys())),
        )

        runner = get_runner()

        subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
        if not subject_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Report generation requires a resolved primary subject for "
                    f"case_id={req.case_id}. Run /intake first."
                ),
            )

        # --- AI-18: deterministic Related Network assembly (Section 8.7) ---
        # Identical to /generate_report — see that route for the full
        # rationale on why this runs BEFORE the cache check below.
        try:
            related_envelope = assemble_related_network(req.case_id, subject_id)
            related = related_envelope["result"]
        except (ValueError, GraphUnavailableError, Neo4jError) as exc:
            logger.warning(
                "related-network assembly unavailable for case_id=%s subject_id=%s — %s",
                req.case_id,
                subject_id,
                exc,
            )
            related = {
                "subject_id": subject_id,
                "related_network": [],
                "confidence_summary": {"high": 0, "medium": 0, "unresolved": 0},
                "rejected_count": 0,
                "unavailable_reason": str(exc),
            }
            related_envelope = {
                "result": related,
                "provenance": {
                    "sources": [],
                    "retrieved_at": "",
                    "computed_by": "reasoning_layer.report_generation.assemble_related_network",
                },
            }

        # investigation_plan_overrides (Section D.6) — fetched fresh here,
        # before the cache check, same as /generate_report.
        try:
            plan_override = get_override(req.case_id)
        except Exception as exc:
            logger.warning(
                "investigation_plan_overrides lookup failed for case_id=%s "
                "during report generation — treating as no override: %s",
                req.case_id,
                exc,
            )
            plan_override = None

        # Report cache (reload_ai_summary=False, default) — identical
        # semantics to /generate_report's cache check. On a hit, splice a
        # freshly-derived Decision & Override Log into the cached
        # narrative markdown (no LLM call), then render THAT markdown to
        # PDF instead of returning it as JSON.
        if not req.reload_ai_summary:
            cached_report = get_latest_report(req.case_id)
            if cached_report is not None:
                cached_content = cached_report.get("content") or {}
                cached_related_network = cached_content.get("related_network", [])
                live_related_network = related.get("related_network", [])

                if live_related_network == cached_related_network:
                    cached_rejected_connections = [
                        entry for entry in cached_related_network if entry.get("status") == "rejected"
                    ]
                    decision_log_envelope = build_decision_log(cached_rejected_connections, plan_override)
                    decision_log_result = decision_log_envelope["result"]

                    cached_report_markdown = (cached_content.get("standard_sections") or {}).get(
                        "report_markdown", ""
                    )
                    resolved_report_markdown = replace_markdown_section(
                        cached_report_markdown,
                        "Decision & Override Log",
                        decision_log_result["decision_log_markdown"],
                    )
                    # Same treatment for Reviewed and Excluded Connections
                    # — see /generate_report above for why.
                    resolved_report_markdown = replace_markdown_section(
                        resolved_report_markdown,
                        "Reviewed and Excluded Connections",
                        render_reviewed_and_excluded_markdown(cached_related_network),
                    )

                    duration_seconds = round(time.time() - start, 1)
                    logger.info(
                        "generate_report/pdf CACHE HIT for case_id=%s — Related Network "
                        "unchanged since last draft, rendering PDF from report_artifacts "
                        "with a freshly-derived Decision & Override Log, no LLM call made",
                        req.case_id,
                    )
                    log_agent_call(
                        case_id=req.case_id,
                        agent_name="report_generation",
                        endpoint="/generate_report/pdf",
                        latency_ms=int(duration_seconds * 1000),
                        status="success",
                    )

                    cached_report_id = cached_content.get("report_id", "")
                    cached_generated_at = cached_content.get("generated_at", "")
                    pdf_bytes = render_report_pdf(
                        resolved_report_markdown,
                        case_id=req.case_id,
                        report_id=cached_report_id,
                        generated_at=cached_generated_at,
                    )
                    filename = report_pdf_filename(req.case_id, cached_report_id)
                    return Response(
                        content=pdf_bytes,
                        media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                    )

                logger.info(
                    "generate_report/pdf CACHE STALE for case_id=%s — Related Network has "
                    "changed since the last draft (rejection, revert, or new connection); "
                    "regenerating a fresh report instead of serving report_artifacts",
                    req.case_id,
                )

        # --- Decision & Override Log assembly (Report Design ACTIONS #3) ---
        # Identical to /generate_report.
        rejected_connections = [
            entry for entry in related.get("related_network", []) if entry.get("status") == "rejected"
        ]
        decision_log_envelope = build_decision_log(rejected_connections, plan_override)
        decision_log_result = decision_log_envelope["result"]

        # rules_fired for the report narrative is a live Neo4j read,
        # identical to the fix in /generate_report above (and the same
        # pattern /intake and /plan already use via
        # fetch_live_graph_findings) — case_data never carries rules_fired
        # at all (core.persistence_filters.strip_graph_derived_fields), so
        # case_data.get("rules_fired") was always None/[] here too and the
        # rendered PDF's "Rules Fired" section always read "No inference
        # rules fired for this case."
        live_report_rules_fired = fetch_live_graph_findings(req.case_id, subject_id).get(
            "rules_fired", []
        )

        # similar_cases for the report narrative is a live Neo4j read,
        # identical to the fix in /generate_report above (and the same
        # helper /similar_cases and /risk_assessment already use via
        # fetch_live_similar_cases) — case_data never carries similar_cases
        # at all (core.persistence_filters.strip_graph_derived_fields), so
        # case_data.get("similar_cases") was always None here too and the
        # rendered PDF's "Similar Cases" section always read "No similar
        # cases identified." regardless of whether /similar_cases had ever
        # been run for this case.
        live_similar_cases = fetch_live_similar_cases(req.case_id)

        # Same context assembly and prompt as /generate_report — the LLM
        # narrates the Related Network, Reviewed and Excluded Connections,
        # and Decision & Override Log sections; it never decides their
        # contents.
        case_data_for_prompt = {
            **case_data,
            "rules_fired": fired_rules_only(live_report_rules_fired),
            "related_network": related.get("related_network", []),
            "confidence_summary": related.get("confidence_summary", {}),
            "rejected_count": related.get("rejected_count", 0),
            "decision_log": decision_log_result.get("decision_log", []),
            "similar_cases": live_similar_cases,
        }
        llm_prompt_context = build_report_llm_context(case_data_for_prompt, case_id=req.case_id)

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_report_generation_prompt(llm_prompt_context),
            user_message=(
                f"Compose the investigation report narrative for case {req.case_id} "
                "from the case record already provided. The Related Network and "
                "Reviewed and Excluded Connections sections are already finalized "
                "in related_network — narrate every entry given, in full, without "
                "adding, removing, or reordering any of them."
            ),
            scope="REPORT_GENERATION",
        )

        # The authoritative related_network section is the DETERMINISTIC
        # graph result, not anything the LLM produced.
        sections: dict = {}
        new_provenance = merge_direct_result(
            sections,
            new_provenance,
            "related_network",
            related_envelope,
        )
        new_provenance = merge_direct_result(
            sections,
            new_provenance,
            "decision_log",
            decision_log_envelope,
        )

        assistant_text = extract_agent_summary(messages)

        # Splice the deterministically-rendered Reviewed and Excluded
        # Connections section into the LLM's markdown — identical fix to
        # /generate_report above, applied here too since this route runs
        # its own independent LLM call rather than reusing that one.
        assistant_text = replace_markdown_section(
            assistant_text,
            "Reviewed and Excluded Connections",
            render_reviewed_and_excluded_markdown(related.get("related_network", [])),
        )

        report_id = f"RPT-{req.case_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        generated_at = datetime.now(timezone.utc).isoformat()
        confidence_summary = related.get("confidence_summary", {"high": 0, "medium": 0, "unresolved": 0})
        report_content = {
            "report_id": report_id,
            "case_id": req.case_id,
            "generated_at": generated_at,
            "status": "draft",
            "standard_sections": {"report_markdown": assistant_text},
            "related_network": related.get("related_network", []),
            "confidence_summary": confidence_summary,
            "decision_log": decision_log_result.get("decision_log", []),
        }

        try:
            validated_report = GeneratedReportContract(**report_content)
            report_content = validated_report.model_dump(exclude_none=True)
        except Exception as e:
            logger.warning(
                f"Generated report schema validation failed for case_id={req.case_id} "
                f"— storing unvalidated: {e}"
            )

        # D.5: report_artifacts is a working/draft copy only — same
        # persistence as /generate_report, so both routes share the same
        # draft history for a given case_id. A write failure here must
        # not fail this investigator-facing PDF download. The PDF route
        # has no JSON body to surface persisted-state metadata in (unlike
        # /generate_report's "persisted_to_postgres"), so the return
        # value is intentionally not captured here.
        save_report(req.case_id, report_content, status="draft")

        log_agent_call(
            case_id=req.case_id,
            agent_name="report_generation",
            endpoint="/generate_report/pdf",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

        pdf_bytes = render_report_pdf(
            assistant_text,
            case_id=req.case_id,
            report_id=report_id,
            generated_at=generated_at,
        )
        filename = report_pdf_filename(req.case_id, report_id)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Report PDF generation route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="report_generation",
            endpoint="/generate_report/pdf",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=500, detail=f"Report PDF generation failed: {exc}") from exc
    finally:
        logger.info("POST /generate_report/pdf completed for case_id=%s", req.case_id)