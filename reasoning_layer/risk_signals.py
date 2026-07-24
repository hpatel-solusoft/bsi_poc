"""
Owns: the four Neo4j-sourced risk signals that are layered on top of the
AppWorks base risk score (AI-15 / Section 8.4).

Section 8.4 is explicit that the AppWorks base scoring logic is UNCHANGED;
these signals are added as additional inputs AFTER the base score is
computed. This module therefore takes the finished AppWorks result and
returns an augmented copy — it never recomputes the base score.

The four signals and their effects (Section 8.4):
  1. Rule 8 — recidivist in active network: HAS_PRIOR_GUILTY_CASE AND
     MEMBER_OF_FRAUD_NETWORK on the subject. Boolean additive signal that
     raises the score.
  2. Network size multiplier: largest FraudNetwork the subject belongs to.
     4-6 members => 1.2x, 7+ => 1.5x, applied to the GRAPH score component
     only (never to the AppWorks base).
  3. Prior guilt recency: most recent date_closed on HAS_PRIOR_GUILTY_CASE.
     <2yr => full weight, 2-5yr => 0.7x, >5yr => 0.4x — weights the
     prior-guilt part of the graph component.
  4. Rule 13 FastTrack override: is_fasttrack on the :Case. Forces
     risk_tier to a minimum of HIGH regardless of the computed score.
  Plus the compound rule penalty: 3+ of Rules 7, 8, 9, 11 firing together
  in rules_fired => compound_escalation flag.

Provenance (Section 8.4 requirement): the source of each risk component is
independently attributable — delivered as TWO provenance blocks in the
trail: the AppWorks base scorer's block and this module's block.

GOVERNANCE: this is a Neo4j read, not an AppWorks call, so per the rule
that manifest.yaml holds a tool only if it is LLM-called AND makes an
AppWorks call, it is invoked DIRECTLY by the /risk_assessment route (the
same pattern as check_network_match / enrich_graph_context /
find_structural_matches), never as an LLM tool.

Does NOT own: the AppWorks base scoring (appworks/risk_scoring.py), the
pipeline, or any write.
"""

from __future__ import annotations

import copy
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# --- tunable graph-signal weights (0.0–1.0 scale, matching the base score) ---
# Section 8.4 fixes the MULTIPLIERS (network size, recency) and the
# STRUCTURE, but not the base point value of each signal. These are the
# graph component's base weights, kept as named constants so they are
# auditable and tunable rather than magic numbers inside the arithmetic.
_RULE_8_SIGNAL_WEIGHT = 0.15      # recidivist in an active network is a strong signal
_PRIOR_GUILT_BASE_WEIGHT = 0.10   # weighted down by recency below

# Section 8.4 tier ladder — identical thresholds to appworks/risk_scoring.py,
# re-declared here only to re-tier the FINAL (base + graph) score. The base
# module's own tiering is left untouched.
_TIER_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def _tier_for_score(score: float) -> str:
    if score >= 0.75:
        return "CRITICAL"
    if score >= 0.50:
        return "HIGH"
    if score >= 0.25:
        return "MEDIUM"
    return "LOW"


def _at_least(tier: str, floor: str) -> str:
    """Return whichever of `tier` / `floor` is more severe — used by the
    FastTrack override to raise the tier to a minimum of HIGH."""
    ti = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else 0
    fi = _TIER_ORDER.index(floor) if floor in _TIER_ORDER else 0
    return _TIER_ORDER[max(ti, fi)]


def _network_size_multiplier(network_size: int) -> float:
    """4-6 members => 1.2x, 7+ => 1.5x, otherwise 1.0x (Section 8.4)."""
    if network_size >= 7:
        return 1.5
    if network_size >= 4:
        return 1.2
    return 1.0


def _recency_weight(years: Optional[float]) -> float:
    """<2yr => 1.0, 2-5yr => 0.7, >5yr => 0.4 (Section 8.4). No prior guilt
    (years is None) contributes nothing."""
    if years is None:
        return 0.0
    if years < 2:
        return 1.0
    if years <= 5:
        return 0.7
    return 0.4


