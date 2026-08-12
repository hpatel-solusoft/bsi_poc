"""
Owns: rule display metadata (numbers, headings, descriptions) and turning
raw subject name parts from a rules_fired instance into a single readable
name — the small presentation concerns rules_fired.py hands off so that
renaming a rule or reformatting a name never touches the query module.

Descriptions are read from config/rule.yaml through rule_registry, NOT
hardcoded here. Display names ARE defined here, because "Rule 13
(FastTrack Recommendation)" is a presentation label, not rule config —
config's own name for it is the longer "FastTrack Escalation
Recommendation", which reads badly at the head of a sentence.

This module previously also built a full investigator-facing narrative
sentence per instance (build_inference / InferenceContext and ~700 lines
of per-rule prose). That narrative was computed on every pipeline run but
never attached to the payload — every call site that would have stored it
(`instance["inference"]`, `entry["inference_summary"]`) was commented out,
so it was pure dead computation. Removed rather than re-wired back in:
if/when an investigator-facing narrative is wanted again, it should be
designed against what the UI actually needs to show, not resurrected from
code that was already disconnected.

Does NOT own: querying (rules_fired.py), rule execution (rule_engine.py),
or rule content (rules/*.cypher).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, Optional

from reasoning_layer import rule_registry

logger = logging.getLogger(__name__)

# Relationship/finding label carried on the block as `relationship_type`.
# This is the graph edge the rule asserted, so a line can be traced back to
# an actual relationship. It is NOT used at the head of the prose any more —
# an investigator reads "Shared Employer", not "SHARES_EMPLOYER_WITH" —
# but it stays on the contract because /rule_audit and the rejection flow
# both key off it.
_RULE_LABELS: Dict[str, str] = {
    "Rule_01_Shared_Employer": "SHARES_EMPLOYER_WITH",
    "Rule_03_Shared_Address": "SHARES_ADDRESS_WITH",
    "Rule_05_Alias_Identity": "SHARES_ALIAS_PATTERN_WITH",
    "Rule_10_Merged_Case_Propagation": "MERGED_CASE_HISTORY",
    "Rule_11_Cross_Case_Hub": "CROSS_CASE_HUB",
    "Rule_02_Employer_Fraud_Network": "EMPLOYER_FRAUD_NETWORK",
    "Rule_04_Address_Fraud_Network": "ADDRESS_FRAUD_NETWORK",
    "Rule_06_Identity_Fraud_Network": "IDENTITY_FRAUD_NETWORK",
    "Rule_07_Prior_Guilty": "HAS_PRIOR_GUILTY_CASE",
    "Rule_08_Recidivist_Escalation": "CASE_RISK_ESCALATION",
    "Rule_09_PCA_CheckSplit": "PCA_CHECKSPLIT_NETWORK",
    "Rule_12_SLAM_Wage_Corroboration": "WAGE_CORROBORATION",
    "Rule_13_FastTrack_Escalation": "FASTTRACK_RECOMMENDATION",
    "Rule_14_Confirmation_Elevation": "NARRATIVE_CORROBORATION",
}

# Short, investigator-facing names used at the head of each narrative.
# Deliberately shorter than config/rule.yaml's `name` field: config names
# describe the rule for an operator tuning it ("High Risk Escalation:
# Recidivist in Active Fraud Network"), these name the finding for someone
# reading a case ("High Risk"). Both are correct for their audience; this
# module owns the reading one.
_RULE_DISPLAY_NAMES: Dict[str, str] = {
    "Rule_01_Shared_Employer": "Shared Employer",
    "Rule_02_Employer_Fraud_Network": "Employer Fraud Network",
    "Rule_03_Shared_Address": "Shared Address",
    "Rule_04_Address_Fraud_Network": "Address Fraud Network",
    "Rule_05_Alias_Identity": "Alias Identity Link",
    "Rule_06_Identity_Fraud_Network": "Identity Fraud Network",
    "Rule_07_Prior_Guilty": "Prior Guilty",
    "Rule_08_Recidivist_Escalation": "High Risk",
    "Rule_09_PCA_CheckSplit": "PCA Check-Split Network",
    "Rule_10_Merged_Case_Propagation": "Merged Case History",
    "Rule_11_Cross_Case_Hub": "Cross-Case Hub",
    "Rule_12_SLAM_Wage_Corroboration": "Wage Corroboration",
    "Rule_13_FastTrack_Escalation": "FastTrack Recommendation",
    "Rule_14_Confirmation_Elevation": "Narrative Corroboration",
}

_RULE_NUMBERS: Dict[str, int] = {rule_id: int(rule_id.split("_")[1]) for rule_id in _RULE_LABELS}


def rule_label(rule_id: str) -> str:
    """The graph relationship type this rule asserts. Unchanged contract —
    /rule_audit and the rejection flow both key off this value."""
    return _RULE_LABELS.get(rule_id, rule_id)


def rule_number(rule_id: str) -> Optional[int]:
    """The rule's numeric identifier (e.g. 3 for Rule_03_...), or None if unknown."""
    return _RULE_NUMBERS.get(rule_id)


