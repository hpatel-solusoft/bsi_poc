"""
Owns: turning raw rule matches into what an investigator reads — the
rule's description, the subject names involved, and a plain-English
`inference` narrative stating what fired, on what evidence, how it was
established, and what the system did as a result.

Why this is its own module rather than more code inside rules_fired.py:
rules_fired.py's job is to ask Neo4j what fired and roll it up into the
A.4 contract. Composing "Rule 1 (Shared Employer): John Smith and Kevin
Nunes both hold employment records with BrightPath Home Health LLC — the
same FEIN 04-7821334..." is a presentation job with entirely different
reasons to change: reword a sentence, add a field to a display, and
rules_fired.py should not be touched at all. No Cypher changes anywhere
in this file's blast radius — every value it renders is already returned
by the existing queries.

THE NARRATIVE HAS THREE PARTS, ALWAYS IN THIS ORDER:

  1. FINDING     — who or what matched, with the concrete values behind
                   it (the employer and its FEIN, the address, the
                   amount against the threshold). An investigator has to
                   be able to check the claim, which means seeing what
                   was matched, not just that something was.
  2. BASIS       — how it was established: a structural match on the case
                   record, an attribution read out of narrative, and
                   whether investigator commentary independently
                   confirmed it. "Confirmed by commentary" and "not yet
                   confirmed" are different evidentiary positions and the
                   line must never blur them.
  3. CONSEQUENCE — what the system did with it: a network formed, a
                   subject flagged, a recommendation raised. For anything
                   that is a RECOMMENDATION rather than a finding, the
                   line says so explicitly and hands the decision back to
                   the investigator. The system does not escalate cases;
                   it proposes, and a person decides.

CROSS-RULE CONTEXT. Some findings are only meaningful in terms of other
findings — Rule 8 is precisely "Rule 7 AND a network rule", and saying so
by name is the difference between an investigator understanding the
escalation and having to reverse-engineer it. `InferenceContext` is built
from the assembled rules_fired block AFTER all fourteen queries have run,
so those references cost no extra round trip and no new Cypher. A rule
whose partner rule did not fire simply drops that clause rather than
asserting a link that is not there.

Descriptions are read from config/rule.yaml through rule_registry, NOT
hardcoded here. Display names ARE defined here, because "Rule 13
(FastTrack Recommendation)" is a presentation label, not rule config —
config's own name for it is the longer "FastTrack Escalation
Recommendation", which reads badly at the head of a sentence.

Every line is built from data the query actually returned. When a field
is missing the sentence degrades to what IS known rather than inventing a
plausible address or employer, because an investigator acting on "same
address" needs that address to be real.

Does NOT own: querying (rules_fired.py), rule execution (rule_engine.py),
or rule content (rules/*.cypher).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence

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

# The plain-English name of the network each network-forming rule creates,
# used both in that rule's own narrative and in the CONSEQUENCE clause of
# the structural rule that fed it ("An Employer Fraud Network has been
# formed between the two subjects").
_NETWORK_NAMES: Dict[str, str] = {
    "Rule_02_Employer_Fraud_Network": "Employer Fraud Network",
    "Rule_04_Address_Fraud_Network": "Address Fraud Network",
    "Rule_06_Identity_Fraud_Network": "Identity Fraud Network",
    "Rule_09_PCA_CheckSplit": "PCA Check-Split Network",
}

# Which structural rule feeds which network rule. Used only to add the
# CONSEQUENCE clause to the structural rule's narrative, and only when the
# network rule ACTUALLY fired for the same pair — never as an assumption
# that a structural match will become a network.
_STRUCTURAL_TO_NETWORK: Dict[str, str] = {
    "Rule_01_Shared_Employer": "Rule_02_Employer_Fraud_Network",
    "Rule_03_Shared_Address": "Rule_04_Address_Fraud_Network",
    "Rule_05_Alias_Identity": "Rule_06_Identity_Fraud_Network",
}

_RULE_NUMBERS: Dict[str, int] = {
    rule_id: int(rule_id.split("_")[1]) for rule_id in _RULE_LABELS
}


def rule_label(rule_id: str) -> str:
    """The graph relationship type this rule asserts. Unchanged contract —
    /rule_audit and the rejection flow both key off this value."""
    return _RULE_LABELS.get(rule_id, rule_id)


def rule_number(rule_id: str) -> Optional[int]:
    return _RULE_NUMBERS.get(rule_id)


def rule_display_name(rule_id: str) -> str:
    """"FastTrack Recommendation" — the finding's name, for a reader."""
    return _RULE_DISPLAY_NAMES.get(rule_id, rule_id)