# --- Neo4j reads. Plain, scalar-returning, single statements each. ---
#
# Two DISTINCT FastTrack facts, deliberately read separately — they mean
# different things and must never be collapsed into one:
#
#   is_fasttrack               — AppWorks' asserted fact. A human, in
#                                AppWorks, set this. Neo4j is not its
#                                system of record (Principle 11), and
#                                rule_13_fasttrack_escalation.cypher is
#                                explicit that it never writes this field.
#   fasttrack_recommendation_* — Rule 13's own output. The rule fired and
#                                recommended escalation. Written by the
#                                rule, owned by the graph.
#
# The "FastTrack override" signal is a RULE-FIRED signal: it reports
# whether Rule 13 fired, not whether a human has since acted on it.
# Reading only is_fasttrack (the previous behaviour) made the tile
# display "No" on every case where Rule 13 had fired correctly but no
# human had yet flipped the AppWorks field — reporting a rule outcome
# through a field the rule is forbidden from writing. The rule's firing
# is the fact the tile is asking about, so that is what it now reads.
#
# status = "active" is required so an investigator's rejection of Rule 13
# (reasoning_layer.rejection, _FAMILY_CASE_FLAG) correctly clears the
# signal rather than leaving a rejected recommendation still counted.
_FASTTRACK_QUERY = """
MATCH (c:Case {case_id: $case_id})
RETURN coalesce(c.is_fasttrack, false) AS is_fasttrack,
       (coalesce(c.fasttrack_recommended, false)
        AND coalesce(c.fasttrack_recommendation_status, "") = "active")
           AS fasttrack_recommended,
       c.fasttrack_reason AS fasttrack_reason
"""

# Rule 8 inputs + prior-guilt recency inputs.
#
# WHY THIS RETURNS FIVE DATE FIELDS PER PRIOR CASE, NOT ONE:
# recency used to read pg.date_closed alone. Rule 7 already coalesces
# case.closed_date -> allegation.date_closed when it writes that
# property, but on older AppWorks cases BOTH of those source fields are
# null (rule_07_prior_guilty.cypher documents this exact null-source
# gap), so pg.date_closed comes back null and the subject ends up
# "detected but unweighable" — prior_guilty_count=1 with
# prior_guilt_recency_years=null, which is what the risk tile was
# showing.
#
# Those same cases DO carry other dates. graph_sync.py populates
# opened_date from WorkfolderOpenDate OR S_CREATEDDATE, and
# S_CREATEDDATE is present on essentially every AppWorks record. So the
# case can almost always be dated — just not from its closure field.
#
# All five candidates are returned and the ranking is done in Python
# (_resolve_prior_recency) rather than with a Cypher coalesce, because
# the caller needs to know WHICH field answered: a recency derived from
# opened_date is an estimate and must be labelled as one on a screen
# that feeds a risk score. A coalesce would collapse that distinction.
_RULE8_RECENCY_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})
OPTIONAL MATCH (s)-[pg:HAS_PRIOR_GUILTY_CASE]->(pc:Case)
    WHERE pg.status = "active"

// The specific allegation Rule 7 attributed, when it recorded one.
// Matched on pg.allegation_id rather than taking any allegation on the
// case: on a multi-allegation case the others may be unrelated to the
// guilty finding and their dates would misdate the prior.
OPTIONAL MATCH (pc)-[:HAS_ALLEGATION]->(pa:Allegation)
    WHERE pg.allegation_id IS NOT NULL
      AND pa.allegation_id = pg.allegation_id
WITH s, pg, pc, collect(DISTINCT pa.date_closed) AS allegation_dates

WITH s,
     count(DISTINCT pg) AS prior_guilty_count,
     collect({
         case_id:                pc.case_id,
         rel_date_closed:        pg.date_closed,
         case_closed_date:       pc.closed_date,
         allegation_date_closed: head([d IN allegation_dates WHERE d IS NOT NULL]),
         case_fraud_end_date:    pc.fraud_end_date,
         case_opened_date:       pc.opened_date
     }) AS prior_case_dates

