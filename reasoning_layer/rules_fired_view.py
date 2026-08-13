"""
Owns: the render-agnostic `rules_fired` UI contract described in
frontend/rules_fired_ui_contract.md — the reshape that lets the
frontend ship exactly THREE list-item components (`pair` / `single` /
`grouped`) forever, instead of new per-rule UI code every time a rule
is added.

This is a pure, in-memory presentation reshape of the block
reasoning_layer/rules_fired.py already assembles (via
reasoning_layer/rule_inference.py's enrich_instance/render_block). It
captures no new data, opens no Neo4j session, and never touches a
rule's .cypher file — every value below is read straight off fields
those two modules already compute onto each entry/instance.

Called from exactly one place: api/response_builders.py's
fired_rules_only(), the existing single choke point every /intake,
/investigation_plan and /report_generation route already funnels its
rules_fired block through before it reaches an investigator's screen
(see that function's own docstring — "the trim happens HERE, at the
response boundary, and nowhere earlier"). Nothing upstream of that
point is touched: CASE_STORE, the pipeline merge
(reasoning_layer/pipeline.py's _merge_rules_fired), rule-aware task
generation and the rule audit trail all keep reading the original
Functional Spec A.4 contract (detail.title, detail.members,
evidence_count, ...) completely unchanged.

ADDING A 15TH RULE: give it an entry in reasoning_layer/rejection.py's
_RULE_SPECS (it already needs one there, to be rejectable) with the
right family. render_shape() below reads that same table — the one
and only rule_id -> family mapping in the codebase (rejection.py's own
docstring) — so a new rule automatically renders as pair / single /
grouped with ZERO changes to this module. That is the whole point of
the contract: no `switch(rule_id)` anywhere, here or in the frontend.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from reasoning_layer import rejection
from reasoning_layer.rule_inference import display_name, format_money

logger = logging.getLogger(__name__)

# --- The three render shapes (rules_fired_ui_contract.md) ------------------
RENDER_PAIR = "pair"
RENDER_SINGLE = "single"
RENDER_GROUPED = "grouped"

# Family -> render shape. The ONE place this mapping lives. Everything else
# below (which InstanceRow variant an instance/member is built with) derives
# from this table via render_shape(), never a second per-rule_id switch.
_FAMILY_RENDER: Dict[str, str] = {
    rejection.FAMILY_SYMMETRIC_EDGE: RENDER_PAIR,  # Rules 1, 3, 5
    rejection.FAMILY_SUBJECT_CASE_EDGE: RENDER_SINGLE,  # Rules 7, 10
    rejection.FAMILY_NETWORK_EDGE: RENDER_GROUPED,  # Rules 2, 4, 6, 9
    rejection.FAMILY_SUBJECT_FLAG: RENDER_SINGLE,  # Rule 11
    rejection.FAMILY_CASE_FLAG: RENDER_SINGLE,  # Rules 8, 13
    rejection.FAMILY_ALLEGATION_FLAG: RENDER_SINGLE,  # Rule 12
}


def render_shape(rule_id: str) -> Optional[str]:
    """"pair" / "single" / "grouped" for `rule_id`, or None for a rule_id
    with no reasoning_layer.rejection._RULE_SPECS entry.

    Rule_14_Confirmation_Elevation is the one rule_id in
    rule_registry.ALL_RULE_IDS this happens for today: it is a
    cross-cutting confidence modifier on an existing edge, not an
    independent finding of its own (see _RULE_SPECS's trailing
    comment) — nothing displayable, so build_rule_view drops it from
    this contract entirely rather than inventing a fourth render value
    for it.
    """
    family = rejection.rule_family(rule_id)
    return _FAMILY_RENDER.get(family) if family else None


# --- chips -------------------------------------------------------------

# Keys that describe STRUCTURE — how the instance/network/member is shaped
# — never a fact an investigator reads as a pill. The same handful of keys
# for every rule family, not a per-rule allowlist; a new rule's `detail`
# needs no entry here unless it introduces a genuinely new structural key.
_STRUCTURAL_DETAIL_KEYS: Set[str] = {
    "title",
    "members",
    "network_type",
    "network_key",
    "formed_by_rule",
    "subject_id",
    "first_name",
    "last_name",
    "match_id",
    "status",
    # Never display-facing (rules_fired_ui_contract.md's "Inconsistent
    # identifiers" problem — an investigator recognises the complaint
    # number, never the internal graph id). No current rule's `detail`
    # carries a raw case id today (related_case_id lives at the
    # top-level instance, never nested under detail — see
    # rules_fired.py's _INSTANCE_KEYS), but this guards a future rule's
    # detail from silently leaking one into a chip.
    "case_id",
    "related_case_id",
    # Same reasoning as network_key: an internal, NORMALISED composite
    # matching key (Rule 3's own Cypher: "244 elmwood ave|quincy|ma|02169"
    # — lowercased, pipe-joined, punctuation-stripped) rather than a fact
    # an investigator reads. It exists so the rule can MATCH two subjects
    # on address; the human-readable street/city/state/zip fields already
    # on this same `detail` say the identical thing legibly, so showing
    # both is redundant noise rather than a second data point.
    "address_key",
}

# key -> label, for fields already known to appear in `detail` across every
# rule family. This dict only exists to fix the handful of cases a
# mechanical Title-Case fallback gets wrong (an acronym, a wording a
# screenshot already fixed) — everything else falls through to
# `_chip_label`'s fallback, so a brand new `detail` field on a 15th rule
# still renders sensibly with zero changes here.
_CHIP_LABELS: Dict[str, str] = {
    "employer_name": "Employer Name",
    "fein": "Fein",
    "alias_value": "Alias Value",
    "alias_pattern": "Alias Pattern",
    "street": "Street",
    "city": "City",
    "state": "State",
    "zip": "Zip",
    "complaint_no": "Complaint No",
    "date_closed": "Date Closed",
    "outcome": "Outcome",
    "fraud_amount": "Fraud Amount",
    "hub_case_ids": "Hub Case Ids",
    "allegation_type": "Allegation Type",
    "case_status": "Case Status",
    "confirmed_relationship": "Confirmed Relationship",
    "fraud_start_date": "Fraud Start Date",
    "fraud_end_date": "Fraud End Date",
}


def _chip_label(key: str) -> str:
    return _CHIP_LABELS.get(key) or key.replace("_", " ").title()


# key -> formatter, for the handful of fields whose raw graph value needs
# reformatting before an investigator reads it — today just `fraud_amount`,
# which Neo4j can hand back as an int or a float depending on how it was
# written (47850 vs 47850.0), and either way "47850.0" is not money. Reuses
# rule_inference.format_money — the SAME "$47,850" formatting already used
# in every narrative that quotes a fraud amount — so the chip and the
# sentence next to it never disagree on how the number reads. Falls back to
# the generic str()/join formatting in `_chip_display_value` for every key
# not listed here, so a 15th rule's new `detail` field still renders
# sensibly with zero changes to this dict.
_CHIP_VALUE_FORMATTERS: Dict[str, Any] = {
    "fraud_amount": format_money,
}


def _chip_display_value(key: str, value: Any) -> Optional[str]:
    """Every chip value is a display string — the frontend's one
    `ChipList` component renders `value` as-is, never branching on its
    Python type. A list (e.g. hub_case_ids) renders as a comma-joined
    string; a key in `_CHIP_VALUE_FORMATTERS` goes through its
    formatter first (e.g. `fraud_amount` -> "$47,850"); anything else
    falls back to str().

    Returns None (never a stringified "None") when a formatter can't
    make sense of the value — the caller drops the chip entirely rather
    than showing a broken one.
    """
    formatter = _CHIP_VALUE_FORMATTERS.get(key)
    if formatter is not None:
        return formatter(value)
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value if v is not None and str(v).strip())
    return str(value)


def build_chips(detail: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The generic `[{key, label, value}]` array one `ChipList` component
    renders for ANY rule, built from whatever fields `detail` actually
    has (rules_fired_ui_contract.md).

    Two things keep this mechanical rather than rule-specific:
      * `_STRUCTURAL_DETAIL_KEYS` drops fields describing the
        instance's SHAPE (network_type, members, ...), not a fact
        about it.
      * chips whose VALUE collides (e.g. Rule 5's alias_pattern and
        alias_value, which resolve through the same coalesce fallback
        chain to the identical string) collapse to ONE chip — no
        alias-specific key is ever named here. The LATER field in
        `detail` wins the collapsed chip's key/label, which is what
        prefers the more specific `alias_value` over `alias_pattern`
        without hardcoding either name; ties are otherwise vanishingly
        rare (two genuinely different facts sharing one string), and
        keeping the position of the FIRST occurrence means chip order
        still matches the order fields were written to `detail`.
    """
    chips_by_value: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    for key, value in (detail or {}).items():
        if key in _STRUCTURAL_DETAIL_KEYS:
            continue
        if value is None or value == "" or value == []:
            continue
        display_value = _chip_display_value(key, value)
        if not display_value:
            continue
        if display_value not in chips_by_value:
            order.append(display_value)
        chips_by_value[display_value] = {"key": key, "label": _chip_label(key), "value": display_value}
    return [chips_by_value[v] for v in order]


