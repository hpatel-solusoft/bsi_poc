"""
Owns: applying reasoning_layer/schema.cypher (and seeding the
:InferenceRule registry) against a live Neo4j instance.

Exists because schema.cypher previously had no runner: the deploy
instructions said "pipe it into cypher-shell", which means a developer
who forgets is running the entire rule library against an unconstrained
graph — where every MERGE is a label scan and concurrent ingests
silently create duplicate :Employer nodes that Rule 1 then fails to
match across. This makes the schema step something the app can do to
itself, idempotently, rather than something a human has to remember.

Usage:
    python -m reasoning_layer.apply_schema

Also called from api/server.py's startup hook when
NEO4J_APPLY_SCHEMA_ON_STARTUP is set (default: on, since every statement
is IF NOT EXISTS and a no-op on an already-provisioned graph).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List

from reasoning_layer import rule_registry
from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

_SCHEMA_FILE = Path(__file__).parent / "schema.cypher"


def _statements() -> List[str]:
    """Neo4j executes one statement per call — split the file, strip
    comments and blanks, keep the order."""
    raw = _SCHEMA_FILE.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if not line.strip().startswith("//")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def apply_schema() -> int:
    """Apply every constraint/index, then seed the rule registry. Returns
    the number of statements executed. Idempotent."""
    statements = _statements()
    with get_session() as session:
        for statement in statements:
            session.run(statement)
    logger.info("apply_schema: %d constraint/index statements applied", len(statements))

    rule_registry.ensure_registry()
    return len(statements)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        count = apply_schema()
    except Exception as exc:  # noqa: BLE001 — CLI entry point: report and exit non-zero
        print(f"FAILED to apply schema: {exc}")
        sys.exit(1)
    print(f"OK — {count} constraints/indexes applied, :InferenceRule registry seeded.")