def rule_heading(rule_id: str) -> str:
    """"Rule 13 (FastTrack Recommendation)" — the head of every narrative.

    Keeping the rule NUMBER visible matters: investigators and the audit
    trail refer to findings by number, and a narrative an investigator
    cannot tie back to a numbered rule cannot be challenged or rejected
    through /reject_inference.
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


@lru_cache(maxsize=1)
def _default_params() -> Dict[str, Dict[str, Any]]:
    """
    Default rule params from config/rule.yaml, for the narratives that need
    to quote a threshold back to the reader.

    Config, not the live :InferenceRule node, for the same reason as
    _descriptions: rendering a sentence must not depend on a Neo4j session.
    The trade is real and worth naming — an operator who retunes
    fasttrack_fraud_threshold on the live node changes what the rule DOES
    immediately, while this line keeps quoting the config default until the
    next deploy. The threshold is therefore rendered from the case's own
    recorded values wherever possible, and the config figure is a fallback
    for the sentence only, never for the decision.
    """
    try:
        config = rule_registry._load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("rule params unavailable for narrative rendering — %s", exc)
        return {}
    return {
        rule_id: dict(entry.get("params") or {})
        for rule_id, entry in (config.get("rules") or {}).items()
        if isinstance(entry, dict)
    }


def rule_description(rule_id: str) -> Optional[str]:
    """The rule's description from config/rule.yaml. None when the config
    has no entry — surfaced as null rather than a filler sentence, so a
    missing description is visible and fixable instead of disguised."""
    return _descriptions().get(rule_id)


# ======================================================================
# Small formatting helpers
# ======================================================================

def display_name(first_name: Any, last_name: Any, subject_id: Any = None) -> Optional[str]:
    """"Maria Williams" from the parts the graph holds.

    Falls back to whichever part exists, then to the subject_id. A subject
    with no name on record still needs to be identifiable in the sentence —
    "subject 658653186" is unhelpful but honest, whereas omitting the party
    entirely would make the narrative unreadable.
    """
    parts = [str(p).strip() for p in (first_name, last_name) if p and str(p).strip()]
    if parts:
        return " ".join(parts)
    return str(subject_id).strip() if subject_id else None


def format_address(detail: Dict[str, Any]) -> Optional[str]:
    """"244 Elmwood Avenue, Quincy MA 02169" from the :Address parts."""
    street = str(detail.get("street")).strip() if detail.get("street") else None
    locality = " ".join(
        str(detail.get(key)).strip()
        for key in ("city", "state", "zip")
        if detail.get(key) and str(detail.get(key)).strip()
    )
    if street and locality:
        return f"{street}, {locality}"
    return street or locality or None


def format_money(value: Any) -> Optional[str]:
    """"$51,550" — whole dollars, or cents where they are actually present.

    Fraud amounts are quoted back to investigators and compared against a
    threshold in the same sentence, so the two must be formatted
    identically; a bare 51550.0 next to "$50,000" reads as two different
    kinds of number.
    """
    if value is None or value == "":
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount == int(amount):
        return f"${int(amount):,}"
    return f"${amount:,.2f}"


def _article(noun: str) -> str:
    """"an Employer Fraud Network" / "a PCA Check-Split Network".

    Worth a helper rather than an inline check: the network name is config-
    driven and appears in four different sentences, and "a Employer Fraud
    Network" in an investigator-facing document reads as a system that was
    not finished.
    """
    return "an" if noun and noun[0].upper() in "AEIOU" else "a"


def _sentence_case(text: str) -> str:
    """Upper-case the first character only.

    Not str.capitalize(), which lower-cases everything after it and turned
    "the subject has a prior Guilty disposition (Rule 7)" into
    "...prior guilty disposition (rule 7)" — quietly destroying both the
    disposition term and the rule reference an investigator needs to follow.
    """
    return text[:1].upper() + text[1:] if text else text


def _join(parts: Sequence[Optional[str]]) -> str:
    """Join sentence fragments, dropping the ones that had no data."""
    return " ".join(part.strip() for part in parts if part and part.strip())


def _oxford(items: Sequence[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    # Serial comma on three or more. These lists are read as the reasons a
    # case was escalated, and "A, B and C" invites reading the last two as
    # one condition.
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _corroboration_clause(instance: Dict[str, Any], basis: str) -> str:
    """
    The BASIS sentence. `basis` names how the fact was established
    ("structurally, from the case record"); this adds whether investigator
    commentary independently confirmed it.

    The uncorroborated wording is deliberately neutral — "has not been
    corroborated" is a statement about the evidence, not a hedge on the
    finding. A structural match on a FEIN is a fact whether or not anyone
    wrote about it, and the sentence must not imply otherwise.
    """
    if instance.get("corroborated"):
        return (
            f"The system matched this {basis}, and confirmed it against "
            "investigator commentary."
        )
    return (
        f"The system matched this {basis}. It has not been corroborated by "
        "investigator commentary."
    )


def _confidence_clause(instance: Dict[str, Any]) -> Optional[str]:
    confidence = instance.get("confidence")
    if not confidence or confidence == "Unresolved":
        return (
            "Confidence is unresolved — the evidence supports more than one reading."
            if confidence == "Unresolved" else None
        )
    return f"Confidence: {confidence}."


def _network_members_phrase(members: Any) -> Optional[str]:
    """
    "Carlos Rivera (BSI-2026-0901, NCP allegation) and Maria Williams
    (BSI-2026-0847, SLAM allegation)" — the members of a detected network
    with the case and allegation that put each of them in it.
    """
    if not members:
        return None
    rendered: List[str] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        name = display_name(
            member.get("first_name"), member.get("last_name"), member.get("subject_id")
        )
        if not name:
            continue
        context: List[str] = []
        if member.get("complaint_no") and str(member["complaint_no"]).strip():
            context.append(f"case {str(member['complaint_no']).strip()}")
        if member.get("allegation_type") and str(member["allegation_type"]).strip():
            context.append(f"{str(member['allegation_type']).strip()} allegation")
        rendered.append(f"{name} ({', '.join(context)})" if context else name)
    return _oxford(rendered) if rendered else None


# ======================================================================
# Cross-rule context
# ======================================================================

class InferenceContext:
    """
    What the OTHER rules found, indexed for the narratives that need it.

    Built once per pipeline run from the assembled rules_fired block, so
    every cross-rule reference below costs zero extra queries and requires
    no change to any .cypher file. Rules 8 and 13 in particular are defined
    in terms of other rules — Rule 8 is literally "Rule 7 AND a network
    rule" — and their own queries return only the case they escalated, with
    no way to name the findings that caused it.

    A missing partner finding is simply absent from the index, and the
    clause that would have cited it is dropped. Nothing here ever asserts a
    link the block does not contain.
    """

    def __init__(self, block: Optional[Iterable[Dict[str, Any]]] = None):
        self.fired_rules: set = set()
        self.subject_names: Dict[str, str] = {}
        self.prior_guilty: Dict[str, List[Dict[str, Any]]] = {}
        self.networks: Dict[str, List[Dict[str, Any]]] = {}
        self.hub_cases: Dict[str, List[Any]] = {}
        for entry in block or []:
            self._index_entry(entry)

    def _index_entry(self, entry: Dict[str, Any]) -> None:
        rule_id = entry.get("rule_id")
        if not entry.get("fired"):
            return
        self.fired_rules.add(rule_id)
        for instance in entry.get("instances") or []:
            # Names are indexed from rejected instances too — a rejected
            # finding still has to render the people in it, or the revert
            # control sits next to an unreadable line.
            self._index_names(instance)
            # Everything BELOW this point is cross-rule evidence: Rule 8
            # citing Rule 7's prior-guilty finding, Rule 1 citing the network
            # Rule 2 formed. A rejected finding must never become another
            # rule's supporting clause — that would launder it back into a
            # live narrative through the side door the investigator just shut.
            if instance.get("status", "active") != "active":
                continue
            if rule_id == "Rule_07_Prior_Guilty":
                subject_id = instance.get("subject_id")
                detail = instance.get("detail") or {}
                if subject_id:
                    self.prior_guilty.setdefault(subject_id, []).append({
                        "case_ref": detail.get("complaint_no") or instance.get("related_case_id"),
                        "outcome": detail.get("outcome"),
                        "date_closed": detail.get("date_closed"),
                    })
            elif rule_id in _NETWORK_NAMES:
                detail = instance.get("detail") or {}
                record = {
                    "rule_id": rule_id,
                    "network_name": _NETWORK_NAMES[rule_id],
                    "network_key": instance.get("related_network_key") or detail.get("network_key"),
                    "member_ids": [
                        m.get("subject_id") for m in (detail.get("members") or [])
                        if isinstance(m, dict) and m.get("subject_id")
                    ],
                }
                # Indexed against EVERY member, not just the anchor subject
                # the query happened to return. Rule 8 asks "is THIS subject
                # in a network", and the anchor is an artifact of the query
                # plan, not an answer to that question.
                for member_id in record["member_ids"] or [instance.get("subject_id")]:
                    if member_id:
                        self.networks.setdefault(member_id, []).append(record)
            elif rule_id == "Rule_11_Cross_Case_Hub":
                subject_id = instance.get("subject_id")
                cases = (instance.get("detail") or {}).get("hub_case_ids") or []
                if subject_id and cases:
                    self.hub_cases[subject_id] = list(cases)

    def _index_names(self, instance: Dict[str, Any]) -> None:
        if instance.get("subject_id") and instance.get("subject_name"):
            self.subject_names.setdefault(instance["subject_id"], instance["subject_name"])
        if instance.get("related_subject_id") and instance.get("related_subject_name"):
            self.subject_names.setdefault(
                instance["related_subject_id"], instance["related_subject_name"]
            )
        for member in (instance.get("detail") or {}).get("members") or []:
            if isinstance(member, dict) and member.get("subject_id"):
                name = display_name(member.get("first_name"), member.get("last_name"))
                if name:
                    self.subject_names.setdefault(member["subject_id"], name)

    # --- lookups used by the narratives ---

    def name_for(self, subject_id: Any) -> Optional[str]:
        return self.subject_names.get(subject_id) if subject_id else None

    def subjects_with_prior_guilty(self) -> List[str]:
        return sorted(self.prior_guilty)

    def networks_for(self, subject_id: Any) -> List[Dict[str, Any]]:
        return self.networks.get(subject_id, []) if subject_id else []

    def shared_network(self, subject_id: Any, other_subject_id: Any,
                       rule_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        The network BOTH subjects belong to, optionally restricted to the
        one a given rule formed. This is what lets Rule 1's narrative end
        with "An Employer Fraud Network has been formed between the two
        subjects" — and only when Rule 2 genuinely put both of them in one.
        """
        if not subject_id or not other_subject_id:
            return None
        for record in self.networks_for(subject_id):
            if rule_id and record["rule_id"] != rule_id:
                continue
            if other_subject_id in record["member_ids"]:
                return record
        return None

    def recidivist_subjects(self) -> List[Dict[str, Any]]:
        """
        Subjects who are BOTH prior-guilty and in an active network — the
        exact population Rule 8 escalates on, reconstructed so Rule 8's
        narrative can name them and cite the two rules that produced them.
        """
        found: List[Dict[str, Any]] = []
        for subject_id, priors in self.prior_guilty.items():
            networks = self.networks_for(subject_id)
            if not networks:
                continue
            found.append({
                "subject_id": subject_id,
                "subject_name": self.name_for(subject_id) or str(subject_id),
                "priors": priors,
                "networks": networks,
            })
        return found