# --- title ---------------------------------------------------------------

_CONNECTOR = "\u2192"  # the "A → B" connector every pair-row screenshot uses


def _pair_title(instance: Dict[str, Any]) -> Dict[str, Any]:
    title: Dict[str, Any] = {"primary": instance.get("subject_name")}
    related = instance.get("related_subject_name")
    if related:
        title["secondary"] = related
        title["connector"] = _CONNECTOR
    return title


def _single_title(instance: Dict[str, Any]) -> Dict[str, Any]:
    # single-render instances name ONE party — complaint numbers, fraud
    # amounts, hub case ids etc. all live in `chips`, never as a second
    # title field (rules_fired_ui_contract.md's Rule 7 / Rule 11 examples).
    return {"primary": instance.get("subject_name")}


def build_title(rule_id: str, instance: Dict[str, Any]) -> Dict[str, Any]:
    """The compact `{primary, secondary?, connector?}` label a `pair` or
    `single` InstanceRow renders at its head. `secondary`/`connector`
    are simply absent (never null) when there is no second party — the
    same component renders either shape without a per-rule `if`."""
    if render_shape(rule_id) == RENDER_PAIR:
        return _pair_title(instance)
    return _single_title(instance)


# --- rejection -----------------------------------------------------------

_REJECTION_CORE_KEYS = (
    "rejected_by",
    "rejected_at",
    "reason",
    "reverted_by",
    "reverted_at",
    "revert_reason",
)
_REJECTION_AUDIT_KEYS = (
    "auto_invalidated",
    "invalidated_by_rule_id",
    "invalidated_reason",
    "reinstated_by_rule_id",
    "reinstated_reason",
    "reinstated_at",
)


