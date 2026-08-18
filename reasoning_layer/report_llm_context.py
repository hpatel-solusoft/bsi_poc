"""
Owns: building the MINIMIZED case context that is actually serialised into
the Report Generation LLM prompt (agent_service.prompt_builders.
build_report_generation_prompt -> config.prompts.REPORT_GENERATION_PROMPT).

Problem this fixes
-------------------
api/server.py's /generate_report route was assembling `case_data_for_prompt`
(a near-complete copy of CS-4's case_data, plus the freshly-computed
related_network / confidence_summary / decision_log) and handing the WHOLE
thing to build_report_generation_prompt, which just does
`json.dumps(case_data, indent=2)` straight into the LLM system prompt. That
blob carried a lot of data the LLM never narrates and that should not leave
the pipeline as a prompt payload:

  * agent_summary_cache          - operational/session cache, not report content
  * provenance_trail             - audit trail, not narrative content
  * network_match_flag           - internal graph-match boolean, not a report fact
  * decision_log                 - the Decision & Override Log section is rendered
                                    entirely deterministically (AI-42,
                                    reasoning_layer.decision_log.build_decision_log)
                                    and spliced into the LLM's markdown after the
                                    fact; the LLM is never asked to write any part
                                    of that section, so it has no use for this list.
  * complaint_intelligence.subjects - every co-subject's full profile, when the
                                    report is scoped to the Primary Subject only
  * risk_assessment              - the full evaluated risk-rule catalogue
                                    (risk_indicators / active_rules), when only
                                    the scores are narrated
  * similar_cases                - the full matched-case list (case_id,
                                    complaint_no, dates, amounts, ...), when
                                    only the count + similarity signal is narrated
  * investigation_plan           - plan metadata (plan_id, evidence_checklist,
                                    escalation_criteria, data_sources,
                                    plan_narrative, ...), when only the steps
                                    are narrated
  * related_network[*].rejection - investigator_id / rejected_at / reason (and
                                    rule_id, already duplicated by the entry's own
                                    source_rule) for every rejected connection. The
                                    Reviewed and Excluded Connections section is
                                    rendered entirely deterministically (AI-40,
                                    reasoning_layer.decision_log.
                                    render_reviewed_and_excluded_markdown) from the
                                    route's own related_network, not from anything
                                    the LLM writes, so this per-rejection audit
                                    detail has no reader in the prompt.
  * rules_fired[*].instances     - per-match detail (subject names, ids, display
                                    chips) for EVERY rule, not only the
                                    prior-guilty ones. The report writes one
                                    summary sentence per rule ("Rule X — Confidence:
                                    Y. ...") and never reads a match's per-instance
                                    detail, so only the rule-level summary fields
                                    (rule_id, heading, description, confidence,
                                    active_count) are kept.
  * prior guilty case lists      - Rule_07_Prior_Guilty's per-instance case
                                    breakdown, the equivalent
                                    HAS_PRIOR_GUILTY_CASE entries in
                                    related_network / network_connections_summary,
                                    AND graph_context.prior_guilty_cases (the
                                    same {case_id, outcome, confidence,
                                    date_closed} list surfaced a second time
                                    via the graph layer) - a count is enough
                                    for the report in every one of these
                                    places.
  * context_enrichment.profiles[*].prior_cases - each profile's full
                                    per-prior-case commentary history
                                    (analyst notes, disposition text, dollar
                                    amounts). This is the heaviest single
                                    section in the payload and a near-verbatim
                                    duplicate of what rules_fired's
                                    Rule_07_Prior_Guilty inference text
                                    already narrates. Each profile carries a
                                    prior_case_count field right alongside
                                    it, so dropping the list loses no
                                    countable information.

This module is the SINGLE place that derives the trimmed prompt context. It
is called from api/services/report_service.py's run_generate_report and
run_generate_report_pdf, immediately before build_report_generation_prompt()
is invoked, on a deep copy of case_data_for_prompt. It touches NOTHING else:

  * The full case_data_for_prompt dict is still what gets persisted to
    report_artifacts and returned to the caller in the route's response --
    unchanged.
  * CASE_STORE / case_ai_summary_store, every other route (/intake,
    /similar_cases, /risk_assessment, /plan, /copilot, /rule_audit, ...),
    and every other consumer of case_data are unaffected -- none of them
    call this function.

Does NOT own: prompt rendering (agent_service/prompt_builders.py), the
Related Network assembly (reasoning_layer/report_generation.py), or the
rules_fired contract (reasoning_layer/rules_fired.py).
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Trim tables — kept as module-level constants so the exact rule is
# grep-able and testable in one place, instead of scattered through the
# function body.
# --------------------------------------------------------------------------

# Top-level case_data keys that never belong in the report LLM prompt.
# decision_log is here (AI-43) alongside the original three: the Decision &
# Override Log section is now always rendered deterministically and spliced
# into the LLM's markdown after the fact (AI-42), so the LLM never reads
# this list at all.
_TOP_LEVEL_KEYS_TO_DROP = ("agent_summary_cache", "provenance_trail", "network_match_flag", "decision_log")

# rules_fired entry fields kept for the report LLM — the rule-level summary
# the "Rules Fired" section narrates one sentence from. Everything else on
# the entry (render, family, corroborated, revertable, total_count, and
# above all "instances" — the per-match names/ids/display chips) is dropped
# by omission (AI-43): the report never writes per-match detail, so the LLM
# never needs to see it. active_count is the rule's live-match count (see
# reasoning_layer.rules_fired_view.build_rule_view) — computed from active
# instances only, matching evidence_count's original "never hand a rejected
# finding to a report/plan/copilot consumer as live evidence" contract.
_RULES_FIRED_SUMMARY_FIELDS: Tuple[str, ...] = (
    "rule_id",
    "heading",
    "description",
    "confidence",
    "active_count",
)

# rule_id prefixes whose "instances" list is a pure prior-guilty-case
# enumeration (one instance per prior case). evidence_count / rejected_count
# on the rule entry itself already carry "how many" — dropping "instances"
# here loses no information the report needs.
_PRIOR_GUILTY_RULE_PREFIXES: Tuple[str, ...] = ("Rule_07_Prior_Guilty",)

# related_network / network_connections_summary relationship types that
# enumerate prior guilty cases one-by-one — same reasoning as above,
# applied to the two other places the same per-case list can appear.
_PRIOR_GUILTY_RELATIONSHIP_TYPES: Tuple[str, ...] = ("HAS_PRIOR_GUILTY_CASE",)

# risk_assessment fields kept when trimming to "scores and related fields,
# not all the risk rules". risk_indicators / active_rules (the per-rule
# breakdown) are dropped by omission.
_RISK_ASSESSMENT_SCORE_FIELDS: Tuple[str, ...] = (
    "subject_id",
    "risk_score",
    "risk_tier",
    "base_risk_score",
    "base_risk_tier",
    "fraud_types",
    "total_points",
    "max_points",
    "neo4j_signals",
    "risk_tier_reason",
)

# Fields kept per similar-case match: the similarity signal + what matched.
# Case-list identifiers (case_id, complaint_no, dates, fraud_amount,
# match_reasons, wf_id, ...) are dropped by omission.
_SIMILAR_CASE_MATCH_FIELDS: Tuple[str, ...] = (
    "similarity_score",
    "matched_allegation_types",
    "allegation_type",
    "summary",
)

# investigation_plan is trimmed to just the steps — plan_id, fraud_types,
# risk_tier, evidence_checklist, escalation_criteria/required, data_sources,
# plan_narrative, and any rule-aware-task breakdown are dropped by omission.
_INVESTIGATION_PLAN_KEPT_FIELDS: Tuple[str, ...] = ("investigation_steps",)


def _trim_rules_fired(rules_fired: Any) -> Any:
    """Collapse every rule entry down to its rule-level summary fields
    (see _RULES_FIRED_SUMMARY_FIELDS) — the per-match `instances` list
    (subject names, ids, display chips) is dropped in full for all 14
    rules, not only the prior-guilty ones (AI-43): the report writes one
    summary sentence per rule and never reads a match's per-instance
    detail."""
    if not isinstance(rules_fired, list):
        return rules_fired

    trimmed: List[Any] = []
    for entry in rules_fired:
        if not isinstance(entry, dict):
            trimmed.append(entry)
            continue
        trimmed.append({k: v for k, v in entry.items() if k in _RULES_FIRED_SUMMARY_FIELDS})
    return trimmed


def _trim_subjects_to_primary(complaint_intelligence: Any) -> Any:
    """
    Keep only the Primary Subject in complaint_intelligence.subjects, and
    drop complaint_intelligence.case_id entirely.

    case_id has no meaning to an investigator (see
    core.case_store.get_complaint_number's docstring for the same
    reasoning stated once, in Python) — complaint_intelligence.summary.
    complaint_no is already present and IS the identifier a human
    recognises, so leaving the raw case_id in the LLM's context serves
    no purpose beyond risking it ending up quoted verbatim in the
    narrative prose (confirmed happening in production: the LLM wrote
    "Case Number 658407434" into the Case Summary section because that
    was the only case-identifying number sitting in its context).
    """
    if not isinstance(complaint_intelligence, dict):
        return complaint_intelligence

    complaint_intelligence = dict(complaint_intelligence)
    complaint_intelligence.pop("case_id", None)

    subjects = complaint_intelligence.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        return complaint_intelligence

    primary = [s for s in subjects if isinstance(s, dict) and s.get("is_primary_subject")]
    # Defensive fallback: if nothing is flagged primary (data gap), keep the
    # first subject rather than silently emptying the list — an unflagged
    # subject list is still one subject's report, not zero.
    complaint_intelligence["subjects"] = primary if primary else subjects[:1]
    return complaint_intelligence


def _trim_risk_assessment(risk_assessment: Any) -> Any:
    if not isinstance(risk_assessment, dict):
        return risk_assessment
    return {k: v for k, v in risk_assessment.items() if k in _RISK_ASSESSMENT_SCORE_FIELDS}


def _trim_similar_cases(similar_cases: Any) -> Any:
    if not isinstance(similar_cases, dict):
        return similar_cases
    matches = similar_cases.get("matches")
    if not isinstance(matches, list):
        return similar_cases

    trimmed_matches = [
        {k: v for k, v in m.items() if k in _SIMILAR_CASE_MATCH_FIELDS}
        for m in matches
        if isinstance(m, dict)
    ]
    count = similar_cases.get("total_candidates_scored", len(matches))
    return {"count": count, "matches": trimmed_matches}


def _trim_investigation_plan(investigation_plan: Any) -> Any:
    if not isinstance(investigation_plan, dict):
        return investigation_plan
    return {k: v for k, v in investigation_plan.items() if k in _INVESTIGATION_PLAN_KEPT_FIELDS}


def _trim_prior_guilty_related_network(related_network: Any) -> Tuple[Any, int]:
    """Collapse HAS_PRIOR_GUILTY_CASE entries in related_network to a count."""
    if not isinstance(related_network, list):
        return related_network, 0
    kept: List[Any] = []
    prior_guilty_count = 0
    for entry in related_network:
        if isinstance(entry, dict) and entry.get("relationship_type") in _PRIOR_GUILTY_RELATIONSHIP_TYPES:
            prior_guilty_count += 1
            continue
        kept.append(entry)
    return kept, prior_guilty_count


def _trim_related_network_rejection_detail(related_network: Any) -> Any:
    """Drop every entry's `rejection` block (investigator_id, rejected_at,
    reason, rule_id — reasoning_layer.report_generation.assemble_related_network)
    entirely (AI-43). The Reviewed and Excluded Connections section is
    rendered deterministically from the route's own related_network, never
    narrated by the LLM (AI-40), so this per-rejection audit detail has no
    reader in the prompt; entries themselves (and their `status`) are kept
    unchanged."""
    if not isinstance(related_network, list):
        return related_network
    trimmed: List[Any] = []
    for entry in related_network:
        if isinstance(entry, dict) and "rejection" in entry:
            entry = dict(entry)
            entry.pop("rejection", None)
        trimmed.append(entry)
    return trimmed


def _trim_graph_context_prior_guilty(graph_context: Any) -> Any:
    """Collapse graph_context.prior_guilty_cases (the {case_id, outcome,
    confidence, date_closed} list) to a count. Same reasoning as the
    related_network / network_connections_summary collapse — this is the
    graph layer's copy of the same per-case enumeration."""
    if not isinstance(graph_context, dict):
        return graph_context
    cases = graph_context.get("prior_guilty_cases")
    if not isinstance(cases, list):
        return graph_context

    graph_context = dict(graph_context)
    graph_context.pop("prior_guilty_cases", None)
    graph_context["prior_guilty_case_count"] = len(cases)
    return graph_context