_EMPTY_CONTEXT = InferenceContext()


# ======================================================================
# Per-rule narratives
# ======================================================================

def _both_parties(instance: Dict[str, Any]) -> Optional[str]:
    subject = instance.get("subject_name")
    other = instance.get("related_subject_name")
    if subject and other:
        return f"{subject} and {other}"
    return subject or other


def _network_consequence(rule_id: str, instance: Dict[str, Any],
                         context: InferenceContext) -> Optional[str]:
    """
    The CONSEQUENCE clause on a structural rule: did this structural match
    actually become a network? Only rendered when the corresponding network
    rule fired for these same two subjects. A structural match that did not
    form a network says nothing, rather than implying one is coming.
    """
    network_rule = _STRUCTURAL_TO_NETWORK.get(rule_id)
    if not network_rule:
        return None
    record = context.shared_network(
        instance.get("subject_id"), instance.get("related_subject_id"), network_rule,
    )
    if not record:
        return None
    article = _sentence_case(_article(record["network_name"]))
    return f"{article} {record['network_name']} has been formed between the two subjects."


def _structural_narrative(rule_id: str, instance: Dict[str, Any],
                          context: InferenceContext) -> Optional[str]:
    parties = _both_parties(instance)
    if not parties:
        return None
    detail = instance.get("detail") or {}

    if rule_id == "Rule_01_Shared_Employer":
        employer = detail.get("employer_name")
        fein = detail.get("fein")
        if employer and fein:
            finding = (
                f"{parties} both hold employment records with {employer} — "
                f"the same FEIN {fein}."
            )
        elif employer:
            finding = (
                f"{parties} both hold employment records with {employer}. "
                "No FEIN is on record for that employer, so the match is on "
                "employer identity rather than tax id."
            )
        elif fein:
            finding = f"{parties} both hold employment records under FEIN {fein}."
        else:
            finding = f"{parties} are on record with the same employer."
        basis = "structurally from the employment records on file"

    elif rule_id == "Rule_03_Shared_Address":
        address = format_address(detail)
        finding = (
            f"{parties} are both on record at {address} — the same normalised address."
            if address else
            f"{parties} are both on record at the same normalised address."
        )
        basis = "structurally from the addresses on file"

    elif rule_id == "Rule_05_Alias_Identity":
        alias = detail.get("alias_pattern")
        finding = (
            f"{parties} are linked by the alias \"{alias}\", held by both subjects."
            if alias else
            f"{parties} are linked by a shared alias."
        )
        basis = "structurally from the alias records on file"

    else:
        return None

    return _join([
        finding,
        _corroboration_clause(instance, basis),
        _network_consequence(rule_id, instance, context),
    ])