OPTIONAL MATCH (s)-[mem:MEMBER_OF_FRAUD_NETWORK]->(:FraudNetwork)
    WHERE mem.status = "active"
RETURN prior_guilty_count,
       prior_case_dates,
       count(DISTINCT mem) AS network_membership_count
"""

# Recency-weight applied when Rule 7 fired but no usable closed_date
# exists to date it from. Rule 7 writes r.date_closed = c.closed_date,
# and on older AppWorks cases that source field is simply never
# populated (the same null-source gap rule_07_prior_guilty.cypher
# documents for c.status). A null date is missing METADATA about a
# prior guilty case, not evidence that the prior guilty case is old or
# absent — so scoring it 0.0, as an unknown-years value previously did
# via _recency_weight(None), silently deleted a fired rule's entire
# contribution to the risk score.
#
# 0.7 is the 2-5yr weight: the middle band, chosen deliberately as the
# neutral assumption for an undated prior. It neither rewards the data
# gap with the full <2yr weight nor penalises it with the >5yr weight.
_UNDATED_PRIOR_GUILT_RECENCY_WEIGHT = 0.7

# Largest active FraudNetwork the subject belongs to (member count includes
# the subject).
_NETWORK_SIZE_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})-[m:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE m.status = "active"
OPTIONAL MATCH (member:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
    WHERE mm.status = "active"
WITH n, count(DISTINCT member) AS members
RETURN coalesce(max(members), 0) AS max_network_size
"""

_COMPOUND_RULE_PREFIXES = ("Rule_07", "Rule_08", "Rule_09", "Rule_11")


# The date fields consulted, in priority order, to age a prior guilty
# case. Each entry is (field returned by the query, label reported to
# the caller, is_estimate).
#
# The first three are genuine closure dates and answer the question
# "how long ago was this subject found guilty?" directly. The last two
# are NOT closure dates — fraud_end_date is when the alleged conduct
# stopped and opened_date is when the case was filed, both of which
# necessarily PRE-date the finding of guilt. Using them therefore
# OVERSTATES the elapsed years, which is the conservative direction for
# a risk score: it can only weaken the prior-guilt weight, never
# inflate it. They are flagged is_estimate=True so the UI can render
# "~4.2 yrs (est. from case open date)" rather than asserting a
# precision the data does not support.
_RECENCY_DATE_SOURCES = (
    ("rel_date_closed",        "prior_guilty_case.date_closed", False),
    ("case_closed_date",       "case.closed_date",              False),
    ("allegation_date_closed", "allegation.date_closed",        False),
    ("case_fraud_end_date",    "case.fraud_end_date",           True),
    ("case_opened_date",       "case.opened_date",              True),
)

# Anything from this year or earlier is treated as an unparsed sentinel
# rather than a date. AppWorks exports use 1900-01-01 and 0001-01-01 as
# "empty", and a 2000-year-old prior would silently pin the weight to
# the >5yr floor while looking like real data. The comparison is
# inclusive — 1900 IS the sentinel, so excluding only years strictly
# below it would let the most common one straight through.
_MIN_PLAUSIBLE_YEAR = 1900

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d",
    "%d-%b-%Y", "%b %d, %Y", "%d %b %Y",
)


def _coerce_date(value: Any) -> Optional[datetime]:
    """
    Turn whatever the graph holds into a naive datetime, or None.

    The previous implementation was `datetime.fromisoformat(str(v)[:10])`
    inside a try/except, which is correct ONLY for a plain ISO string.
    etl/normalizers.to_iso_date does normally produce exactly that, but
    it returns None whenever it fails to parse, and any property written
    by something other than that normaliser (a rule, a manual Neo4j fix,
    a re-import) can hold a native temporal type or a locale format
    instead. Every one of those cases silently became "no date", which
    is indistinguishable from "no prior guilt" by the time it reaches
    the tile.
    """
    if value is None or value == "":
        return None

    # neo4j.time.Date / DateTime expose to_native(); datetime.date and
    # datetime.datetime do not.
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        try:
            value = to_native()
        except Exception:  # pragma: no cover - defensive
            pass

    # datetime BEFORE date: datetime is a subclass of date, so the
    # reverse order would truncate every timestamp's time component.
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    text = str(value).strip()
    if not text:
        return None

    # Epoch seconds/milliseconds, and the .NET "/Date(...)/" wrapper
    # some AppWorks endpoints emit.
    digits = text
    if digits.startswith("/Date(") and digits.endswith(")/"):
        digits = digits[6:-2].split("+")[0].split("-")[0]
    if digits.lstrip("-").isdigit() and len(digits.lstrip("-")) in (10, 13):
        try:
            seconds = int(digits) / (1000.0 if len(digits.lstrip("-")) == 13 else 1.0)
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError, OSError):
            pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:len(fmt) + 6].strip(), fmt)
        except ValueError:
            continue
    return None