def _trim_context_enrichment_prior_cases(context_enrichment: Any) -> Any:
    """Drop each profile's full prior_cases commentary history —
    prior_case_count (kept) already carries the countable fact, and the
    commentary text duplicates rules_fired's Rule_07_Prior_Guilty
    narration."""
    if not isinstance(context_enrichment, dict):
        return context_enrichment
    profiles = context_enrichment.get("profiles")
    if not isinstance(profiles, list):
        return context_enrichment

    new_profiles: List[Any] = []
    for profile in profiles:
        if isinstance(profile, dict) and "prior_cases" in profile:
            profile = dict(profile)
            profile.pop("prior_cases", None)
        new_profiles.append(profile)

    context_enrichment = dict(context_enrichment)
    context_enrichment["profiles"] = new_profiles
    return context_enrichment


def _trim_network_connections_summary(summary: Any) -> Any:
    """Drop the per-case `members` list for prior-guilty rows; count stays."""
    if not isinstance(summary, list):
        return summary
    trimmed = []
    for entry in summary:
        if isinstance(entry, dict) and str(entry.get("source_rule") or "").startswith(
            _PRIOR_GUILTY_RULE_PREFIXES
        ):
            entry = dict(entry)
            entry.pop("members", None)
        trimmed.append(entry)
    return trimmed