def _network_narrative(rule_id: str, instance: Dict[str, Any],
                       context: InferenceContext) -> Optional[str]:
    detail = instance.get("detail") or {}
    network_name = _NETWORK_NAMES.get(rule_id, "fraud network")
    members = _network_members_phrase(detail.get("members"))
    network_key = instance.get("related_network_key") or detail.get("network_key")

    article = _sentence_case(_article(network_name))
    member_count = len([
        m for m in (detail.get("members") or []) if isinstance(m, dict) and m.get("subject_id")
    ])
    if members and member_count == 1:
        # One member is a real and reportable state — the second member can
        # sit outside this run's scope. "Its members are Ann Lee" is not, and
        # an investigator reading it assumes data is missing.
        finding = (
            f"{article} {network_name} has been formed. The member in scope for "
            f"this case is {members}."
        )
    elif members:
        finding = f"{article} {network_name} has been formed. Its members are {members}."
    elif network_key:
        finding = f"{article} {network_name} has been formed around {network_key}."
    else:
        return None

    # WHY these people are a network — the shared attribute, stated
    # explicitly. "A network was formed" without the basis is an assertion
    # an investigator can neither verify nor reject.
    if rule_id == "Rule_02_Employer_Fraud_Network":
        basis_detail = (
            "Members each hold an active allegation on a different case and share "
            "the same employer."
        )
    elif rule_id == "Rule_04_Address_Fraud_Network":
        basis_detail = (
            "Members each hold an active allegation on a different case and share "
            "the same address."
        )
    elif rule_id == "Rule_06_Identity_Fraud_Network":
        basis_detail = (
            "Members each hold an active allegation on a different case and are "
            "linked by a shared alias pattern."
        )
    else:  # Rule 9
        basis_detail = (
            "Members appear together as co-subjects on a case carrying a "
            "check-splitting allegation, and appear on the same employer's payment "
            "records — the check-splitting signature."
        )

    corroboration = (
        "The network is corroborated by investigator commentary."
        if instance.get("corroborated")
        else "The network has not been corroborated by investigator commentary."
    )
    return _join([finding, basis_detail, corroboration, _confidence_clause(instance)])


