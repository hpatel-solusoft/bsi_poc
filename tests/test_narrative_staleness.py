"""
AI-32 — unit tests for core/narrative_staleness.py.

No I/O, no mocking needed: this module is deliberately pure, so these
tests exercise it directly against plain datetime values.

Covers the ticket's own acceptance bar verbatim — "Test all 4
combinations (neither / core only / graph only / both)" — plus the
edge cases that make the two None-handling branches and the strict-`>`
boundary condition explicit and regression-proof.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.narrative_staleness import StalenessCheck, check_staleness, is_graph_newer

_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
_BEFORE = _T0 - timedelta(hours=1)
_AFTER = _T0 + timedelta(hours=1)


# --------------------------------------------------------------------
# The ticket's own 4-combination acceptance test, on one representative
# "seed case" worth of timestamps (mirrors what a real /intake call
# would pass: cache_generated_at = T0, and either no reject/revert at
# all, one from before the cache was generated, or one from after).
# --------------------------------------------------------------------


def test_combination_neither_stale():
    check = check_staleness(
        reload_ai_summary_requested=False,
        last_inference_change_at=_BEFORE,  # reject/revert happened, but before the cache
        cache_generated_at=_T0,
    )
    assert check.stale_reason is None
    assert check.should_refresh is False
    assert check.should_rerun_full_pipeline is False


def test_combination_core_data_only():
    check = check_staleness(
        reload_ai_summary_requested=True,
        last_inference_change_at=_BEFORE,
        cache_generated_at=_T0,
    )
    assert check.stale_reason == "core_data"
    assert check.should_refresh is True
    assert check.should_rerun_full_pipeline is True


def test_combination_graph_only():
    check = check_staleness(
        reload_ai_summary_requested=False,
        last_inference_change_at=_AFTER,  # reject/revert happened AFTER the cache
        cache_generated_at=_T0,
    )
    assert check.stale_reason == "graph"
    assert check.should_refresh is True
    assert check.should_rerun_full_pipeline is False, (
        "graph-only staleness must never trigger the expensive full pipeline "
        "re-run — see StalenessCheck.should_rerun_full_pipeline's docstring"
    )


def test_combination_both():
    check = check_staleness(
        reload_ai_summary_requested=True,
        last_inference_change_at=_AFTER,
        cache_generated_at=_T0,
    )
    assert check.stale_reason == "both"
    assert check.should_refresh is True
    assert check.should_rerun_full_pipeline is True


# --------------------------------------------------------------------
# is_graph_newer edge cases
# --------------------------------------------------------------------


def test_is_graph_newer_none_last_inference_change_at_is_false():
    """No investigator has ever rejected/reverted anything for this
    case — nothing to compare, never a graph staleness signal."""
    assert is_graph_newer(None, _T0) is False


def test_is_graph_newer_none_cache_generated_at_is_false():
    """No cached narrative exists yet at all (first-ever run) — nothing
    stale to detect; the imminent run will capture the current graph."""
    assert is_graph_newer(_AFTER, None) is False


def test_is_graph_newer_both_none_is_false():
    assert is_graph_newer(None, None) is False


def test_is_graph_newer_equal_timestamps_is_false():
    """Strict `>`, not `>=`: a narrative generated in the exact refresh
    that set this last_inference_change_at value must not immediately
    re-report itself as stale."""
    assert is_graph_newer(_T0, _T0) is False


def test_is_graph_newer_strictly_after_is_true():
    assert is_graph_newer(_AFTER, _T0) is True


def test_is_graph_newer_strictly_before_is_false():
    assert is_graph_newer(_BEFORE, _T0) is False


# --------------------------------------------------------------------
# StalenessCheck as a standalone value type (constructed directly,
# bypassing check_staleness — confirms the dataclass's own properties
# are correct in isolation, not just via the factory).
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "core_data_changed,graph_changed,expected_reason",
    [
        (False, False, None),
        (True, False, "core_data"),
        (False, True, "graph"),
        (True, True, "both"),
    ],
)
def test_staleness_check_stale_reason_matrix(core_data_changed, graph_changed, expected_reason):
    check = StalenessCheck(core_data_changed=core_data_changed, graph_changed=graph_changed)
    assert check.stale_reason == expected_reason
    assert check.should_refresh == (core_data_changed or graph_changed)
    assert check.should_rerun_full_pipeline == core_data_changed


def test_staleness_check_is_frozen():
    """Immutable by construction — a route must not be able to mutate a
    staleness verdict mid-request after computing it."""
    check = StalenessCheck(core_data_changed=False, graph_changed=False)
    with pytest.raises(AttributeError):
        check.core_data_changed = True  # type: ignore[misc]