def build_report_llm_context(case_data: Dict[str, Any], *, case_id: str = "") -> Dict[str, Any]:
    """
    Return a MINIMIZED, LLM-prompt-ready copy of case_data — the report
    narrates from this, not from the full case record.

    This function is pure: it deep-copies its input and never mutates the
    caller's dict, so the caller is always free to keep using its own
    (untrimmed) case_data / case_data_for_prompt for persistence, the HTTP
    response, or anything else.

    Trims applied (see module docstring for the full reasoning):
      1. Drops agent_summary_cache, provenance_trail, network_match_flag,
         decision_log.
      2. complaint_intelligence.subjects -> Primary Subject only, and
         complaint_intelligence.case_id is dropped entirely (the LLM never
         sees the raw case_id — only complaint_no, via complaint_intelligence.
         summary.complaint_no, which is already present).
      3. related_network[*] -> drops the "rejection" block
         (investigator_id / rejected_at / reason / rule_id) entirely.
      4. rules_fired entries, for ALL 14 rules -> collapsed to rule-level
         summary fields only (rule_id, heading, description, confidence,
         active_count); the per-match "instances" list is dropped in full.
      5. risk_assessment -> scores + related fields only, not the full
         evaluated risk-rule list.
      6. similar_cases -> count + similarity_score + matched allegation
         type/summary only, not the full case list.
      7. investigation_plan -> investigation_steps only.
      8. related_network / network_connections_summary -> prior-guilty
         per-case entries collapsed to a single count.
      9. graph_context.prior_guilty_cases -> collapsed to
         graph_context.prior_guilty_case_count (same per-case list, surfaced
         a second time via the graph layer).
      10. context_enrichment.profiles[*].prior_cases -> dropped entirely;
          each profile's prior_case_count (already present) is kept.

    Args:
        case_data: the (already assembled) context that would otherwise be
            serialised whole into the Report Generation prompt.
        case_id: used only for logging/traceability.

    Returns:
        A new dict — safe to pass straight into
        agent_service.prompt_builders.build_report_generation_prompt.
    """
    if not isinstance(case_data, dict):
        logger.warning(
            "build_report_llm_context: case_id=%s received non-dict case_data (%s) — " "returning unchanged",
            case_id,
            type(case_data).__name__,
        )
        return case_data

    context = copy.deepcopy(case_data)
    before_keys = sorted(context.keys())
    before_bytes = len(json.dumps(context, default=str))

    # 1. Drop operational/audit/internal-flag sections outright.
    for key in _TOP_LEVEL_KEYS_TO_DROP:
        context.pop(key, None)

    # 2. complaint_intelligence.subjects -> Primary Subject only; case_id dropped.
    if "complaint_intelligence" in context:
        context["complaint_intelligence"] = _trim_subjects_to_primary(context["complaint_intelligence"])

    # 3. related_network[*].rejection -> dropped entirely.
    if "related_network" in context:
        context["related_network"] = _trim_related_network_rejection_detail(context["related_network"])

    # 4. rules_fired -> rule-level summary fields only, for all 14 rules.
    if "rules_fired" in context:
        context["rules_fired"] = _trim_rules_fired(context["rules_fired"])

    # 5. risk_assessment -> scores only.
    if "risk_assessment" in context:
        context["risk_assessment"] = _trim_risk_assessment(context["risk_assessment"])

    # 6. similar_cases -> count + similarity signal only.
    if "similar_cases" in context:
        context["similar_cases"] = _trim_similar_cases(context["similar_cases"])

    # 7. investigation_plan -> steps only.
    if "investigation_plan" in context:
        context["investigation_plan"] = _trim_investigation_plan(context["investigation_plan"])

    # 8. prior-guilty case lists -> count only, wherever they appear.
    prior_guilty_count = 0
    if "related_network" in context:
        context["related_network"], rn_count = _trim_prior_guilty_related_network(context["related_network"])
        prior_guilty_count += rn_count
    if "network_connections_summary" in context:
        context["network_connections_summary"] = _trim_network_connections_summary(
            context["network_connections_summary"]
        )
    if prior_guilty_count:
        context["prior_guilty_case_count"] = prior_guilty_count

    # 9. graph_context.prior_guilty_cases -> count only (same list, surfaced
    # a second time via the graph layer).
    if "graph_context" in context:
        context["graph_context"] = _trim_graph_context_prior_guilty(context["graph_context"])

    # 10. context_enrichment.profiles[*].prior_cases -> dropped; each
    # profile's prior_case_count is kept.
    if "context_enrichment" in context:
        context["context_enrichment"] = _trim_context_enrichment_prior_cases(context["context_enrichment"])

    after_keys = sorted(context.keys())
    after_bytes = len(json.dumps(context, default=str))
    reduction_pct = (100.0 * (before_bytes - after_bytes) / before_bytes) if before_bytes else 0.0

    logger.info(
        "build_report_llm_context: case_id=%s before_keys=%s after_keys=%s "
        "before_bytes=%d after_bytes=%d reduction=%.1f%%",
        case_id,
        before_keys,
        after_keys,
        before_bytes,
        after_bytes,
        reduction_pct,
    )

    # Explicit debug print of the exact JSON that will be serialised into
    # the LLM prompt, plus the full before/after diagnostics — requested
    # so the report-generation input is always visible/inspectable, not
    # just logged at DEBUG level (which is off by default in most deploys).
    print("=" * 100)
    print(f"[report_llm_context] case_id={case_id or 'unknown'}")
    print(f"[report_llm_context] BEFORE keys ({len(before_keys)}): {before_keys}")
    print(f"[report_llm_context] AFTER  keys ({len(after_keys)}): {after_keys}")
    print(
        f"[report_llm_context] BEFORE size: {before_bytes} bytes | AFTER size: {after_bytes} bytes "
        f"| reduction: {reduction_pct:.1f}%"
    )
    print("[report_llm_context] FINAL JSON going to the LLM prompt:")
    print(json.dumps(context, indent=2, default=str))
    print("=" * 100)

    return context