"""
Owns: the rule-to-task mapping that makes the Investigation Plan agent
rule-aware (AI-16 / Section 8.5).

Section 8.5 is explicit about the constraints:
  * "No new database connection required."
  * "Works entirely from the rules_fired block already in context; no
     direct AppWorks calls of its own."

So this module opens NO Neo4j session and makes NO AppWorks call. It is a
pure function over context the /plan route already has: the rules_fired
block (produced by Phase 2 Context Enrichment, AI-13) and the
graph_context alongside it. If rules_fired is absent — a Phase 1 case, or
a case whose pipeline has not run — it returns an empty list and the plan
degrades to exactly the generic LLM synthesis it was before. Rule
awareness is additive, never a precondition.

The mapping covers the six rules Section 8.5 names — 7, 8, 9, 11, 12, 13.
The other eight rules are deliberately unmapped: they are structural
findings (shared employer, shared address, alias identity) that inform the
narrative but do not by themselves imply a specific BSI task. Inventing
tasks for them would put words in the investigator's mouth that the spec
does not sanction.

Two task templates carry placeholders that Section 8.5 writes as [ID] and
[N]. Those are filled from graph_context — the prior guilty case's own ID
and the co-subject hub's case count — because a task that says "Review
prior case [ID]" is not actionable. When the context needed to fill a
placeholder is missing, the task is still emitted with a truthful generic
phrasing rather than a literal "[ID]" leaking to an investigator.

Does NOT own: the AppWorks task catalogue (appworks/allegation_tasks.py),
the plan prompt, or the LLM's selection between the two sources.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Priority per rule. Section 8.5 requires a `priority` on every task but
# does not fix its values, so these encode BSI's own escalation logic:
# anything that forces a supervisor into the loop today (FastTrack, active
# network recidivism) outranks evidence-gathering, which in turn outranks
# breadth-of-review work.
_PRIORITY_CRITICAL = "CRITICAL"
_PRIORITY_HIGH = "HIGH"
_PRIORITY_MEDIUM = "MEDIUM"

# rule_id prefix -> (priority, [task templates])
# Keyed by the stable "Rule_NN" prefix rather than the full rule_id, so a
# rule being renamed in the registry (e.g. Rule_07_Prior_Guilty ->
# Rule_07_Prior_Guilty_Case) does not silently unmap its tasks.
_RULE_TASK_MAP: Dict[str, Dict[str, Any]] = {
    "Rule_07": {
        "detects": "Prior guilty case confirmed",
        "priority": _PRIORITY_HIGH,
        "tasks": [
            "Review prior case {prior_case_id} disposition record",
            "Request prior case evidence package",
        ],
    },
    "Rule_08": {
        "detects": "Recidivist in active fraud network",
        "priority": _PRIORITY_CRITICAL,
        "tasks": [
            "Escalate to supervisor, active network recidivist",
        ],
    },
    "Rule_09": {
        "detects": "PCA check-split network detected",
        "priority": _PRIORITY_HIGH,
        "tasks": [
            "Request Employment Verification per shared employer",
            "Request EBT Transaction History per employer",
        ],
    },
    "Rule_11": {
        "detects": "Cross-case co-subject hub detected",
        "priority": _PRIORITY_MEDIUM,
        "tasks": [
            "Review all {co_subject_case_count} co-subject cases for connected fraud patterns",
        ],
    },
    "Rule_12": {
        "detects": "SLAM wage corroboration confirmed",
        "priority": _PRIORITY_MEDIUM,
        "tasks": [
            "Request DOR Wagematch beyond current fraud date range",
            "Request DOR Taxes",
        ],
    },
    "Rule_13": {
        "detects": "FastTrack eligibility confirmed",
        "priority": _PRIORITY_CRITICAL,
        "tasks": [
            "Escalate to FastTrack, immediate supervisor notification required",
        ],
    },
}

# Fallback phrasings used when graph_context cannot supply a placeholder.
# An investigator should never be handed a task containing a literal
# "[ID]" or "{...}" — a slightly vaguer but honest instruction is strictly
# better than a broken one.
_PLACEHOLDER_FALLBACKS = {
    "prior_case_id": "the prior guilty case",
    "co_subject_case_count": "all",
}


def _placeholder_values(graph_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract the values Section 8.5's [ID] and [N] placeholders need from
    the graph_context AI-13 already put in context. Missing context is not
    an error — it degrades to the fallback phrasings above."""
    gc = graph_context or {}

    prior_cases = gc.get("prior_guilty_cases") or []
    prior_case_id = None
    if prior_cases:
        # Most relevant prior case = the first the graph returned. Its
        # case_id is what an investigator needs to pull the disposition.
        first = prior_cases[0]
        if isinstance(first, dict):
            prior_case_id = first.get("case_id")

    # Section 8.5's [N] is the number of co-subject cases in the hub. The
    # hub_case_ids list is the authoritative source (Rule 11 writes it);
    # fall back to the count of distinct shared connections if absent.
    hub_case_ids = gc.get("hub_case_ids") or []
    co_subject_case_count = len(hub_case_ids) if hub_case_ids else None
    if not co_subject_case_count:
        shared = gc.get("shared_connections") or []
        co_subject_case_count = len(shared) or None

    return {
        "prior_case_id": prior_case_id or _PLACEHOLDER_FALLBACKS["prior_case_id"],
        "co_subject_case_count": co_subject_case_count or _PLACEHOLDER_FALLBACKS["co_subject_case_count"],
    }


