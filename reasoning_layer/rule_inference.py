"""
Owns: turning one raw rule match into what an investigator reads — the
rule's description, the subject names involved, and a one-line `inference`
stating why the rule fired against the actual data.

Why this is its own module rather than more code inside rules_fired.py:
rules_fired.py's job is to ask Neo4j what fired and roll it up into the
A.4 contract. Composing "SHARES_ADDRESS_WITH Maria Williams — 244 Elmwood
Avenue Quincy MA 02169 (structural match)" is a presentation job with
entirely different reasons to change: reword a sentence, add a field to a
display, and rules_fired.py should not be touched at all.

Descriptions are read from config/rule.yaml through rule_registry, NOT
hardcoded here. A rule's description belongs beside its enabled flag and
wave, in the one file that already defines the rule — duplicating it in
Python would create exactly the second driftable copy the architecture
guideline warns about.

Every inference line is built from data the query actually returned. When
a field is missing the line degrades to what IS known rather than
inventing a plausible address or employer, because an investigator acting
on "same address" needs that address to be real.

Does NOT own: querying (rules_fired.py), rule execution (rule_engine.py),
or rule content (rules/*.cypher).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, Optional

from reasoning_layer import rule_registry

logger = logging.getLogger(__name__)

# Relationship/finding label shown at the head of each inference line. This
# is the investigator-facing name of what the rule asserted, and matches the
# relationship type the graph actually carries so the line can be traced
# back to an edge.
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


def rule_label(rule_id: str) -> str:
    return _RULE_LABELS.get(rule_id, rule_id)


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
    """"Maria Williams" from the parts the graph holds.

    Falls back to whichever part exists, then to the subject_id. A subject
    with no name on record still needs to be identifiable in the line —
    "SHARES_ADDRESS_WITH 658653186" is unhelpful but honest, whereas
    omitting the party entirely would make the inference unreadable.
    """
    parts = [str(p).strip() for p in (first_name, last_name) if p and str(p).strip()]
    if parts:
        return " ".join(parts)
    return str(subject_id).strip() if subject_id else None


def format_address(detail: Dict[str, Any]) -> Optional[str]:
    """"244 Elmwood Avenue Quincy MA 02169" from the :Address parts."""
    parts = [
        str(detail.get(key)).strip()
        for key in ("street", "city", "state", "zip")
        if detail.get(key) and str(detail.get(key)).strip()
    ]
    return " ".join(parts) if parts else None


def _network_members_phrase(members: Any) -> Optional[str]:
    """
    "Carlos Rivera (BSI-2026-0901, NCP allegation) + Maria Williams
    (BSI-2026-0847, SLAM allegation)" — the members of a detected network
    with the case and allegation that put each of them in it.
    """
    if not members:
        return None
    rendered = []
    for member in members:
        if not isinstance(member, dict):
            continue
        name = display_name(
            member.get("first_name"), member.get("last_name"), member.get("subject_id")
        )
        if not name:
            continue
        context = [
            str(member[key]).strip()
            for key in ("complaint_no", "allegation_type")
            if member.get(key) and str(member[key]).strip()
        ]
        if context:
            allegation = member.get("allegation_type")
            if allegation and str(allegation).strip():
                context[-1] = f"{str(allegation).strip()} allegation"
            rendered.append(f"{name} ({', '.join(context)})")
        else:
            rendered.append(name)
    return " + ".join(rendered) if rendered else None


def build_inference(rule_id: str, instance: Dict[str, Any]) -> Optional[str]:
    """
    One line stating why this rule fired, built from the instance's own
    data. Returns None when nothing beyond the rule name is known — an
    empty inference is better than a sentence asserting a relationship the
    query could not actually evidence.
    """
    label = rule_label(rule_id)
    detail = instance.get("detail") or {}
    other = instance.get("related_subject_name")

    # --- structural subject-to-subject rules ---
    if rule_id == "Rule_03_Shared_Address":
        address = format_address(detail)
        if other and address:
            return f"{label} {other} — {address} (structural match)"
        if other:
            return f"{label} {other} (structural match)"

    if rule_id == "Rule_01_Shared_Employer":
        employer = detail.get("employer_name") or detail.get("fein")
        fein = detail.get("fein")
        if other and employer:
            suffix = f" (FEIN {fein})" if fein and employer != fein else ""
            return f"{label} {other} — {employer}{suffix} (structural match)"
        if other:
            return f"{label} {other} (structural match)"

    if rule_id == "Rule_05_Alias_Identity":
        alias = detail.get("alias_pattern")
        if other and alias:
            return f"{label} {other} — alias pattern '{alias}' (structural match)"
        if other:
            return f"{label} {other} (structural match)"

    # --- fraud networks: WHY these people form a network ---
    if rule_id in (
        "Rule_02_Employer_Fraud_Network", "Rule_04_Address_Fraud_Network",
        "Rule_06_Identity_Fraud_Network", "Rule_09_PCA_CheckSplit",
    ):
        members = _network_members_phrase(detail.get("members"))
        shared = (
            format_address(detail)
            or detail.get("employer_name")
            or detail.get("alias_pattern")
            or instance.get("related_network_key")
        )
        if members:
            anchor = f" at same {'address' if 'Address' in rule_id else 'employer'}" \
                if rule_id in ("Rule_02_Employer_Fraud_Network", "Rule_04_Address_Fraud_Network") else ""
            shared_text = f" {shared}" if shared and anchor else ""
            return (
                f"{label} — {members} both have active allegations"
                f"{anchor}{shared_text} under DIFFERENT cases"
            )
        if shared:
            return f"{label} — network formed on {shared}"

    # --- case-linked rules ---
    if rule_id == "Rule_07_Prior_Guilty":
        case_ref = detail.get("complaint_no") or instance.get("related_case_id")
        closed = detail.get("date_closed")
        if case_ref:
            when = f", closed {closed}" if closed else ""
            return f"{label} {case_ref} — prior case disposed guilty{when}"

    if rule_id == "Rule_10_Merged_Case_Propagation":
        case_ref = detail.get("complaint_no") or instance.get("related_case_id")
        if case_ref:
            return f"{label} — subject history inherited from merged case {case_ref}"

    if rule_id == "Rule_11_Cross_Case_Hub":
        case_ids = detail.get("hub_case_ids") or []
        subject = instance.get("subject_name")
        if subject and case_ids:
            return (
                f"{label} — {subject} appears as co-subject across "
                f"{len(case_ids)} cases ({', '.join(str(c) for c in case_ids)})"
            )

    if rule_id == "Rule_08_Recidivist_Escalation":
        case_ref = detail.get("complaint_no") or instance.get("related_case_id")
        if case_ref:
            return (
                f"{label} {case_ref} — subject has a prior guilty case AND is an "
                f"active member of a detected fraud network"
            )

    if rule_id == "Rule_12_SLAM_Wage_Corroboration":
        allegation = instance.get("allegation_type") or detail.get("allegation_type")
        employer = detail.get("employer_name")
        verified = instance.get("corroborated")
        if allegation:
            source = f" against {employer} wage records" if employer else " against wage records"
            checked = (
                " with the fraud date range verified" if verified
                else " (wage records present, date range not verified)"
            )
            return f"{label} — {allegation} allegation corroborated{source}{checked}"

    if rule_id == "Rule_13_FastTrack_Escalation":
        case_ref = detail.get("complaint_no") or instance.get("related_case_id")
        if case_ref:
            return f"{label} {case_ref} — case meets FastTrack escalation criteria"

    if rule_id == "Rule_14_Confirmation_Elevation":
        confirmed = detail.get("confirmed_relationship")
        if other and confirmed:
            return (
                f"{label} — inferred {confirmed} with {other} independently "
                f"confirmed by case narrative"
            )
        if other:
            return f"{label} — inferred relationship with {other} confirmed by case narrative"

    return None


def enrich_instance(rule_id: str, instance: Dict[str, Any]) -> Dict[str, Any]:
    """Add subject display names and the inference line to one instance."""
    enriched = dict(instance)

    subject_name = display_name(
        enriched.pop("first_name", None), enriched.pop("last_name", None),
        enriched.get("subject_id"),
    )
    related_name = display_name(
        enriched.pop("related_first_name", None), enriched.pop("related_last_name", None),
        enriched.get("related_subject_id"),
    )
    if subject_name:
        enriched["subject_name"] = subject_name
    if related_name:
        enriched["related_subject_name"] = related_name

    inference = build_inference(rule_id, enriched)
    if inference:
        enriched["inference"] = inference
    return enriched