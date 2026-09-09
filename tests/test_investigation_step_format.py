"""
Regression tests for two linked bugs reported against the Investigation
Plan / Copilot flow:

  1. Investigation Plan steps rendered a stray literal "+" between the
     task label and its rationale (e.g. "Check DTA Beacon Database + to
     confirm ..."). Root cause: PLAN_PROMPT's MANDATORY STEP FORMAT
     instructed the LLM to join the two with a literal " + ", and
     nothing downstream ever parsed on that character -- it was pure
     prose that leaked straight through to the UI and into Copilot's
     context.

  2. Asking Copilot "what are the modified investigation steps?" caused
     it to invent a description for a terse human-edited step (e.g. a
     step whose entire saved text was "Testing" came back with a
     fabricated line about it being "a placeholder for further
     development"). Root cause: COPILOT_TOOL_PROMPT had no instruction
     to reproduce step text verbatim, so once bug (1) had already
     conditioned the surrounding steps into a "Label + rationale" shape,
     the model pattern-matched and "completed" the one step that didn't
     fit.

Fix: config/plan_step_format.py is now the single source of truth for
the step-authoring contract (a markdown "**label:** rationale" convention,
never a literal "+"), used by BOTH config.prompts.PLAN_PROMPT (author
side) and reasoning_layer.investigation_tasks (parser side). COPILOT_TOOL_PROMPT
gained an explicit verbatim-reproduction guardrail, reinforced by a
"_verbatim_notice" field agent_service.prompt_builders.build_copilot_prompt
now attaches directly to the serialized investigation_plan context.

These tests intentionally check PROMPT CONTENT (not LLM behavior, which
is not deterministic and not something a unit test can assert on) plus
the deterministic parsing/splicing code around it. They are the
regression guard against someone re-introducing the "+" glue, or
removing the verbatim guardrail, in a future edit.
"""

from __future__ import annotations

import json

from agent_service.prompt_builders import build_copilot_prompt
from config.plan_step_format import STEP_LABEL_RE, step_format_instructions
from config.prompts import COPILOT_TOOL_PROMPT, PLAN_PROMPT
from reasoning_layer.investigation_tasks import parse_declared_step_source, tag_step_sources


# ---------------------------------------------------------------------------
# PLAN_PROMPT no longer bakes a literal "+" into the step-authoring contract
# ---------------------------------------------------------------------------


def test_plan_prompt_never_instructs_a_literal_plus_separator():
    """The exact bug: PLAN_PROMPT used to read
    '... = "[Label] + [clause]." ...'. That literal glue text is what the
    LLM reproduced verbatim in every step. It must never come back."""
    assert " + [" not in PLAN_PROMPT
    assert '"+"' not in PLAN_PROMPT or "never a literal" in PLAN_PROMPT


def test_plan_prompt_uses_the_shared_label_convention():
    """PLAN_PROMPT's MANDATORY STEP FORMAT must be built from
    config.plan_step_format, not a hand-typed duplicate, so the prompt's
    prose and the parser's regex can never drift apart again."""
    assert step_format_instructions() in PLAN_PROMPT
    assert "**[TaskName" in PLAN_PROMPT


def test_plan_prompt_explicitly_forbids_plus_as_a_join_character():
    assert 'never a literal "+"' in PLAN_PROMPT


# ---------------------------------------------------------------------------
# The parser recovers label/rationale from the mandated convention, and
# never invents a split for text that doesn't follow it.
# ---------------------------------------------------------------------------


def test_step_label_re_matches_the_mandated_convention():
    match = STEP_LABEL_RE.match(
        "**Check DTA Beacon Database:** to confirm John Smith's benefit status."
    )
    assert match is not None
    assert match.group("label") == "Check DTA Beacon Database"
    assert match.group("rationale") == "to confirm John Smith's benefit status."


def test_step_label_re_does_not_match_plus_joined_text():
    """A step written the OLD, buggy way must not be mistaken for the new
    convention -- it should simply fail to match, not be mis-parsed."""
    match = STEP_LABEL_RE.match(
        "Check DTA Beacon Database + to confirm John Smith's benefit status."
    )
    assert match is None


def test_parse_declared_step_source_splits_label_and_rationale_with_no_plus():
    raw = (
        "**Check DTA Beacon Database:** to confirm John Smith's current benefit "
        "status and identify any discrepancies in reported income that may affect "
        "his eligibility for SNAP. (Source: BSI catalogue)"
    )
    parsed = parse_declared_step_source(raw, rule_aware_tasks=[])

    assert "+" not in parsed["action"]
    assert parsed["label"] == "Check DTA Beacon Database"
    assert parsed["rationale"].startswith("to confirm John Smith's current benefit status")
    assert parsed["source"] == "catalog"


