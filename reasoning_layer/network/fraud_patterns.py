"""
Owns: the read-only, case-anchored Fraud Patterns result (Agent 8) built
from the allegation taxonomy in Neo4j: no LLM, no writes, no new inference
rule. One public function, find_fraud_patterns(case_id), returns the four
category-level sections no other tab shows:

  1. precedents          other cases in the same category, different-type
                         cases first (Similar Cases cannot find those)
  2. financial_exposure  case count and total fraud amount per category
  3. outcomes            closed-case outcomes per category (history, not a
                         prediction for this case)
  4. trend               cases opened per calendar year per category

Cypher text lives in fraud_patterns_queries.py. Everything below it that
sorts, groups, counts or classifies is a small plain function over rows, so
tests call it without Neo4j. The LLM only explains this result; it never
decides what is included.

Raises ValueError on a blank case id; Neo4j errors propagate to the caller
(api/pipeline_execution.fetch_live_fraud_patterns turns them into an
"unavailable" result).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from config.settings import (
    FRAUD_PATTERNS_MAX_PRECEDENTS,
    FRAUD_PATTERNS_OUTCOME_PARAM,
    FRAUD_PATTERNS_OUTCOME_RULE_ID,
)
from reasoning_layer import rule_registry
from reasoning_layer.neo4j_client import get_session
from reasoning_layer.network import fraud_patterns_queries as queries
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

SOURCE = "structural_graph"
COMPUTED_BY = "reasoning_layer.network.fraud_patterns"
NOTE_NO_CATEGORY = "This case is not assigned to a category."
OUTCOME_SUBSTANTIATED = "substantiated"
OUTCOME_OTHER = "other"
OUTCOME_NOT_RECORDED = "not_recorded"
_YEAR_PREFIX = re.compile(r"^\s*(\d{4})")
_Row = Dict[str, Any]


def _empty_precedents() -> Dict[str, Any]:
    """The precedents block with no matches and zero totals."""
    return {"matches": [], "total_matches": 0, "same_type_count": 0, "category_only_count": 0}


def empty_fraud_patterns(
    case_types: Optional[List[str]] = None,
    case_categories: Optional[List[str]] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """The full output contract with every section empty (note added when given)."""
    result: Dict[str, Any] = {
        "case_allegation_types": case_types or [],
        "case_categories": case_categories or [],
        "precedents": _empty_precedents(),
        "financial_exposure": [],
        "outcomes": [],
        "trend": [],
        "source": SOURCE,
    }
    if note:
        result["note"] = note
    return result


def _group_by_category(rows: Iterable[_Row]) -> Dict[str, List[_Row]]:
    """Rows keyed by their `category`, categories in alphabetical order."""
    grouped: Dict[str, List[_Row]] = defaultdict(list)
    for row in rows:
        grouped[row["category"]].append(row)
    return dict(sorted(grouped.items()))


def _date_text(value: Any) -> Optional[str]:
    """ISO-ish date text for ordering, or None when missing or blank."""
    text = str(value).strip() if value is not None else ""
    return text or None


def order_related_cases(cases: List[_Row]) -> List[_Row]:
    """Different-type cases first, then newest date_opened, then case id.

    Cases with no date go last within their group (a missing date sorts as
    "", below every real date, in the descending pass). Three stable sorts,
    least significant key first, because ISO date text cannot be negated.
    """
    ordered = sorted(cases, key=lambda c: str(c["case_id"]))
    ordered.sort(key=lambda c: _date_text(c.get("date_opened")) or "", reverse=True)
    ordered.sort(key=lambda c: bool(c["also_same_type"]))
    return ordered


def _shape_related_case(row: _Row) -> _Row:
    """One related-case row in the contract's field set, lists sorted."""
    return {
        "case_id": row["case_id"],
        "complaint_no": row.get("complaint_no"),
        "status": row.get("status"),
        "fraud_amount": row.get("fraud_amount"),
        "date_opened": row.get("date_opened"),
        "case_types": sorted(row.get("case_types") or []),
        "shared_categories": sorted(row.get("shared_categories") or []),
        "also_same_type": bool(row.get("also_same_type")),
        "also_linked_through_people": bool(row.get("also_linked_through_people")),
    }


def build_precedents(rows: List[_Row], limit: int) -> Dict[str, Any]:
    """Order all related cases, cap the list, and report the true totals."""
    cases = order_related_cases([_shape_related_case(row) for row in rows])
    same_type = sum(1 for case in cases if case["also_same_type"])
    return {
        "matches": cases[: max(limit, 0)],
        "total_matches": len(cases),
        "same_type_count": same_type,
        "category_only_count": len(cases) - same_type,
    }


