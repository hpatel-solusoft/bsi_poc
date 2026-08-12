"""
Owns: AI-32 — combining the two independent staleness signals every
cached-narrative endpoint (/intake, /risk_assessment, /plan,
/similar_cases, /generate_report) tracks. `stale` exposes ONLY the
graph-side signal to the caller (AppWorks) as a plain true/false — see
StalenessCheck.stale's docstring for why core_data is deliberately
excluded from it. Also owns the downstream decisions a route makes once
it knows both raw signals: whether to auto-refresh at all
(should_auto_refresh), and — for the one route that has a genuinely
separate "re-run the reasoning pipeline" stage (see
should_rerun_full_pipeline's docstring) — whether that heavier step is
warranted.

The two triggers, per the Data Persistence Spec's existing Section E
Staleness/Reload Pattern and AI-31:

  1. "core_data" — AppWorks case data changed. An AppWorks workflow rule
     sets reload_ai_summary on the Workfolder record; the frontend reads
     that flag and forwards it to every route as the reload_ai_summary
     request field, exactly as it already did before this ticket. This
     module does not detect this trigger itself — Section E.1 is
     explicit that "the agent service does not set it and does not read
     it directly" — it only receives the caller's already-resolved
     boolean. Never surfaced back out via `stale`: it is AppWorks's own
     signal, so echoing it back would be circular — see
     StalenessCheck.stale's docstring.
  2. "graph" — an investigator rejected or reverted an inferred fact
     (AI-31's (:Case).last_inference_change_at) more recently than the
     cached narrative was last generated. Unlike core_data, this module
     DOES detect this trigger: it is pure timestamp comparison, with no
     AppWorks-side flag to mirror. This is the ONE signal `stale`
     reports — AppWorks reads it and, when true, is expected to call
     back with reload_ai_summary=true to force a full refresh.

A graph-only signal is reported (stale=True) but does NOT by itself make
a route auto-run the agent — see StalenessCheck.should_auto_refresh's
docstring. Only core_data_changed (an explicit reload_ai_summary=True)
or an explicit caller reload triggers an automatic re-run; a graph-only
change waits for the caller, having seen stale=true, to ask for one.

There is deliberately no separate "reason" field: with only one signal
(graph) ever surfaced, a reason string would carry zero information
beyond `stale` itself — every route's response carries `stale` as its
one and only staleness field.

Does NOT own: reading either timestamp — that is
core/case_store.get_case_ai_summary_cache_updated_at (or the
route-specific equivalent, e.g. report_artifacts.generated_at for
/generate_report) and reasoning_layer/case_staleness.py respectively.
Does NOT own what a "narrative-only regenerate" actually executes at the
pipeline level (api/pipeline_execution.py, api/server.py's route
handlers) — this module is the pure decision logic only, deliberately
free of any database or Neo4j dependency, so it is cheap to exhaustively
unit test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class StalenessCheck:
    """
    The result of one staleness check, carrying both raw individual
    signals (core_data_changed, graph_changed — so a caller can decide
    WHAT KIND of refresh to run internally) and the caller-facing
    `stale` flag, which deliberately reports the graph-side signal ONLY
    (see `stale`'s own docstring).
    """

    core_data_changed: bool
    graph_changed: bool

    @property
    def stale(self) -> bool:
        """
        Plain true/false — the one signal AppWorks actually needs from
        this backend: has AI-31's graph-side signal (an investigator's
        reject/revert) moved since this narrative was last generated.

        Deliberately does NOT fold in core_data_changed. AppWorks is the
        caller that sets reload_ai_summary in the first place (Section
        E.1 — "the agent service does not set it and does not read it
        directly"), so echoing that same flag straight back to AppWorks
        as `stale=true` would just be telling it something it already
        knows about its own request. What AppWorks cannot see for
        itself is whether the GRAPH changed — an investigator action
        inside this system — which is exactly what this field reports.
        AppWorks reads it and, when true, is expected to call back with
        reload_ai_summary=true to force a full refresh, so this must
        stay a plain boolean the caller can branch on directly, never a
        multi-value reason string — there is no `stale_reason` field
        precisely because, with only this one signal ever surfaced, a
        reason string would say nothing `stale` doesn't already say.
        """
        return self.graph_changed

    @property
    def should_refresh(self) -> bool:
        """Whether EITHER reason means the cached narrative must not be
        served as-is. Internal decision helper only — NOT what the
        response's `stale` field reports to the caller (see `stale`'s
        own docstring); should_auto_refresh, not this property, is what
        every route's cache-hit gate actually uses."""
        return self.core_data_changed or self.graph_changed

    @property
    def should_auto_refresh(self) -> bool:
        """
        Whether a route should bypass its cache and automatically run
        the agent (LLM call + persistence) for THIS request, with no
        further reload explicitly requested beyond what already
        produced this StalenessCheck.

        Deliberately narrower than `should_refresh`: a graph-only
        change (an investigator's reject/revert, AI-31) still reports
        `stale=True` exactly as before — the caller still learns the
        cached narrative may be out of date — but it no longer, by
        itself, makes the route silently re-run the agent on the
        investigator's next unrelated call. The already-live
        rules_fired/graph_context reads every route does on every call
        reflect the graph change immediately regardless of whether the
        narrative text itself is regenerated (see
        should_rerun_full_pipeline's docstring), so nothing becomes
        factually wrong by not auto-refreshing — only the cached prose
        may lag until the caller, having seen stale=True, explicitly
        asks for a reload (e.g. POST /reload_all with
        reload_ai_summary=True). Auto-refreshing on every graph-only
        signal meant an investigator opening an unrelated tab could
        trigger an unwanted LLM call and Postgres write with no user
        action requesting one.

        core_data_changed (an explicit reload_ai_summary=True from the
        caller) still auto-refreshes immediately — that signal IS the
        explicit ask, and is exactly what this property used to be
        called with, before AI-32 widened the cache-hit gate to
        should_refresh.
        """
        return self.core_data_changed

    @property
    def should_rerun_full_pipeline(self) -> bool:
        """
        Whether a refresh should re-run the full, expensive pipeline
        stage — re-fetching AppWorks and re-running Wave 1/Wave 2 rule
        inference — rather than only regenerating the narrative text.

        Only "core_data" ever warrants this. A graph-only change is an
        investigator overruling a fact the pipeline already found and
        already wrote to Neo4j (AI-31's reject/revert) — nothing about
        AppWorks structural data or Wave 1/2 rule matching needs
        redoing for that; the already-live rules_fired/graph_context
        reads every route already does reflect it immediately regardless.
        Re-running the full pipeline for a graph-only change would be
        pure waste: extra AppWorks load and latency for a re-computation
        that cannot produce a different structural result.

        NOTE: today only /intake has code that actually branches on
        this (reasoning_layer.context_enrichment.enrich_graph_context's
        `force` parameter, which controls the Wave 1/2 re-run). The
        other four cached-narrative routes have no equivalent separate
        pipeline stage to skip in the first place — for them,
        "regenerate the narrative" and "the only refresh work there is"
        are the same operation, so should_auto_refresh alone is what
        they need; see each route's own AI-32 comment in api/server.py
        for why that is a faithful, not a partial, implementation of
        this ticket's third bullet for those four.
        """
        return self.core_data_changed


