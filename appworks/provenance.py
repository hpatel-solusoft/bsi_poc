"""
Provenance Tracking Utility
---------------------------
Ensures strict compliance with Principle 8 of the BSI Architecture.
Accumulates AppWorks record identifiers during deep entity traversal and 
generates the standardized {sources, retrieved_at, computed_by} envelope.
"""

from datetime import datetime, timezone
from typing import Dict, Any, Set, List
from config.settings import PRIMARY_BUSINESS_ENTITIES  # Import from config

class ProvenanceTracker:
       
    def __init__(self, base_entity_type: str, base_entity_id: str):
        self.sources: Set[str] = set()
        if base_entity_id:
            self.add_source(base_entity_type, base_entity_id)

    def add_source(self, entity_type: str, entity_id: str) -> None:
        if entity_id and entity_type in PRIMARY_BUSINESS_ENTITIES:
            display_name = "Subject" if entity_type == "SubjectDetail" else entity_type
            self.sources.add(f"AppWorks {display_name} record {entity_id}")

    def get_provenance_block(self, computed_by: str = "AppWorks REST retrieval") -> Dict[str, Any]:
        """
        Builds the final provenance dictionary required by the dispatcher envelope.
        """
        return {
            "sources": sorted(list(self.sources)),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": computed_by
        }