"""
Owns: AI-32 — combining the two independent staleness signals every
cached-narrative endpoint (/intake, /risk_assessment, /plan,
/similar_cases, /generate_report) needs into one `stale_reason` the
frontend can render directly, instead of guessing why a refresh banner
appeared. Also owns the two downstream decisions a route makes once it
knows the reason: whether to bypass its cache at all, and — for the one
route that has a genuinely separate "re-run the reasoning pipeline"
stage (see should_rerun_full_pipeline's docstring) — whether that heavier
step is warranted.

The two triggers, per the Data Persistence Spec's existing Section E
Staleness/Reload Pattern and AI-31:

  1. "core_data" — AppWorks case data changed. An AppWorks workflow rule
     sets reload_ai_summary on the Workfolder record; the frontend reads
     that flag and forwards it to every route as the reload_ai_summary
     request field, exactly as it already did before this ticket. This
     module does not detect this trigger itself — Section E.1 is
     explicit that "the agent service does not set it and does not read
     it directly" — it only receives the caller's already-resolved
     boolean.
  2. "graph" — an investigator rejected or reverted an inferred fact
     (AI-31's (:Case).last_inference_change_at) more recently than the
     cached narrative was last generated. Unlike core_data, this module
     DOES detect this trigger: it is pure timestamp comparison, with no
     AppWorks-side flag to mirror.

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

# The four values the ticket specifies verbatim. A plain Optional[str]
# rather than an enum.Enum: this crosses into a JSON response body
# as-is, and a str subclass would just add ceremony a route handler
# would immediately .value away again.
StaleReason = Optional[str]

_BOTH = "both"
_CORE_DATA = "core_data"
_GRAPH = "graph"


@dataclass(frozen=True)
class StalenessCheck:
    """
    The result of one staleness check, carrying both the raw individual
    signals (so a caller can decide WHAT KIND of refresh to run — the
    third bullet of AI-32) and the single combined reason a response
    reports to the frontend.
    """

    core_data_changed: bool
    graph_changed: bool

    @property
    def stale_reason(self) -> StaleReason:
        """"core_data" / "graph" / "both" / None — exactly the four
        values AI-32 asks for, and the literal field a route's response
        carries."""
        if self.core_data_changed and self.graph_changed:
            return _BOTH
        if self.core_data_changed:
            return _CORE_DATA
        if self.graph_changed:
            return _GRAPH
        return None

    @property
    def should_refresh(self) -> bool:
        """Whether EITHER reason means the cached narrative must not be
        served as-is. This is the boolean every route's existing
        `if not req.reload_ai_summary:` cache-hit check widens to."""
        return self.core_data_changed or self.graph_changed

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
        are the same operation, so should_refresh alone is what they
        need; see each route's own AI-32 comment in api/server.py for
        why that is a faithful, not a partial, implementation of this
        ticket's third bullet for those four.
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
            case (and this route) was generated — case_ai_summary_store.
            updated_at for the four LLM-narrative routes, or
            report_artifacts.generated_at for /generate_report — as a
            timezone-aware datetime, or None if nothing is cached yet.
    """
    return StalenessCheck(
        core_data_changed=bool(reload_ai_summary_requested),
        graph_changed=is_graph_newer(last_inference_change_at, cache_generated_at),
    )