def _case_narrative(rule_id: str, instance: Dict[str, Any],
                    context: InferenceContext) -> Optional[str]:
    detail = instance.get("detail") or {}
    subject = instance.get("subject_name")
    case_ref = detail.get("complaint_no") or instance.get("related_case_id")

    if rule_id == "Rule_07_Prior_Guilty":
        if not case_ref:
            return None
        outcome = str(detail.get("outcome")).strip() if detail.get("outcome") else "Guilty"
        closed = detail.get("date_closed")
        who = subject or "The subject"
        when = f", closed {closed}" if closed else ""
        return _join([
            f"{who} has a prior case ({case_ref}) disposed as {outcome}{when}.",
            "The system read this from the closed case record on file, not from narrative.",
            "The prior disposition is a matter of record and carries into the risk "
            "assessment for the active case.",
        ])

    if rule_id == "Rule_10_Merged_Case_Propagation":
        if not case_ref:
            return None
        who = subject or "The subject"
        return _join([
            f"{who}'s history has been inherited from case {case_ref}, which was "
            "merged into this case.",
            "The system propagated the subject's involvement forward along the "
            "recorded merge, so the merged case's history now reads as part of this one.",
            "The originating case is recorded on the link, so the inherited records "
            "remain distinguishable from those raised on this case directly.",
        ])

    if rule_id == "Rule_11_Cross_Case_Hub":
        case_ids = detail.get("hub_case_ids") or []
        if not subject or not case_ids:
            return None
        listed = ", ".join(str(c) for c in case_ids)
        return _join([
            f"{subject} appears as a co-subject across {len(case_ids)} separate "
            f"cases ({listed}).",
            "The system flagged this subject as a cross-case hub — a subject "
            "recurring across otherwise unconnected investigations.",
            "Recurrence alone is not a finding of wrongdoing; it is a pattern the "
            "investigator should account for.",
        ])

    if rule_id == "Rule_08_Recidivist_Escalation":
        return _recidivist_narrative(instance, context, case_ref)

    if rule_id == "Rule_13_FastTrack_Escalation":
        return _fasttrack_narrative(instance, context, case_ref)

    return None


