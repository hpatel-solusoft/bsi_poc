"""
Provenance
----------
Single source of truth for the {sources, retrieved_at, computed_by}
provenance envelope, for BOTH data paths:

  * ProvenanceTracker  — AppWorks REST retrievals. Accumulates individual
    record citations through a gatekeeper allowlist, because an AppWorks
    answer is assembled from many records and each one must be nameable.

  * graph_provenance / graph_envelope — Neo4j reasoning-layer results.
    A graph answer comes from ONE query against an already-reasoned graph,
    so there are no per-record IDs to gate; the citation is the query
    itself plus which function ran it.

Both produce the identical envelope shape, so nothing downstream —
merge_provenance, CASE_STORE, the Data Sources renderer — can tell them
apart or needs to care.

The timestamp is generated in exactly one place (_now_iso). It was
previously re-derived in a dozen modules, which is how blank and
inconsistent retrieved_at values got into the trail.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Set

# ── Import the Gatekeeper Allowlist from Central Config ──
from config.settings import ALLOWED_ENTITIES 

# ── Canonical source labels ──
# Named so a typo cannot silently create a second spelling of the same
# source, which would defeat de-duplication in merge_provenance.
GRAPH_QUERY = "Neo4j graph query"
REASONING_PIPELINE = "reasoning pipeline"


def _now_iso() -> str:
    """UTC, ISO-8601. The one place a provenance timestamp is created."""
    return datetime.now(timezone.utc).isoformat()


def graph_provenance(
    computed_by: str,
    sources: Optional[Sequence[str]] = None,
    retrieved_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the provenance block for a reasoning-layer (Neo4j) result.

    Args:
        computed_by:  the function that produced the result, e.g.
                      "reasoning_layer.similar_cases".
        sources:      what was read or written. Defaults to a single
                      graph-query citation, which is the common case.
        retrieved_at: override only when the caller already has the
                      authoritative moment — a write path should cite
                      when the write was ASSERTED, not when the envelope
                      happened to be built.

    computed_by is required and unvalidated by design: an empty or wrong
    value is a bug worth seeing in the trail, not one worth hiding behind
    a default.
    """
    if sources is None:
        resolved = [GRAPH_QUERY]
    else:
        # Preserve caller order (it is meaningful — pipeline before query)
        # while dropping blanks and repeats.
        seen, resolved = set(), []
        for src in sources:
            text = str(src).strip()
            if text and text not in seen:
                seen.add(text)
                resolved.append(text)

    return {
        "sources": resolved,
        "retrieved_at": retrieved_at or _now_iso(),
        "computed_by": computed_by,
    }


def graph_envelope(
    result: Any,
    computed_by: str,
    sources: Optional[Sequence[str]] = None,
    retrieved_at: Optional[str] = None,
) -> Dict[str, Any]:
    """The full {result, provenance} envelope every reasoning-layer
    function returns. Identical in shape to an AppWorks tool result."""
    return {
        "result": result,
        "provenance": graph_provenance(computed_by, sources, retrieved_at),
    }


class ProvenanceTracker:
    # UI DISPLAY ALIASES (The Beautifier)
    # Translates internal AppWorks schema names into human-readable investigator terms.
    DISPLAY_NAMES = {
        "SubjectDetail": "Subject",
        "FraudRiskRule": "Risk Rule",
        "Workfolder": "Case File"
    }

    def __init__(self, base_entity_type: str, base_entity_id: Optional[str]):
        self.sources: Set[str] = set()
        self.add_source(base_entity_type, base_entity_id)

    def add_source(self, entity_type: str, entity_id: Optional[str]) -> None:
        """
        Registers a new AppWorks record ONLY if it is a primary business entity.
        Includes aggressive sanitization to drop leaky URLs and internal mapping names.
        """
        # 1. Gatekeeper: Drop if missing ID or unauthorized type
        if not entity_id or entity_type not in ALLOWED_ENTITIES:
            return
            
        # 2. Fix the "ai_summary" name for the UI
        if entity_type == "SystemMemory":
            # Translate technical variable name to a professional UI term
            display_id = "Verified Case Context" if entity_id == "ai_summary" else entity_id
            
            # Prevent duplicate "SystemMemory" entries if called multiple times
            if "Verified Case Context" in display_id:
                self.sources.add("Internal System Memory: Verified Case Context")
            else:
                self.sources.add(f"Internal System Memory: {display_id}")
            return

        # 3. AGGRESSIVE GARBAGE FILTER for AppWorks IDs
        # Real AppWorks business IDs are numeric (e.g., "658407433") or clean alphanumerics.
        # If the ID leaked a URL path ("/") or a relationship name ("_"), silently drop it!
        entity_id_str = str(entity_id)
        if "/" in entity_id_str or "_" in entity_id_str:
            return

        # 4. Map internal names to clean UI names and add to set
        display_name = self.DISPLAY_NAMES.get(entity_type, entity_type)
        self.sources.add(f"AppWorks {display_name} record {entity_id}")


    def get_provenance_block(self, computed_by: str = "AppWorks REST retrieval") -> Dict[str, Any]:
        """Returns the standardized provenance envelope."""
        return {
            "sources": sorted(list(self.sources)),
            "retrieved_at": _now_iso(),
            "computed_by": computed_by
        }