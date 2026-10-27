"""
Owns: loading config/taxonomy.yaml (allegation types grouped into fraud
categories) and keeping the :Category / :AllegationType nodes in Neo4j
identical to that file.

The YAML is the single source of truth. On every startup (called from
reasoning_layer/apply_schema.py, right after the rule registry) Neo4j is
made to match it: missing nodes are created, a type that moved to another
category is re-linked, and types or categories no longer in the file are
deleted. Same posture as rule_registry.py: a malformed file fails loudly
at startup rather than silently dropping a type.

Graph shape written here:
    (:AllegationType {code, description, match_keys})-[:BELONGS_TO]->(:Category {name})

`match_keys` holds the normalized code, description and aliases of a type.
etl/graph_sync.py compares each allegation's normalized text against it to
create (Case)-[:HAS_ALLEGATION_TYPE]->(AllegationType) links, so the
normalization rule lives in exactly one place: normalize_match_key().

Does NOT own: the case-to-type links (etl/graph_sync.py) or reading the
taxonomy for a case (reasoning_layer/network/fraud_patterns.py).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

TAXONOMY_FILE = Path(__file__).resolve().parent.parent / "config" / "taxonomy.yaml"
_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True)
class AllegationTypeSpec:
    """One allegation type and the category it belongs to."""

    code: str
    description: str
    category: str
    match_keys: Tuple[str, ...]


def normalize_match_key(text: Any) -> str:
    """Trim, collapse repeated whitespace and lowercase. None becomes ''."""
    return _WHITESPACE_RUN.sub(" ", str(text or "").strip()).lower()


def _text_field(entry: Dict[str, Any], field: str, where: str) -> str:
    """Return a required non-blank string field, or raise a clear error."""
    value = str(entry.get(field) or "").strip()
    if not value:
        raise ValueError(f"{TAXONOMY_FILE.name}: {where} is missing '{field}'")
    return value


def _build_spec(entry: Any, category: str) -> AllegationTypeSpec:
    """Validate one type entry and compute its match keys."""
    if not isinstance(entry, dict):
        raise ValueError(f"{TAXONOMY_FILE.name}: category '{category}' has a type that is not a mapping")
    code = _text_field(entry, "code", f"a type in category '{category}'")
    description = _text_field(entry, "description", f"type '{code}'")
    raw_keys = [code, description, *(entry.get("aliases") or [])]
    keys = tuple(dict.fromkeys(k for k in map(normalize_match_key, raw_keys) if k))
    return AllegationTypeSpec(code=code, description=description, category=category, match_keys=keys)


def _check_unique(specs: List[AllegationTypeSpec]) -> None:
    """Reject a duplicate code, or a match key claimed by two types."""
    codes: set = set()
    owners: Dict[str, str] = {}
    for spec in specs:
        if spec.code in codes:
            raise ValueError(f"{TAXONOMY_FILE.name}: duplicate allegation type code '{spec.code}'")
        codes.add(spec.code)
        for key in spec.match_keys:
            if owners.setdefault(key, spec.code) != spec.code:
                raise ValueError(f"{TAXONOMY_FILE.name}: '{key}' matches both {owners[key]} and {spec.code}")


def parse_taxonomy(raw: Any) -> List[AllegationTypeSpec]:
    """Turn the parsed YAML into validated specs, in file order."""
    categories = raw.get("categories") if isinstance(raw, dict) else None
    if not categories:
        raise ValueError(f"{TAXONOMY_FILE.name} is empty: it must define a non-empty 'categories' list")
    specs: List[AllegationTypeSpec] = []
    for category in categories:
        name = _text_field(category, "name", "a category")
        if not category.get("types"):
            raise ValueError(f"{TAXONOMY_FILE.name}: category '{name}' has no types")
        specs.extend(_build_spec(entry, name) for entry in category["types"])
    _check_unique(specs)
    return specs


def load_taxonomy(path: Path = TAXONOMY_FILE) -> List[AllegationTypeSpec]:
    """Read and validate the taxonomy file. Raises on a missing or bad file."""
    return parse_taxonomy(yaml.safe_load(path.read_text(encoding="utf-8")))


_SYNC_QUERY = """
UNWIND $types AS spec
MERGE (cat:Category {name: spec.category})
MERGE (t:AllegationType {code: spec.code})
SET t.description = spec.description,
    t.match_keys  = spec.match_keys
WITH t, cat
OPTIONAL MATCH (t)-[stale:BELONGS_TO]->(other:Category)
WHERE other <> cat
DELETE stale
WITH DISTINCT t, cat
MERGE (t)-[:BELONGS_TO]->(cat)
RETURN count(DISTINCT t) AS n
"""

_REMOVE_TYPES_QUERY = """
MATCH (t:AllegationType)
WHERE NOT t.code IN $codes
DETACH DELETE t
RETURN count(t) AS n
"""

_REMOVE_EMPTY_CATEGORIES_QUERY = """
MATCH (cat:Category)
WHERE NOT EXISTS { MATCH (cat)<-[:BELONGS_TO]-(:AllegationType) }
DETACH DELETE cat
RETURN count(cat) AS n
"""


def _count(session, query: str, **params: Any) -> int:
    """Run one statement and return its `n` count (0 when no row)."""
    record = session.run(query, **params).single()
    return int(record["n"]) if record and record["n"] is not None else 0


def ensure_taxonomy() -> int:
    """Make Neo4j's taxonomy match config/taxonomy.yaml. Idempotent.

    Returns the number of allegation types present afterwards.
    """
    specs = load_taxonomy()
    rows = [
        {
            "code": s.code,
            "description": s.description,
            "category": s.category,
            "match_keys": list(s.match_keys),
        }
        for s in specs
    ]
    with get_session() as session:
        loaded = _count(session, _SYNC_QUERY, types=rows)
        removed_types = _count(session, _REMOVE_TYPES_QUERY, codes=[s.code for s in specs])
        removed_categories = _count(session, _REMOVE_EMPTY_CATEGORIES_QUERY)
    logger.info(
        "taxonomy_registry: %d allegation types loaded, %d types and %d categories removed",
        loaded,
        removed_types,
        removed_categories,
    )
    return loaded
