"""
Owns: the LLM call that performs Narrative Extraction (Python
Implementation Reference, Section 5.3 Step 3) — turns the narrative
records reasoning_layer/commentary_reader.py pulled from Neo4j into
validated candidate allegation-to-subject attributions. Does not read
Neo4j (that's commentary_reader.py) and does not write to Neo4j (that's
graph_load.py) — this module's whole job is the extraction reasoning
step in between.

Reached only from reasoning_layer/pipeline.py, never directly from the
dispatcher — Narrative Extraction is not itself a manifest.yaml tool
(Section 5.3 describes it as an internal pipeline step, not an
agent-facing capability), so this module has no dispatcher-facing
entry point of its own.

Deliberately does not import agent_service.agent_runner. Layer 4
(reasoning_layer) is a peer of Layer 2 (agent_service + semantic_layer),
not a caller of it (Four-Layer Architecture, Section 1) — this module
owns its own OpenAI client rather than reusing BSIAgentRunner's, the
same way appworks_services.py (Layer 3) never reaches into agent_runner
either. It reads config/prompts.py's template and
agent_service/prompt_builders.py's renderer, both of which are shared
prompt-construction infrastructure, not agent-loop logic.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from openai import OpenAI
from pydantic import ValidationError

from agent_service.prompt_builders import build_extraction_prompt
from semantic_layer.entity_contracts import ExtractionResult

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _client


def _model() -> str:
    # Falls back to the same default agent_runner.py uses, so a single
    # OPENAI_MODEL env var controls both agent-facing and internal
    # reasoning-layer LLM calls unless BSI_EXTRACTION_MODEL is set to
    # deliberately diverge (e.g. a cheaper/faster model for a
    # structured-extraction task versus the conversational agents).
    return os.environ.get("BSI_EXTRACTION_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))


def _envelope(result: Dict[str, Any], computed_by: str) -> dict:
    return {
        "result": result,
        "provenance": {
            "sources": ["Neo4j :Commentary / :Allegation narrative fields"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": computed_by,
        },
    }


def _flatten_allegation_ids(narrative_records: Dict[str, Any]) -> List[str]:
    return [
        row["allegation_id"]
        for row in narrative_records.get("allegations", [])
        if row.get("allegation_id")
    ]


def _has_any_commentary(narrative_records: Dict[str, Any]) -> bool:
    return any(
        row.get("commentary")
        for row in narrative_records.get("allegations", [])
    )


def _call_llm_json(prompt: str) -> dict:
    """
    One JSON-mode completion call. Raises json.JSONDecodeError if the
    model still returns non-JSON despite response_format — callers
    retry once (run_extraction) before treating it as a hard failure.
    """
    response = _get_client().chat.completions.create(
        model=_model(),
        messages=[{"role": "user", "content": prompt}],
        temperature=0,  # extraction is a structured-reasoning task, not
                        # a creative one — determinism is preferred over
                        # varied phrasing across pipeline runs.
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    return json.loads(raw)


def run_extraction(subject_id: str, narrative_records: Dict[str, Any]) -> dict:
    """
    Runs Step 3 (Narrative Extraction) for one subject.

    Short-circuits the LLM call entirely when there is no commentary at
    all anywhere in this subject's case history — every allegation is
    reported unresolved without spending a model call on empty input,
    which is both cheaper and more deterministic than asking an LLM to
    conclude "nothing here" on its own.

    Returns the standard {result, provenance} envelope (Principle 8),
    where result is ExtractionResult.model_dump().

    Raises ValueError if the LLM's JSON — even after one retry — still
    fails validation against ExtractionResult. pipeline.py treats this
    as a genuine Wave-1-succeeded-but-this-step-failed error and applies
    Principle 15's failure handling, the same as a Neo4j outage during
    Wave 1.
    """
    all_allegation_ids = _flatten_allegation_ids(narrative_records)

    if not all_allegation_ids:
        logger.info(
            "extraction_stage: subject_id=%s has no allegations in scope — "
            "nothing to extract", subject_id,
        )
        result = ExtractionResult(subject_id=subject_id).model_dump()
        return _envelope(result, "reasoning_layer.extraction_stage.run_extraction")

    if not _has_any_commentary(narrative_records):
        logger.info(
            "extraction_stage: subject_id=%s has %d allegation(s) but zero "
            "commentary — skipping the LLM call, all marked unresolved",
            subject_id, len(all_allegation_ids),
        )
        result = ExtractionResult(
            subject_id=subject_id,
            unresolved_allegation_ids=all_allegation_ids,
        ).model_dump()
        return _envelope(result, "reasoning_layer.extraction_stage.run_extraction")

    prompt = build_extraction_prompt(
        subject_id,
        narrative_records["allegations"],
        # Rule 14's input. Wave 1 has already run by the time the Extraction
        # Stage is called (Section 5.3's step order), so these relationships
        # exist and can be offered to the LLM as confirmable candidates. If
        # the list is empty, the model correctly returns no corroborations.
        narrative_records.get("structural_relationships", []),
    )

    parsed: dict | None = None
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            parsed = _call_llm_json(prompt)
            break
        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning(
                "extraction_stage: subject_id=%s attempt=%d returned invalid JSON: %s",
                subject_id, attempt, exc,
            )

    if parsed is None:
        raise ValueError(
            f"Extraction Stage LLM call for subject_id={subject_id!r} returned "
            f"invalid JSON on both attempts: {last_error}"
        )

    try:
        validated = ExtractionResult(**parsed)
    except ValidationError as exc:
        raise ValueError(
            f"Extraction Stage LLM output for subject_id={subject_id!r} failed "
            f"schema validation: {exc}"
        ) from exc

    logger.info(
        "extraction_stage: subject_id=%s attributions=%d unresolved=%d corroborations=%d",
        subject_id, len(validated.attributions), len(validated.unresolved_allegation_ids),
        len(validated.corroborations),
    )
    return _envelope(validated.model_dump(), "reasoning_layer.extraction_stage.run_extraction")