def is_graph_newer(
    last_inference_change_at: Optional[datetime],
    cache_generated_at: Optional[datetime],
) -> bool:
    """
    True if AI-31's (:Case).last_inference_change_at is strictly after
    the timestamp the cached narrative was generated at — i.e. an
    investigator's reject/revert happened after the text currently
    cached was written, so that text may describe a fact that is no
    longer active (or omit one that has been reinstated).

    Both None cases resolve to False rather than guessing:
      * last_inference_change_at is None whenever no investigator has
        ever rejected or reverted a finding for this case — there is
        nothing to compare against, so the graph has no independent
        staleness signal to report.
      * cache_generated_at is None whenever there is no cached narrative
        for this case yet at all (first-ever run for this route). There
        is nothing stale to detect: the run about to happen will capture
        whatever the graph looks like right now anyway.

    Equal timestamps are NOT "newer" (strict `>`, not `>=`): a narrative
    generated in the same reject/revert-triggered refresh that set this
    exact last_inference_change_at value must not immediately re-report
    itself as stale.
    """
    if last_inference_change_at is None or cache_generated_at is None:
        return False
    return last_inference_change_at > cache_generated_at


def check_staleness(
    *,
    reload_ai_summary_requested: bool,
    last_inference_change_at: Optional[datetime],
    cache_generated_at: Optional[datetime],
) -> StalenessCheck:
    """
    The one entry point every cached-narrative route calls. A pure
    function — no I/O of its own — so callers own fetching the two
    timestamps (see the module docstring's "Does NOT own" note) and this
    just combines them.

    Args:
        reload_ai_summary_requested: this request's reload_ai_summary
            field, exactly as received — the existing core-data-changed
            signal, unmodified by this ticket.
        last_inference_change_at: AI-31's (:Case).last_inference_change_at,
            parsed to a timezone-aware datetime, or None.
        cache_generated_at: when the narrative currently cached for this
            case (and this route) was generated, as a timezone-aware
            datetime, or None if nothing is cached yet.
            case_ai_summary_store.updated_at (the shared, case-wide
            column) for /intake, /similar_cases, and /risk_assessment;
            report_artifacts.generated_at for /generate_report; and, as
            of AI-35, /plan's OWN per-tab generated_at (AI-34's
            agent_summary_cache["plan"]["generated_at"], via
            core.case_store.get_route_generated_at_datetime) rather
            than the shared column — a refresh on another tab must not
            make /plan's graph-staleness check look fresh for a change
            that has nothing to do with /plan.
    """
    return StalenessCheck(
        core_data_changed=bool(reload_ai_summary_requested),
        graph_changed=is_graph_newer(last_inference_change_at, cache_generated_at),
    )