def rule_display_name(rule_id: str) -> str:
    """ "FastTrack Recommendation" — the finding's name, for a reader."""
    return _RULE_DISPLAY_NAMES.get(rule_id, rule_id)


def rule_heading(rule_id: str) -> str:
    """ "Rule 13 (FastTrack Recommendation)" — a rule's number plus its
    reading name, used wherever a finding needs to be tied back to a
    numbered rule an investigator can look up or reject.
    """
    number = rule_number(rule_id)
    name = rule_display_name(rule_id)
    return f"Rule {number} ({name})" if number else name


@lru_cache(maxsize=1)
def _descriptions() -> Dict[str, str]:
    """
    Rule descriptions straight from config/rule.yaml.

    Deliberately reads the CONFIG rather than rule_registry.load_registry():
    load_registry opens a Neo4j session to read the seeded :InferenceRule
    nodes, and a rule's description is static config text — needing a live
    graph to render a label would make the whole block degrade to nulls
    during an outage, exactly when an investigator most needs to read it.

    Cached because the config does not change within a process, and this is
    called once per rule per pipeline run.
    """
    try:
        config = rule_registry._load_config()
    except Exception as exc:  # noqa: BLE001 — a display concern must not break the block
        logger.warning("rule descriptions unavailable — %s", exc)
        return {}
    return {
        rule_id: entry.get("description")
        for rule_id, entry in (config.get("rules") or {}).items()
        if isinstance(entry, dict) and entry.get("description")
    }


def rule_description(rule_id: str) -> Optional[str]:
    """The rule's description from config/rule.yaml. None when the config
    has no entry — surfaced as null rather than a filler sentence, so a
    missing description is visible and fixable instead of disguised."""
    return _descriptions().get(rule_id)


def display_name(first_name: Any, last_name: Any, subject_id: Any = None) -> Optional[str]:
    """ "Maria Williams" from the parts the graph holds.

    Falls back to whichever part exists, then to the subject_id. A subject
    with no name on record still needs to be identifiable in the sentence —
    "subject 658653186" is unhelpful but honest, whereas omitting the party
    entirely would make the narrative unreadable.
    """
    parts = [str(p).strip() for p in (first_name, last_name) if p and str(p).strip()]
    if parts:
        return " ".join(parts)
    return str(subject_id).strip() if subject_id else None


def enrich_instance(rule_id: str, instance: Dict[str, Any]) -> Dict[str, Any]:
    """Replace an instance's raw first/last name fields with a single
    display name for the subject and, if present, the related subject."""
    enriched = dict(instance)

    subject_name = display_name(
        enriched.pop("first_name", None),
        enriched.pop("last_name", None),
        enriched.get("subject_id"),
    )
    related_name = display_name(
        enriched.pop("related_first_name", None),
        enriched.pop("related_last_name", None),
        enriched.get("related_subject_id"),
    )
    if subject_name:
        enriched["subject_name"] = subject_name
    if related_name:
        enriched["related_subject_name"] = related_name

    return enriched


def render_block(block: list) -> list:
    """
    Add the rule-level display fields (rule_number, rule_display_name,
    rule_heading) to every entry in an assembled rules_fired block.

    Mutates and returns the same list — rules_fired hands it straight on.
    """
    for entry in block:
        rule_id = entry.get("rule_id")
        entry["rule_number"] = rule_number(rule_id)
        entry["rule_display_name"] = rule_display_name(rule_id)
        entry["rule_heading"] = rule_heading(rule_id)
    return block