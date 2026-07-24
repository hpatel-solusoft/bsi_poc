"""
Owns: assembling the "Decision & Override Log" section for Report
Generation (AI-18 / Functional Specification Section 8.7) — one
chronological list combining every investigation-plan override on file
for the case with a single aggregate pointer back to whatever
connections were reviewed and excluded (Section 8.7 / Report Design:
"Reviewed and Excluded Connections" already carries the per-connection
detail; the Decision Log never repeats it).

Same governance as reasoning_layer/report_generation.py: pure,
synchronous formatting over data the caller already fetched. This
module makes no Postgres or Neo4j call of its own — /generate_report
already has both inputs in hand (the plan override via
core.investigation_plan_override_repository.get_override, the rejected
connections as the "rejected" entries of
reasoning_layer.report_generation.assemble_related_network's
related_network) before calling here. "No AI involved in content, just
formatting" (Report Design ACTIONS #3) — the LLM downstream narrates
the entries this module returns, it does not decide what belongs in
them (REPORT_GENERATION_PROMPT: "you do not decide which ... entries
... belong in the report").

Does NOT own: computing the plan override itself (core/
investigation_plan_override_repository.py), computing the rejected
connections themselves (reasoning_layer/report_generation.py), or the
report narrative prompt (config/prompts.py REPORT_GENERATION_PROMPT,
which is the authoritative contract for the entry shape below — keep
the two in sync if either changes).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# Entry "type" tags — REPORT_GENERATION_PROMPT switches its rendering on
# these exact strings (see config/prompts.py, Decision & Override Log
# section), so they are not free-form and must not be renamed without
# updating the prompt in lockstep.
_TYPE_PLAN_MODIFICATION = "plan_modification"
_TYPE_REJECTED_CONNECTION = "rejected_connection"


def _envelope(result: Dict[str, Any], sources: List[str]) -> dict:
    """Standard {result, provenance} envelope (Principle 8) — identical in
    shape to reasoning_layer.report_generation._envelope, so
    /generate_report can merge this into `sections`/`provenance_trail`
    the same way every other direct-call result is merged."""
    return {
        "result": result,
        "provenance": graph_provenance(
            "reasoning_layer.decision_log.build_decision_log", sources=sources
        ),
    }


def _plan_modification_entry(plan_override: Dict[str, Any]) -> Dict[str, Any]:
    """One decision-log entry for the case's current investigation-plan
    override, in full detail (Report Design #3 — plan modifications are
    never summarised down to a pointer the way rejected connections
    are). modified_on may arrive as a datetime (straight from Postgres,
    the /plan route's own path) or as an already-serialised string
    (round-tripped through case_data) — normalised to ISO-8601 either
    way so the prompt's json.dumps(..., default=str) never has to
    guess."""
    modified_on = plan_override.get("modified_on")
    timestamp = modified_on.isoformat() if hasattr(modified_on, "isoformat") else modified_on
    modified_steps = plan_override.get("modified_steps") or []

    return {
        "type": _TYPE_PLAN_MODIFICATION,
        "actor": plan_override.get("modified_by"),
        "timestamp": timestamp,
        "comment": plan_override.get("comment"),
        "modified_step_count": len(modified_steps),
        "modified_steps": modified_steps,
    }


def _rejected_connection_entry(
    rejected_connections: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """One AGGREGATE decision-log entry standing in for every reviewed/
    excluded connection — never one entry per connection. Per-connection
    detail (who, when, why) already lives in the Reviewed and Excluded
    Connections section built from the same related_network list
    (reasoning_layer.report_generation.assemble_related_network); this
    entry is deliberately just a count plus enough of a timestamp to
    place it chronologically, per Report Design #3 ("brief pointer ...
    no repeated detail")."""
    rejection_timestamps = [
        entry["rejection"]["rejected_at"]
        for entry in rejected_connections
        if isinstance(entry.get("rejection"), dict) and entry["rejection"].get("rejected_at")
    ]
    # Most recent review activity, used only for chronological placement
    # against a plan_modification entry — the individual timestamps
    # themselves are not surfaced here (that stays in Reviewed and
    # Excluded Connections, which is where the prompt is told to source
    # per-connection notation from).
    latest_timestamp = max(rejection_timestamps) if rejection_timestamps else None

    return {
        "type": _TYPE_REJECTED_CONNECTION,
        "count": len(rejected_connections),
        "timestamp": latest_timestamp,
    }


def _chronological_sort_key(entry: Dict[str, Any]):
    """Oldest first. An entry with no resolvable timestamp (a plan
    override whose modified_on genuinely was never set, or a rejected-
    connection aggregate with no rejected_at on any of its underlying
    :Rejection records) sorts last rather than being dropped —
    unresolved ordering information is not a reason to omit an entry,
    matching assemble_related_network's own "never silently omit"
    stance on rejected facts."""
    timestamp = entry.get("timestamp")
    return (timestamp is None, timestamp or "")


def build_decision_log(
    rejected_connections: List[Dict[str, Any]],
    plan_override: Optional[Dict[str, Any]],
) -> dict:
    """
    Build the Decision & Override Log section: one chronological list
    combining (a) the case's investigation-plan override, if one
    exists, shown in full, and (b) a single aggregate pointer for every
    reviewed/excluded connection, if any exist — never the connections
    themselves a second time.

    Args:
        rejected_connections: the subset of a related_network list
            (reasoning_layer.report_generation.assemble_related_network)
            whose status == "rejected". Passing the full related_network
            list is also safe — entries without status == "rejected"
            are ignored — but the caller is expected to have already
            filtered, since it is already iterating that list to build
            Reviewed and Excluded Connections.
        plan_override: the row returned by
            core.investigation_plan_override_repository.get_override
            for this case, or None when the investigator has never
            edited the AI-generated plan.

    Returns (inside the standard {result, provenance} envelope):
        {
          "decision_log": [
            {"type": "plan_modification", "actor", "timestamp",
             "comment", "modified_step_count", "modified_steps"},
            {"type": "rejected_connection", "count", "timestamp"},
          ],
          "plan_modified": bool,
          "rejected_connection_count": int,
          "decision_log_markdown": str,  # pre-rendered section body —
              # the prompt should copy this verbatim rather than
              # re-deriving bullets from decision_log itself.
        }

    Neither input existing is not an error: a case with no plan
    override and no rejected connections is not a bug, and returns an
    empty decision_log — the honest answer to "what has an investigator
    decided here" when nothing has been overridden or excluded yet,
    matching REPORT_GENERATION_PROMPT's own "No modifications have been
    made ..." graceful-degradation branch.
    """
    rejected_connections = [
        entry for entry in (rejected_connections or [])
        if entry.get("status") == "rejected"
    ]

    entries: List[Dict[str, Any]] = []
    sources: List[str] = []

    if plan_override:
        entries.append(_plan_modification_entry(plan_override))
        sources.append("Postgres investigation_plan_overrides")

    if rejected_connections:
        entries.append(_rejected_connection_entry(rejected_connections))
        sources.append(
            "reasoning_layer.report_generation.assemble_related_network (rejected entries)"
        )

    entries.sort(key=_chronological_sort_key)

    result = {
        "decision_log": entries,
        "plan_modified": plan_override is not None,
        "rejected_connection_count": len(rejected_connections),
    }
    result["decision_log_markdown"] = render_decision_log_markdown(entries)
    logger.info(
        "build_decision_log: entries=%d plan_modified=%s rejected_connection_count=%d",
        len(entries), plan_override is not None, len(rejected_connections),
    )
    return _envelope(result, sources)


def render_decision_log_markdown(entries: List[Dict[str, Any]]) -> str:
    """
    Deterministically render the "## Decision & Override Log" section
    body from `entries` (the same list returned as
    result["decision_log"] above) — no LLM in the loop.

    This exists because leaving the empty/non-empty decision to the
    LLM (i.e. "if a rejected_connection entry exists, add a bullet;
    otherwise don't") is itself a piece of content logic, which
    Report Design ACTIONS #3 explicitly rules out ("No AI involved in
    content, just formatting"). A model can and does get that
    conditional wrong — it has been observed emitting a stray "0
    connection(s) reviewed and excluded" bullet even when no
    rejected_connection entry exists in decision_log at all. Building
    the exact text here and having the prompt copy it verbatim removes
    that failure mode entirely: there is no conditional left for the
    model to mis-apply.

    Mirrors config/prompts.py REPORT_GENERATION_PROMPT's own "##
    Decision & Override Log" formatting rules exactly — keep the two
    in sync if either changes.
    """
    plan_entries = [e for e in entries if e.get("type") == _TYPE_PLAN_MODIFICATION]
    rejected_entries = [e for e in entries if e.get("type") == _TYPE_REJECTED_CONNECTION]

    lines: List[str] = []

    if plan_entries:
        for entry in plan_entries:
            actor = entry.get("actor") or "not recorded"
            timestamp = entry.get("timestamp") or "not recorded"
            comment = (entry.get("comment") or "").strip()
            if not comment:
                step_count = entry.get("modified_step_count", 0)
                comment = f"{step_count} investigation step(s) modified."
            lines.append(f"* Modified by: {actor} on {timestamp} — {comment}")
    else:
        lines.append("No modifications have been made to the investigation plan.")

    if rejected_entries:
        # By construction there is at most one rejected_connection
        # entry (the aggregate built in _rejected_connection_entry
        # above) — never one per connection.
        count = rejected_entries[0].get("count", 0)
        lines.append("")
        lines.append(
            f"* {count} connection(s) reviewed and excluded by an investigator "
            "— see Reviewed and Excluded Connections above for detail."
        )

    return "\n".join(lines)