def _recidivist_narrative(instance: Dict[str, Any], context: InferenceContext,
                          case_ref: Any) -> Optional[str]:
    """
    Rule 8 escalates a CASE, so its own query returns no subject — but the
    escalation is meaningless without naming the person and the two findings
    that combined to trigger it. Both come from the cross-rule context, and
    both cite their rule by number so the investigator can go and read them.
    """
    recidivists = context.recidivist_subjects()
    if recidivists:
        clauses: List[str] = []
        for record in recidivists:
            prior = record["priors"][0] if record["priors"] else {}
            prior_ref = prior.get("case_ref")
            prior_text = (
                f"a prior Guilty case ({prior_ref}, Rule 7)" if prior_ref
                else "a prior Guilty case (Rule 7)"
            )
            network = record["networks"][0]
            network_text = (
                f"a member of {_article(network['network_name'])} {network['network_name']}"
            )
            network_rule = rule_number(network["rule_id"])
            network_text += f" (Rule {network_rule})" if network_rule else ""
            clauses.append(f"{record['subject_name']} has {prior_text} and is {network_text}")
        finding = f"{_oxford(clauses)}."
    else:
        # The partner findings are outside this run's scope (a different
        # subject's pipeline wrote them). State the escalation without
        # inventing the names behind it.
        finding = (
            "A subject on this case has both a prior Guilty case and active "
            "fraud network membership."
        )

    case_text = f" on case {case_ref}" if case_ref else ""
    return _join([
        finding,
        f"Combined with the active allegations{case_text}, the system has flagged "
        "this subject as a recidivist within an active fraud network.",
        "This raises the case's risk position; it does not itself determine the outcome.",
        _confidence_clause(instance),
    ])


