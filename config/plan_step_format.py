"""
Owns: the textual contract for how one AI-generated investigation step is
written, shared between the prompt that instructs the LLM to produce it
(config.prompts.PLAN_PROMPT) and the parser that reads it back
(reasoning_layer.investigation_tasks.parse_declared_step_source).

WHY THIS MODULE EXISTS
-----------------------
Before this module existed, the step-label/rationale separator lived only
as a literal " + " typed directly into PLAN_PROMPT's prose — nowhere else
in the codebase referenced or depended on that character. Two things went
wrong as a result:

1. The LLM reproduced the literal "+" verbatim in its markdown output
   (because that IS what was instructed), so every investigation step
   shown to the investigator — in the Investigation Plan tab and in any
   Copilot answer quoting a step back — displayed a stray, unexplained
   "+" glyph between the task label and its rationale.
2. Nothing downstream (reasoning_layer.investigation_tasks) ever parsed
   on that "+" — it was decorative prose, not a real delimiter — so the
   "+" carried no structural meaning and was pure UI noise.

The fix is not to invent a new hidden delimiter (that would just move the
same class of bug elsewhere); it is to use ordinary, idiomatic markdown
that already reads correctly if a person looks at the raw text: a bold
label followed by a colon, exactly the convention
api.response_builders.format_plan_markdown_item already uses for every
other numbered item in this app ("- **N:** label (meta)"). A colon-joined
sentence is also unambiguous to split on programmatically (the label is
always the first bolded run), so no separate hidden token is needed for
reasoning_layer.investigation_tasks to recover a step's source tag.

Both PLAN_PROMPT and the parser import their delimiter from here. If this
contract ever needs to change again, it changes in exactly one place.
"""

from __future__ import annotations

import re

# The LLM must wrap the verbatim task/rule label in markdown bold and
# close it with a colon, e.g. "**Check DTA Beacon Database:** ...". This
# is deliberately plain, human-readable markdown -- not a synthetic
# delimiter -- so the raw text is already presentation-ready even before
# any downstream parsing happens.
STEP_LABEL_OPEN = "**"
STEP_LABEL_CLOSE = ":**"

# The three source-attribution tags PLAN_PROMPT requires every step to
# close with. SOURCE_TAG_RULE_TEMPLATE takes the fired rule's id.
SOURCE_TAG_CATALOG = "(Source: BSI catalogue)"
SOURCE_TAG_ANALYST = "(Source: analyst-recommended)"
SOURCE_TAG_RULE_TEMPLATE = "(Source: Inference Rule — {source_rule})"

# Recovers {"label": ..., "rationale": ...} from one step's cleaned text
# (after any leading "**Step N:**" prefix and trailing "(Source: ...)"
# tag have already been stripped by the caller). Matches only the
# STEP_LABEL_OPEN/STEP_LABEL_CLOSE-wrapped convention above -- never a
# "+"-joined or otherwise ad-hoc format, so a step that does not follow
# the mandated contract simply fails to match rather than being
# mis-parsed.
STEP_LABEL_RE = re.compile(
    rf"^\{STEP_LABEL_OPEN}\s*(?P<label>.+?)\s*{re.escape(STEP_LABEL_CLOSE)}\s*(?P<rationale>.+)$",
    re.DOTALL,
)


def step_format_instructions() -> str:
    """Render PLAN_PROMPT's MANDATORY STEP FORMAT paragraph from the
    constants above, so the prompt's prose and the parser's regex are
    generated from -- and can never disagree with -- the same values."""
    return (
        f'each step = "{STEP_LABEL_OPEN}[TaskName or rule task_type, verbatim as the lead '
        f'clause]{STEP_LABEL_CLOSE} [one synthesized clause applying it to this case\'s specific '
        f'subject/system/record, using case facts]." The task label must open the sentence '
        f"verbatim, wrapped exactly as {STEP_LABEL_OPEN}label{STEP_LABEL_CLOSE} — do not "
        f"paraphrase it into the middle, do not drop it, and do not join the label and the "
        f'clause with any other character (never a literal "+", "-", or bullet). Close the '
        f'sentence with "{SOURCE_TAG_RULE_TEMPLATE}" or "{SOURCE_TAG_CATALOG}" as appropriate.'
    )
