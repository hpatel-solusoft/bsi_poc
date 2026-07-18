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
# came from. The LLM selects steps from two candidate pools (catalogue tasks
# and rule-aware tasks) and may also synthesise its own, but it writes prose
# — it does not return a machine-readable source tag. So the source is
# recovered here by matching the step text back to the candidate pools.
#
# Matching is deterministic and conservative: a step is only attributed to a
# source when its wording substantially overlaps a candidate task. Anything
# else is honestly labelled llm_generated rather than being credited to a
# rule or the catalogue it did not actually come from — a false attribution
# would put BSI's name behind a task BSI never defined.

_SOURCE_RULE_AWARE = "rule_aware"
_SOURCE_CATALOG = "catalog"
_SOURCE_LLM = "llm_generated"

# Fraction of a candidate task's significant words that must appear in the
# step for it to count as the same task. High enough to avoid crediting a
# step that merely shares common verbs ("request", "review").
_MATCH_THRESHOLD = 0.6

_STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "and", "or", "per", "all", "any",
    "with", "from", "on", "in", "by", "case", "cases",
}


def _significant_words(text: str) -> set:
    words = {w.strip(".,;:()[]").lower() for w in str(text or "").split()}
    return {w for w in words if w and w not in _STOPWORDS and len(w) > 2}


def _overlap(step_text: str, task_text: str) -> float:
    task_words = _significant_words(task_text)
    if not task_words:
        return 0.0
    step_words = _significant_words(step_text)
    return len(task_words & step_words) / len(task_words)


def tag_step_sources(
    steps: List[Dict[str, Any]],
    rule_aware_tasks: Optional[List[Dict[str, Any]]] = None,
    catalog_tasks: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Annotate each investigation step with a `source` (AI-16).

    Rule-aware tasks are checked FIRST: when a step matches both pools, the
    rule-derived attribution is the more specific and more useful one for an
    investigator, because it also carries the source_rule that justifies it.

    Returns a new list; the input steps are not mutated.
    """
    rule_aware_tasks = rule_aware_tasks or []
    catalog_tasks = catalog_tasks or []
    tagged: List[Dict[str, Any]] = []

    for step in steps or []:
        annotated = dict(step)
        action = annotated.get("action", "")

        best_rule, best_rule_score = None, 0.0
        for task in rule_aware_tasks:
            score = _overlap(action, task.get("task_type", ""))
            if score > best_rule_score:
                best_rule, best_rule_score = task, score

        best_cat, best_cat_score = None, 0.0
        for task in catalog_tasks:
            score = _overlap(action, task.get("task_type", ""))
            if score > best_cat_score:
                best_cat, best_cat_score = task, score

        if best_rule is not None and best_rule_score >= _MATCH_THRESHOLD:
            annotated["source"] = _SOURCE_RULE_AWARE
            annotated["source_rule"] = best_rule.get("source_rule")
            annotated["priority"] = best_rule.get("priority")
        elif best_cat is not None and best_cat_score >= _MATCH_THRESHOLD:
            annotated["source"] = _SOURCE_CATALOG
        else:
            annotated["source"] = _SOURCE_LLM

        tagged.append(annotated)

    logger.info(
        "tag_step_sources: %d step(s) tagged (rule_aware=%d catalog=%d llm=%d)",
        len(tagged),
        sum(1 for s in tagged if s["source"] == _SOURCE_RULE_AWARE),
        sum(1 for s in tagged if s["source"] == _SOURCE_CATALOG),
        sum(1 for s in tagged if s["source"] == _SOURCE_LLM),
    )
    return tagged