def _fasttrack_narrative(instance: Dict[str, Any], context: InferenceContext,
                         case_ref: Any) -> Optional[str]:
    """
    Rule 13 produces a RECOMMENDATION, and the wording has to keep that
    distinction visible. Every other narrative reports what the system
    found; this one reports what the system proposes, and says plainly that
    the decision is the investigator's. That sentence is not boilerplate —
    a FastTrack designation changes how a case is worked, and an automated
    system must not appear to have made that call.
    """
    detail = instance.get("detail") or {}
    amount = format_money(detail.get("fraud_amount"))
    threshold = format_money(
        _default_params().get("Rule_13_FastTrack_Escalation", {}).get("fasttrack_fraud_threshold")
    )

    reasons: List[str] = []
    if amount and threshold:
        reasons.append(f"fraud exceeds the {threshold} threshold ({amount})")
    elif amount:
        reasons.append(f"the recorded fraud amount is {amount}")
    elif threshold:
        reasons.append(f"fraud exceeds the {threshold} threshold")

    priors = context.subjects_with_prior_guilty()
    if priors:
        named = _oxford([context.name_for(sid) or str(sid) for sid in priors])
        reasons.append(f"{named} has a prior Guilty disposition (Rule 7)")
    reasons.append("the case is not currently designated FastTrack")

    case_text = f" case {case_ref}" if case_ref else " this case"
    return _join([
        f"The system has recommended{case_text} for FastTrack escalation.",
        f"{_sentence_case(_oxford(reasons))}.",
        "This is a recommendation generated by the system — the investigator "
        "decides whether to act on it.",
        _confidence_clause(instance),
    ])


def _wage_corroboration_narrative(instance: Dict[str, Any],
                                  context: InferenceContext) -> Optional[str]:
    detail = instance.get("detail") or {}
    allegation = instance.get("allegation_type") or detail.get("allegation_type")
    if not allegation:
        return None
    subject = instance.get("subject_name") or "the subject"
    employer = detail.get("employer_name")
    start, end = detail.get("fraud_start_date"), detail.get("fraud_end_date")

    source = f"wage records at {employer}" if employer else "wage records on file"
    # "corroborated" is a claim about verified evidence and must not be used
    # for the unverified branch. The rule still fired and the wage record is
    # real; what is missing is the date check, and the verb has to say so.
    verb = "is corroborated by" if instance.get("corroborated") else "is supported by"
    finding = f"The {allegation} allegation against {subject} {verb} {source}."

    if instance.get("corroborated") and start and end:
        basis = (
            f"The wage period was checked against the case's fraud date range "
            f"({start} to {end}) and overlaps it."
        )
        weight = "The corroboration is verified against dates, not assumed."
    elif instance.get("corroborated"):
        basis = "The wage period was checked against the case's fraud date range and overlaps it."
        weight = "The corroboration is verified against dates, not assumed."
    else:
        basis = (
            "No fraud date range is recorded on the case, so the wage period could "
            "not be checked against it."
        )
        weight = (
            "The wage record exists, but the overlap is UNVERIFIED — treat this as "
            "supporting context rather than confirmed corroboration."
        )
    return _join([finding, basis, weight, _confidence_clause(instance)])


def _elevation_narrative(instance: Dict[str, Any],
                         context: InferenceContext) -> Optional[str]:
    detail = instance.get("detail") or {}
    other = instance.get("related_subject_name")
    subject = instance.get("subject_name") or "the subject"
    confirmed = detail.get("confirmed_relationship")
    if not other:
        return None
    what = f"inferred {confirmed} connection" if confirmed else "inferred connection"
    return _join([
        f"The {what} between {subject} and {other} is independently described in "
        "investigator commentary.",
        "The system had already inferred this connection structurally; the narrative "
        "confirms it from a separate source.",
        "Its confidence has been elevated to High on that basis.",
    ])


# ======================================================================
# Entry points
# ======================================================================

def _rejection_clause(instance: Dict[str, Any]) -> Optional[str]:
    """
    The closing sentence on a rejected finding.

    The FINDING text is deliberately left intact above it. An investigator
    deciding whether to revert has to read what was rejected — a line that
    said only "this was rejected" would make the revert control a blind
    one. What changes is that the narrative now ends by stating the finding
    is not in force, who withdrew it and why, and that it can be restored.
    """
    if instance.get("status") != "rejected":
        return None
    audit = instance.get("rejection") or {}
    who = audit.get("rejected_by")
    when = audit.get("rejected_at")
    reason = audit.get("reason")

    attribution = f" by {who}" if who else ""
    timing = f" on {str(when)[:10]}" if when else ""
    because = f' Reason given: "{reason}".' if reason else ""
    return (
        f"REJECTED{attribution}{timing} — this inference has been withdrawn by an "
        f"investigator and is excluded from the case's active findings.{because} "
        "It can be restored through the revert control."
    )