def _amount(value: Any) -> Optional[float]:
    """A fraud amount as a float, or None when missing or not numeric."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def build_financial_exposure(rows: List[_Row]) -> List[_Row]:
    """Per category: case count and total fraud amount (missing counts as 0).

    A missing amount adds 0 and is counted in cases_missing_amount, so the
    total is a floor, not a certainty (same rule as get_network_financial_exposure).
    """
    exposure = []
    for category, cases in _group_by_category(rows).items():
        amounts = [_amount(case.get("fraud_amount")) for case in cases]
        exposure.append(
            {
                "category": category,
                "case_count": len(cases),
                "total_exposure": round(sum(a for a in amounts if a is not None), 2),
                "cases_missing_amount": sum(1 for a in amounts if a is None),
            }
        )
    return exposure


def classify_outcome(
    outcomes: List[Any], statuses: List[Any], disposition: Any, guilty_values: List[str]
) -> str:
    """substantiated / other / not_recorded for one closed case.

    Substantiated: any guilty value appears (case-insensitive CONTAINS) in an
    allegation outcome, an allegation status or the case disposition, which
    is how Rule 7 reads a guilty outcome. Other: an outcome or disposition is
    recorded but matches nothing. Not recorded: neither exists.
    """
    recorded = [str(v).strip().lower() for v in [*outcomes, disposition] if v and str(v).strip()]
    status_texts = [str(v).strip().lower() for v in statuses if v and str(v).strip()]
    if any(value in text for text in [*recorded, *status_texts] for value in guilty_values):
        return OUTCOME_SUBSTANTIATED
    return OUTCOME_OTHER if recorded else OUTCOME_NOT_RECORDED


def build_outcomes(rows: List[_Row], guilty_values: List[str]) -> List[_Row]:
    """Per category: closed-case counts split by classify_outcome."""
    summary = []
    for category, cases in _group_by_category(rows).items():
        labels = [
            classify_outcome(
                case.get("outcomes") or [], case.get("statuses") or [], case.get("disposition"), guilty_values
            )
            for case in cases
        ]
        summary.append(
            {
                "category": category,
                "closed_count": len(labels),
                "substantiated_count": labels.count(OUTCOME_SUBSTANTIATED),
                "other_count": labels.count(OUTCOME_OTHER),
                "not_recorded_count": labels.count(OUTCOME_NOT_RECORDED),
            }
        )
    return summary


def year_of(value: Any) -> Optional[int]:
    """Calendar year of an ISO date string or date object, or None."""
    year = getattr(value, "year", None)
    if isinstance(year, int):
        return year
    match = _YEAR_PREFIX.match(str(value)) if value is not None else None
    return int(match.group(1)) if match else None


def build_trend(rows: List[_Row]) -> List[_Row]:
    """Per category: cases opened per calendar year (oldest first) plus the unknown-date count."""
    trend = []
    for category, cases in _group_by_category(rows).items():
        years = [year_of(case.get("opened_date")) for case in cases]
        known = sorted({y for y in years if y is not None})
        trend.append(
            {
                "category": category,
                "by_year": [{"year": y, "case_count": years.count(y)} for y in known],
                "unknown_date_count": years.count(None),
            }
        )
    return trend


def _rule_vocab(param: str) -> List[str]:
    """One of Rule 7's vocabulary lists, lower-cased, read live so operator tuning applies."""
    params = rule_registry.load_registry().get(FRAUD_PATTERNS_OUTCOME_RULE_ID, {}).get("params", {})
    values = [str(v).strip().lower() for v in params.get(param) or [] if str(v).strip()]
    if not values:
        raise ValueError(f"{FRAUD_PATTERNS_OUTCOME_RULE_ID}.{param} is not configured")
    return values


def _guilty_values() -> List[str]:
    """Rule 7's guilty outcome vocabulary."""
    return _rule_vocab(FRAUD_PATTERNS_OUTCOME_PARAM)


def _closed_statuses() -> List[str]:
    """Rule 7's closed-status vocabulary (decides which cases count as closed)."""
    return _rule_vocab("closed_case_statuses")


def _run(session, query: str, case_id: str, **params: Any) -> List[_Row]:
    """Run one read-only query for this case and return its rows."""
    return session.run(query, case_id=case_id, **params).data()


def _build_sections(session, case_id: str, limit: int) -> Dict[str, Any]:
    """Run the four section queries and shape them into the contract fields."""
    category_cases = _run(session, queries.CATEGORY_CASES, case_id)
    return {
        "precedents": build_precedents(_run(session, queries.RELATED_CASES, case_id), limit),
        "financial_exposure": build_financial_exposure(category_cases),
        "outcomes": build_outcomes(
            _run(session, queries.CLOSED_CASE_OUTCOMES, case_id, closed_statuses=_closed_statuses()),
            _guilty_values(),
        ),
        "trend": build_trend(category_cases),
    }


def find_fraud_patterns(case_id: str, limit: int = FRAUD_PATTERNS_MAX_PRECEDENTS) -> Dict[str, Any]:
    """Return the Fraud Patterns result for `case_id` in the {result, provenance} envelope.

    A case with no category (only OTH/EEC types, or no recognised type) gets
    the empty contract plus a `note`, never an error. `limit` caps
    precedents.matches only; the totals beside it are never capped.

    Raises:
        ValueError: blank case_id, or Rule 7's guilty vocabulary unconfigured.
        GraphUnavailableError / Neo4jError: propagated to the caller.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("find_fraud_patterns requires a non-empty case_id")
    case_id = str(case_id).strip()
    with get_session() as session:
        own = _run(session, queries.OWN_TYPES, case_id)
        types = sorted({row["description"] for row in own if row.get("description")})
        categories = sorted({row["category"] for row in own if row.get("category")})
        if categories:
            result = {**empty_fraud_patterns(types, categories), **_build_sections(session, case_id, limit)}
        else:
            result = empty_fraud_patterns(types, categories, NOTE_NO_CATEGORY)
    logger.info(
        "find_fraud_patterns: case_id=%s categories=%d related=%d",
        case_id,
        len(categories),
        result["precedents"]["total_matches"],
    )
    return {"result": result, "provenance": graph_provenance(COMPUTED_BY)}