def build_rejection(instance: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The `rejection` object a `status: "rejected"` instance carries —
    what the "Rejected Details" link opens. None for anything else, so
    the frontend's `if rejection:` never has to distinguish "not
    rejected" from "rejected with an empty audit trail"."""
    if instance.get("status") != "rejected":
        return None
    raw = instance.get("rejection") or {}
    out = {k: raw[k] for k in _REJECTION_CORE_KEYS if raw.get(k) not in (None, "")}
    for k in _REJECTION_AUDIT_KEYS:
        if raw.get(k) not in (None, ""):
            out[k] = raw[k]
    return out or None


# --- pair / single instance ------------------------------------------------


def build_flat_instance_view(rule_id: str, instance: Dict[str, Any]) -> Dict[str, Any]:
    """One `pair`/`single`-render instance (rules_fired_ui_contract.md's
    "pair / single instance object") — rows 1-14, everything except the
    network family."""
    return {
        "match_id": instance.get("match_id"),
        "status": instance.get("status", "active"),
        "confidence": instance.get("confidence", "Unresolved"),
        "corroborated": bool(instance.get("corroborated", False)),
        "revertable": bool(instance.get("revertable", False)),
        "title": build_title(rule_id, instance),
        "chips": build_chips(instance.get("detail")),
        "rejection": build_rejection(instance),
    }


# --- grouped (network) instance -------------------------------------------

_MEMBER_STRUCTURAL_KEYS = {"subject_id", "first_name", "last_name", "match_id", "status"}


def build_member_view(member: Dict[str, Any]) -> Dict[str, Any]:
    """One row inside a `grouped` instance's `members` list — the exact
    same single-shaped object a top-level `single` instance is, so the
    frontend's member row and its `single` InstanceRow are ONE
    component, per the contract."""
    member_detail = {k: v for k, v in member.items() if k not in _MEMBER_STRUCTURAL_KEYS}
    return {
        "match_id": member.get("match_id"),
        "status": member.get("status", "active"),
        "title": {"primary": display_name(member.get("first_name"), member.get("last_name"), member.get("subject_id"))},
        "chips": build_chips(member_detail),
    }


def build_grouped_instance_view(instance: Dict[str, Any]) -> Dict[str, Any]:
    """One `grouped` instance — a network header plus its member rows.
    `instances` for a network-family rule holds one of these PER
    NETWORK, never one per member (rules_fired_ui_contract.md)."""
    detail = instance.get("detail") or {}
    members = [build_member_view(m) for m in (detail.get("members") or [])]
    network_type = detail.get("network_type")
    title: Dict[str, Any] = {"primary": f"{network_type} Network" if network_type else "Network"}
    network_key = detail.get("network_key")
    if network_key:
        title["secondary"] = network_key
    return {
        "match_id": None,
        "status": instance.get("status", "active"),
        "confidence": instance.get("confidence", "Unresolved"),
        "corroborated": bool(instance.get("corroborated", False)),
        "title": title,
        "member_count": len(members),
        "members": members,
    }


# --- rule level ------------------------------------------------------------


def build_rule_view(rule_id: str, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One rule-level object of the new contract, or None when `rule_id`
    has no render shape (see render_shape) — a finding with nowhere to
    render is dropped rather than shown with a `render` value the
    frontend has never heard of."""
    shape = render_shape(rule_id)
    if shape is None:
        return None
    # Same lookup render_shape() itself just did — reasoning_layer.rejection
    # is the one and only rule_id -> family table (its own docstring), so
    # this is a second read of that table, never a second copy of it. Sent
    # alongside `render` so a frontend/analytics consumer that cares about
    # the underlying rule FAMILY (e.g. grouping rules 1/3/5 together, or
    # logging "network_edge" events) doesn't have to reverse-engineer it
    # from `render` — one render shape (e.g. "single") maps to several
    # families (subject_case_edge, subject_flag, case_flag,
    # allegation_flag), so `render` alone can't answer that question.
    family = rejection.rule_family(rule_id)

    instances = entry.get("instances") or []
    if shape == RENDER_GROUPED:
        instance_views = [build_grouped_instance_view(i) for i in instances]
    else:
        instance_views = [build_flat_instance_view(rule_id, i) for i in instances]

    # active_count/total_count/revertable are derived straight from THIS
    # entry's own `instances` — never from evidence_count/rejected_count —
    # so they stay correct whether `entry` came from a single-subject
    # reasoning_layer.rules_fired.build_rules_fired() call or a case-level
    # merge (reasoning_layer.pipeline._merge_rules_fired), and never drift
    # out of sync with the instances actually being rendered below them.
    active_count = sum(1 for i in instances if i.get("status", "active") == "active")
    total_count = len(instances)
    revertable = any(i.get("status") == "rejected" for i in instances)

    return {
        "rule_id": rule_id,
        "rule_number": entry.get("rule_number"),
        "heading": entry.get("rule_heading"),
        "description": entry.get("rule_description"),
        "render": shape,
        "family": family,
        "fired": bool(entry.get("fired")),
        "confidence": entry.get("confidence", "Unresolved"),
        "corroborated": bool(entry.get("corroborated", False)),
        "active_count": active_count,
        "total_count": total_count,
        "revertable": revertable,
        "instances": instance_views,
    }


def build_rules_fired_view(rules_fired_block: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The full render-agnostic UI contract (rules_fired_ui_contract.md)
    for one case's `rules_fired` block.

    Callers should pass already-`matched`-filtered entries (see
    api/response_builders.py's fired_rules_only, the one place this is
    called from) — a rule that never matched anything has nothing to
    render.

    Pure and side-effect free: reads only what
    reasoning_layer/rules_fired.py + rule_inference.py already computed
    onto each entry/instance; opens no session, writes nothing. Any
    entry that is not a dict, has no rule_id, or has no render shape
    (Rule 14) is silently dropped rather than raising — a malformed or
    non-renderable entry must not take down the whole panel.
    """
    views: List[Dict[str, Any]] = []
    for entry in rules_fired_block or []:
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("rule_id")
        if not rule_id:
            continue
        view = build_rule_view(rule_id, entry)
        if view is not None:
            views.append(view)
    return views