def test_parse_declared_step_source_leaves_label_rationale_none_for_free_text():
    """A bare human-edited step ("Testing") does not follow the **label:**
    convention at all -- the parser must NOT invent a label/rationale
    split for it. This is the data-level guarantee that backs the
    Copilot verbatim-reproduction guardrail: there is nothing here for a
    downstream consumer to mistakenly treat as "the step's rationale"."""
    parsed = parse_declared_step_source("Testing", rule_aware_tasks=[])

    assert parsed["action"] == "Testing"
    assert parsed["label"] is None
    assert parsed["rationale"] is None


def test_tag_step_sources_propagates_label_and_rationale():
    steps = [{"step": 1, "action": "**Check DTA Beacon Database:** to confirm status. (Source: BSI catalogue)"}]
    tagged = tag_step_sources(steps, rule_aware_tasks=[], catalog_tasks=[])

    assert tagged[0]["label"] == "Check DTA Beacon Database"
    assert "+" not in tagged[0]["action"]
    assert "+" not in tagged[0]["rationale"]


def test_tag_step_sources_does_not_add_label_rationale_for_terse_steps():
    steps = [{"step": 4, "action": "Testing"}]
    tagged = tag_step_sources(steps, rule_aware_tasks=[], catalog_tasks=[])

    assert "label" not in tagged[0]
    assert "rationale" not in tagged[0]
    assert tagged[0]["action"] == "Testing"


# ---------------------------------------------------------------------------
# COPILOT_TOOL_PROMPT: verbatim-reproduction guardrail
# ---------------------------------------------------------------------------


def test_copilot_prompt_requires_verbatim_step_reproduction():
    assert "reproduced EXACTLY" in COPILOT_TOOL_PROMPT
    assert "human_approved" in COPILOT_TOOL_PROMPT
    assert "do not manufacture a rationale" in COPILOT_TOOL_PROMPT


def test_copilot_prompt_forbids_editorializing_about_step_intent():
    assert "placeholder" in COPILOT_TOOL_PROMPT  # the exact hallucination this bug produced
    assert "unless that characterization is present verbatim" in COPILOT_TOOL_PROMPT


# ---------------------------------------------------------------------------
# build_copilot_prompt: human-approved steps are spliced in unmodified,
# with the defense-in-depth verbatim notice attached.
# ---------------------------------------------------------------------------


def _case_data_with_override(steps):
    return {
        "modified_ai_investigation_plan": {
            "source": "human_approved",
            "steps": steps,
            "modified_by": "j.doe",
            "modified_on": "2026-09-09T12:00:00+00:00",
            "comment": "",
        }
    }


def test_build_copilot_prompt_splices_terse_human_step_unmodified():
    case_data = _case_data_with_override(
        [
            {"step": 1, "action": "**Check DTA Beacon Database:** to confirm status. (Source: BSI catalogue)"},
            {"step": 2, "action": "Testing"},
        ]
    )

    prompt = build_copilot_prompt("CASE-1", case_data)

    # The raw step text must appear byte-for-byte in the serialized
    # context -- build_copilot_prompt only splices data, it must never
    # rewrite, pad, or annotate a step's own text.
    context_json = prompt[prompt.index("{"): prompt.rindex("}") + 1]
    context = json.loads(context_json)
    steps = context["investigation_plan"]["investigation_steps"]

    assert steps[1]["action"] == "Testing"
    assert "+" not in steps[0]["action"]


def test_build_copilot_prompt_attaches_verbatim_notice_for_human_plan():
    case_data = _case_data_with_override([{"step": 1, "action": "Testing"}])

    prompt = build_copilot_prompt("CASE-1", case_data)
    context_json = prompt[prompt.index("{"): prompt.rindex("}") + 1]
    context = json.loads(context_json)

    assert context["investigation_plan"]["_steps_source"] == "human_approved"
    notice = context["investigation_plan"]["_verbatim_notice"]
    assert "do not add, infer, or complete" in notice


def test_build_copilot_prompt_no_verbatim_notice_without_an_override():
    """No human_approved plan present -> no override splice, no notice
    added -- the field must be conditional, never a hardcoded no-op key."""
    prompt = build_copilot_prompt("CASE-1", {"investigation_plan": {"investigation_steps": []}})
    assert "_verbatim_notice" not in prompt