def _years_since(value: Any) -> Optional[float]:
    """Years elapsed from `value` until now, or None if it is not a
    usable date. Never negative: a future date reads as 0.0 years,
    which is the honest answer to "how long ago" and keeps the <2yr
    weight rather than producing a nonsensical negative age."""
    parsed = _coerce_date(value)
    if parsed is None or parsed.year <= _MIN_PLAUSIBLE_YEAR:
        return None
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - parsed
    return max(delta.days / 365.25, 0.0)


def _resolve_prior_recency(prior_case_dates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Age every prior guilty case using the first usable date in
    _RECENCY_DATE_SOURCES, then report the MOST RECENT one.

    Most recent, not oldest: recency weighting asks "how long since this
    subject was last found guilty?", so a subject with priors from 2019
    and 2024 is a 2024-recency risk. min() over years is that.

    Returns a dict that is always fully populated in shape — years/date/
    source are None together when nothing could be dated, so a caller
    never has to guess whether a missing key means undated or unset.
    """
    best: Optional[Dict[str, Any]] = None

    for entry in prior_case_dates or []:
        # The Cypher collect() emits one all-null map when the subject
        # has no priors at all; case_id is the discriminator.
        if not entry or not entry.get("case_id"):
            continue
        for field, label, is_estimate in _RECENCY_DATE_SOURCES:
            raw = entry.get(field)
            years = _years_since(raw)
            if years is None:
                continue
            candidate = {
                "years": round(years, 2),
                "date": _coerce_date(raw).date().isoformat(),
                "source": label,
                "estimated": is_estimate,
                "case_id": entry.get("case_id"),
            }
            if best is None or candidate["years"] < best["years"]:
                best = candidate
            break  # first usable field wins for THIS prior case

    if best is None:
        return {"years": None, "date": None, "source": None,
                "estimated": False, "case_id": None}
    return best


def _compound_escalation(rules_fired: List[Dict[str, Any]]) -> bool:
    """3+ of Rules 7, 8, 9, 11 firing simultaneously (Section 8.4)."""
    fired = {
        r.get("rule_id", "")[:7]
        for r in (rules_fired or [])
        if r.get("fired") and r.get("rule_id", "").startswith(_COMPOUND_RULE_PREFIXES)
    }
    return len(fired) >= 3


def apply_graph_risk_signals(
    case_id: str,
    subject_id: str,
    base_result: Dict[str, Any],
    rules_fired: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """
    Layer the four Neo4j graph signals on top of an AppWorks base risk
    result (Section 8.4). Does not recompute the base score.

    Args:
        case_id, subject_id: the case and its primary subject.
        base_result: the AppWorks risk result dict, containing at least
            risk_score (0.0–1.0) and risk_tier.
        rules_fired: the pipeline's rules_fired block (from CS-4 / Context
            Enrichment), used only for the compound-escalation flag.

    Returns (inside the standard {result, provenance} envelope) a COPY of
    base_result with:
        * base_risk_score / base_risk_tier  — the untouched AppWorks values
        * risk_score / risk_tier            — the final values after signals
        * neo4j_signals: {
              rule_8_signal, network_size, network_size_multiplier,
              prior_guilt_recency_years, prior_guilt_recency_date,
              prior_guilt_recency_source, prior_guilt_recency_estimated,
              prior_guilt_recency_case_id, fasttrack_override,
              compound_escalation, graph_score_component
          }
    and a provenance block for the Neo4j graph signals. Together with the
    AppWorks base scorer's own provenance block (already in the trail from
    calculate_risk_metrics), this gives the two independently-attributable
    computed_by entries Section 8.4 requires.

    Raises GraphUnavailableError / Neo4jError on a graph problem — the
    /risk_assessment route catches these and returns the base result
    unaugmented (non-blocking), rather than fabricating signals.
    """
    if not subject_id or not str(subject_id).strip():
        raise ValueError("apply_graph_risk_signals requires a non-empty subject_id")
    case_id = str(case_id).strip()
    subject_id = str(subject_id).strip()

    with get_session() as session:
        ft_rec = session.run(_FASTTRACK_QUERY, case_id=case_id).single()
        r8_rec = session.run(_RULE8_RECENCY_QUERY, subject_id=subject_id).single()
        ns_rec = session.run(_NETWORK_SIZE_QUERY, subject_id=subject_id).single()

    is_fasttrack = bool(ft_rec["is_fasttrack"]) if ft_rec else False
    fasttrack_recommended = bool(ft_rec["fasttrack_recommended"]) if ft_rec else False
    fasttrack_reason = ft_rec["fasttrack_reason"] if ft_rec else None
    prior_guilty_count = int(r8_rec["prior_guilty_count"]) if r8_rec else 0
    network_membership_count = int(r8_rec["network_membership_count"]) if r8_rec else 0
    prior_case_dates = list(r8_rec["prior_case_dates"]) if r8_rec else []
    network_size = int(ns_rec["max_network_size"]) if ns_rec else 0

    # --- signal 1: Rule 8 — recidivist in an active network ---
    rule_8_signal = prior_guilty_count > 0 and network_membership_count > 0

    # --- signal 3: prior-guilt recency (most recent prior guilty case) ---
    # prior_guilt_fired is the SIGNAL; recency_years is optional detail
    # ABOUT that signal. Rule 7 having fired is a fact in its own right,
    # independent of whether the source data happened to carry a usable
    # closed_date to date it with — so the two are reported separately
    # and the score no longer collapses to zero when only the date is
    # missing (see _UNDATED_PRIOR_GUILT_RECENCY_WEIGHT).
    prior_guilt_fired = prior_guilty_count > 0
    recency = _resolve_prior_recency(prior_case_dates)
    prior_guilt_recency_years = recency["years"]

    if not prior_guilt_fired:
        recency_weight = 0.0
    elif prior_guilt_recency_years is None:
        # Genuinely undated: not one of the five candidate fields held a
        # usable value. Falls back to the documented neutral weight
        # rather than deleting the fired rule's contribution.
        recency_weight = _UNDATED_PRIOR_GUILT_RECENCY_WEIGHT
    else:
        recency_weight = _recency_weight(prior_guilt_recency_years)

    # --- signal 2: network size multiplier (graph component only) ---
    network_size_multiplier = _network_size_multiplier(network_size)

    # --- compound escalation flag ---
    compound_escalation = _compound_escalation(rules_fired)

    # --- assemble the graph score component ---
    rule_8_component = _RULE_8_SIGNAL_WEIGHT if rule_8_signal else 0.0
    prior_guilt_component = (_PRIOR_GUILT_BASE_WEIGHT * recency_weight) if prior_guilt_fired else 0.0
    graph_component = round((rule_8_component + prior_guilt_component) * network_size_multiplier, 4)

    base_score = float(base_result.get("risk_score", 0.0) or 0.0)
    base_tier = base_result.get("risk_tier", _tier_for_score(base_score))

    final_score = round(min(base_score + graph_component, 1.0), 4)
    final_tier = _tier_for_score(final_score)

    # --- signal 4: FastTrack override — floor the tier at HIGH ---
    # Fires on EITHER the human-asserted AppWorks flag or Rule 13's own
    # active recommendation. A case Rule 13 has recommended for
    # escalation carries the same risk whether or not a human has yet
    # clicked the button in AppWorks; gating the floor on the human
    # action alone meant the rule could fire and change nothing.
    fasttrack_override = is_fasttrack or fasttrack_recommended
    if fasttrack_override:
        final_tier = _at_least(final_tier, "HIGH")

    # Build the augmented result (copy so the base result is never mutated).
    augmented = copy.deepcopy(base_result)
    augmented["base_risk_score"] = round(base_score, 4)
    augmented["base_risk_tier"] = base_tier
    augmented["risk_score"] = final_score
    augmented["risk_tier"] = final_tier
    augmented["neo4j_signals"] = {
        "rule_8_signal": rule_8_signal,
        "network_size": network_size,
        "network_size_multiplier": network_size_multiplier,
        # Rule 7 fired or not — the signal the "Prior guilt recency" tile
        # reports. True here means a prior guilty case is on file for this
        # subject, and stays true whether or not the source data carried a
        # closed_date. Previously the tile inferred this from
        # prior_guilt_recency_years alone, so a null date rendered as
        # "N/A" and a genuinely-fired rule looked like it had not fired.
        "prior_guilt_signal": prior_guilt_fired,
        "prior_guilty_count": prior_guilty_count,
        # Detail ABOUT the signal above, not the signal itself. None means
        # "fired, but the prior case's closed_date is absent in the source
        # data" — NOT "no prior guilt". Callers rendering the tile should
        # branch on prior_guilt_signal and treat this as an optional
        # qualifier ("Yes" / "Yes — 3.4 yrs"), never as the presence test.
        "prior_guilt_recency_years": prior_guilt_recency_years,
        # Which field the age was read from, the date itself, and
        # whether it is an estimate. Reported because a recency derived
        # from a case OPEN date is weaker evidence than one derived from
        # its closure date, and a number feeding a risk tier should not
        # hide which of the two it is. estimated=True means the date
        # necessarily pre-dates the finding of guilt, so the years are
        # an upper bound and the weight is conservative.
        "prior_guilt_recency_date": recency["date"],
        "prior_guilt_recency_source": recency["source"],
        "prior_guilt_recency_estimated": recency["estimated"],
        "prior_guilt_recency_case_id": recency["case_id"],
        "prior_guilt_recency_weight": recency_weight,
        # True when Rule 13 fired OR a human set is_fasttrack in AppWorks.
        # The two sources are kept separately below so the distinction
        # stays auditable and Principle 11 is not blurred — the graph
        # still never claims AppWorks asserted something it did not.
        "fasttrack_override": fasttrack_override,
        "fasttrack_recommended": fasttrack_recommended,
        "fasttrack_reason": fasttrack_reason,
        "fasttrack_asserted_in_appworks": is_fasttrack,
        "compound_escalation": compound_escalation,
        "graph_score_component": graph_component,
    }

    logger.info(
        "apply_graph_risk_signals: case_id=%s subject_id=%s base=%.4f/%s -> final=%.4f/%s "
        "rule8=%s net_size=%d mult=%.1f prior_guilt=%s (count=%d recency_yrs=%s via=%s est=%s weight=%.2f) "
        "fasttrack=%s (recommended=%s appworks=%s) compound=%s",
        case_id, subject_id, base_score, base_tier, final_score, final_tier,
        rule_8_signal, network_size, network_size_multiplier,
        prior_guilt_fired, prior_guilty_count, prior_guilt_recency_years,
        recency["source"], recency["estimated"], recency_weight,
        fasttrack_override, fasttrack_recommended, is_fasttrack, compound_escalation,
    )

    return {
        "result": augmented,
        "provenance": graph_provenance(
            # Section 8.4 requires each risk component to be independently
            # attributable. That is delivered as TWO provenance BLOCKS in the
            # trail: the AppWorks base scorer's block (computed_by "BSI
            # deterministic rules engine", added by calculate_risk_metrics) and
            # THIS block for the Neo4j graph signals. computed_by stays a single
            # string per block so merge_provenance can hash it for
            # de-duplication; the two-entry requirement is met by the two
            # blocks, not by a list inside one.
            "reasoning_layer.risk_signals (Neo4j graph signals)",
        ),
    }