def build_rule_aware_tasks(
    rules_fired: Optional[List[Dict[str, Any]]],
    graph_context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Map the rules that FIRED to their BSI task recommendations (Section 8.5).

    Args:
        rules_fired:   the pipeline's rules_fired block from context. Each
                       entry is {rule_id, fired, confidence, corroborated}.
        graph_context: the AI-13 graph_context, used only to fill the [ID]
                       and [N] placeholders. Optional.

    Returns a list of {source_rule, task_type, priority, detects,
    confidence, corroborated} — one entry per recommended task. Empty when
    rules_fired is absent or nothing relevant fired, which is the correct
    "no rule-aware recommendations" answer rather than an error.

    Order is deterministic: rules in ascending rule number, tasks in the
    order Section 8.5 lists them. Two runs over the same context produce
    byte-identical output.
    """
    if not rules_fired:
        logger.info("build_rule_aware_tasks: no rules_fired in context — no rule-aware tasks")
        return []

    values = _placeholder_values(graph_context)
    tasks: List[Dict[str, Any]] = []

    # Sort by rule prefix so output order never depends on how the pipeline
    # happened to order its results.
    for entry in sorted(rules_fired, key=lambda r: str(r.get("rule_id", ""))):
        if not entry.get("fired"):
            continue
        rule_id = str(entry.get("rule_id", ""))
        mapping = _RULE_TASK_MAP.get(rule_id[:7])
        if not mapping:
            # A fired rule with no task mapping is expected for the eight
            # structural rules — not a warning-worthy event.
            continue

        for template in mapping["tasks"]:
            try:
                task_type = template.format(**values)
            except (KeyError, IndexError):
                # A template referencing a value we did not compute is a
                # coding error in the map above, not a data problem. Emit
                # the raw template rather than dropping the task silently.
                logger.warning("build_rule_aware_tasks: unfilled placeholder in %r", template)
                task_type = template

            tasks.append({
                "source_rule": rule_id,
                "task_type": task_type,
                "priority": mapping["priority"],
                # Carried through so the UI can show WHY this task appeared
                # and how much weight the graph put behind it.
                "detects": mapping["detects"],
                "confidence": entry.get("confidence"),
                "corroborated": bool(entry.get("corroborated", False)),
            })

    logger.info(
        "build_rule_aware_tasks: %d rule-aware task(s) from %d fired rule(s)",
        len(tasks),
        sum(1 for r in rules_fired if r.get("fired")),
    )
    return tasks


# --- step source attribution -------------------------------------------------
# Section 8.5 / AI-16 require every investigation step to declare where it
# came from. PLAN_PROMPT's MANDATORY STEP FORMAT (config/prompts.py) already
# makes the LLM write this itself, inline, on every step:
#   "**Step N:** <TaskName verbatim> <synthesized clause> (Source: ...)"
# with the tag being exactly one of "Inference Rule — <rule_id>",
# "BSI catalogue", or "analyst-recommended".
#
# That means the LLM's own text is the single source of truth for
# attribution. This module's job is narrow: recover the SAME fact as a
# machine-readable field (for the API contract / override UI / analytics)
# by reading the tag the LLM already wrote — never by independently
# re-guessing it from wording similarity. A second, fuzzier guess at a fact
# already stated in the text can only ever disagree with what the
# investigator is reading on screen, which is strictly worse than not
# having the structured field at all.

_SOURCE_RULE_AWARE = "rule_aware"
_SOURCE_CATALOG = "catalog"
_SOURCE_LLM = "llm_generated"

# Strips the "**Step N:**" lead the prompt mandates, so the structured
# `action` field holds clean step text rather than a re-embedded copy of
# the markdown formatting.
_STEP_PREFIX_RE = re.compile(r"^\*\*\s*Step\s*\d+\s*:\s*\*\*\s*")

# Matches the trailing "(Source: ...)" tag PLAN_PROMPT requires on every
# step, and captures its inner content for classification.
_SOURCE_TAG_RE = re.compile(r"\s*\(Source:\s*(.*?)\)\s*$", re.IGNORECASE)

# "(Source: Inference Rule — Rule_09)" / "Inference Rule - Rule_09" (either
# dash style survives an LLM turn) -> captures the rule id.
_RULE_TAG_RE = re.compile(r"^Inference Rule\s*[—\-–]\s*(.+)$", re.IGNORECASE)


def parse_declared_step_source(
    raw_text: str,
    rule_aware_tasks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Split one LLM-authored step line into its clean action text plus the
    source attribution the LLM already declared inline.

    priority is looked up from rule_aware_tasks by source_rule — an exact
    key match against data this same request already computed
    (reasoning_layer.investigation_tasks.build_rule_aware_tasks), not a
    second heuristic — so a rule-aware step's priority can never drift
    from the priority BSI's own rule map assigns that rule.

    Returns {"action", "source", "source_rule", "priority"}. Falls back to
    llm_generated with no rule/priority when the turn is missing the
    mandated tag entirely (a malformed LLM turn, not the expected path) —
    logged, never raised, so one malformed step never fails the whole plan.
    """
    text = _STEP_PREFIX_RE.sub("", str(raw_text or "").strip())

    source = _SOURCE_LLM
    source_rule = None
    priority = None

    tag_match = _SOURCE_TAG_RE.search(text)
    if tag_match:
        tag = tag_match.group(1).strip()
        text = _SOURCE_TAG_RE.sub("", text).strip()

        rule_match = _RULE_TAG_RE.match(tag)
        if rule_match:
            source = _SOURCE_RULE_AWARE
            source_rule = rule_match.group(1).strip()
        elif tag.lower().startswith("bsi catalogue"):
            source = _SOURCE_CATALOG
        # else: "analyst-recommended" (or anything unrecognized) stays
        # llm_generated — an honest label rather than a forced match.
    else:
        logger.warning(
            "parse_declared_step_source: step text missing the mandated "
            "(Source: ...) tag — labelling llm_generated: %r", text[:120],
        )

    if source == _SOURCE_RULE_AWARE and source_rule:
        for task in rule_aware_tasks or []:
            if task.get("source_rule") == source_rule:
                priority = task.get("priority")
                break

    return {
        "action": text,
        "source": source,
        "source_rule": source_rule,
        "priority": priority,
    }


def tag_step_sources(
    steps: List[Dict[str, Any]],
    rule_aware_tasks: Optional[List[Dict[str, Any]]] = None,
    catalog_tasks: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Annotate each investigation step with its source (AI-16), by parsing
    the (Source: ...) tag the LLM's own step text already carries under
    PLAN_PROMPT's MANDATORY STEP FORMAT.

    catalog_tasks is accepted for call-site compatibility but is not
    consulted: the LLM already writes "(Source: BSI catalogue)" verbatim
    when it selects a catalogue task, so there is nothing left to infer.

    Returns a new list; the input steps are not mutated. `action` on the
    returned steps is the clean step text with the markdown Step-N prefix
    and Source tag both stripped into their own fields — callers that
    render a step (e.g. a fallback markdown rebuild) must re-add at most
    ONE of each, never assume the raw LLM formatting is still embedded.
    """
    tagged: List[Dict[str, Any]] = []

    for step in steps or []:
        parsed = parse_declared_step_source(step.get("action", ""), rule_aware_tasks)
        annotated = dict(step)
        annotated["action"] = parsed["action"]
        annotated["source"] = parsed["source"]
        if parsed["source_rule"]:
            annotated["source_rule"] = parsed["source_rule"]
        if parsed["priority"]:
            annotated["priority"] = parsed["priority"]
        tagged.append(annotated)

    logger.info(
        "tag_step_sources: %d step(s) tagged from declared source "
        "(rule_aware=%d catalog=%d llm=%d)",
        len(tagged),
        sum(1 for s in tagged if s["source"] == _SOURCE_RULE_AWARE),
        sum(1 for s in tagged if s["source"] == _SOURCE_CATALOG),
        sum(1 for s in tagged if s["source"] == _SOURCE_LLM),
    )
    return tagged