def build_inference(rule_id: str, instance: Dict[str, Any],
                    context: Optional[InferenceContext] = None) -> Optional[str]:
    """
    The investigator-facing narrative for one rule match: what was found, on
    what evidence, how it was established, and what the system did with it.

    Returns None when nothing beyond the rule name is known — an empty
    inference is better than a sentence asserting a relationship the query
    could not actually evidence.

    `context` is optional so a caller with a single instance and no block
    still gets a sensible line; the cross-rule clauses simply do not appear.
    """
    context = context or _EMPTY_CONTEXT

    if rule_id in _STRUCTURAL_TO_NETWORK:
        body = _structural_narrative(rule_id, instance, context)
    elif rule_id in _NETWORK_NAMES:
        body = _network_narrative(rule_id, instance, context)
    elif rule_id == "Rule_12_SLAM_Wage_Corroboration":
        body = _wage_corroboration_narrative(instance, context)
    elif rule_id == "Rule_14_Confirmation_Elevation":
        body = _elevation_narrative(instance, context)
    else:
        body = _case_narrative(rule_id, instance, context)

    if not body:
        return None
    return _join([f"{rule_heading(rule_id)}: {body}", _rejection_clause(instance)])


def enrich_instance(rule_id: str, instance: Dict[str, Any],
                    context: Optional[InferenceContext] = None) -> Dict[str, Any]:
    """Add subject display names and the narrative to one instance."""
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

    inference = build_inference(rule_id, enriched, context)
    if inference:
        enriched["inference"] = inference
    return enriched


def render_block(block: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Second pass over the assembled rules_fired block: rebuild every
    narrative with full cross-rule context, and add the rule-level display
    fields.

    Two passes are necessary and cheap. Rule 8's narrative needs Rule 7's
    and Rule 2's findings, and Rule 1's closing clause needs Rule 2's — but
    rules_fired assembles the block in rule-number order, so on the first
    pass those findings do not exist yet. Rather than reorder the block (a
    contract change) or issue extra queries (a Cypher change), the first
    pass renders what it can and this pass, which sees everything, renders
    the rest. It is pure in-memory work over data already fetched.

    Mutates and returns the same list — rules_fired hands it straight on.
    """
    context = InferenceContext(block)
    for entry in block:
        rule_id = entry.get("rule_id")
        entry["rule_number"] = rule_number(rule_id)
        entry["rule_display_name"] = rule_display_name(rule_id)
        entry["rule_heading"] = rule_heading(rule_id)

        instances = entry.get("instances") or []
        for instance in instances:
            narrative = build_inference(rule_id, instance, context)
            if narrative:
                instance["inference"] = narrative
            elif "inference" in instance:
                # A first-pass line that this pass cannot reproduce would be
                # a bug; leaving the stale text would hide it.
                instance.pop("inference")

        # Rule-level narrative: the single line a summary view shows without
        # expanding the instances. First instance for a single match; a count
        # plus the first for several, so the summary never silently implies
        # there was only one.
        # Prefer a LIVE finding for the collapsed summary line. A rule with
        # one rejected and one active instance should summarise as the active
        # one; leading with the rejected line would read, at a glance, as if
        # the whole rule had been withdrawn.
        rendered = [
            i["inference"] for i in instances
            if i.get("inference") and i.get("status", "active") == "active"
        ] or [i["inference"] for i in instances if i.get("inference")]
        if not rendered:
            entry["inference_summary"] = None
        elif len(rendered) == 1:
            entry["inference_summary"] = rendered[0]
        else:
            entry["inference_summary"] = (
                f"{rendered[0]} ({len(rendered) - 1} further "
                f"{'match' if len(rendered) == 2 else 'matches'} of this rule on this case.)"
            )
    return block