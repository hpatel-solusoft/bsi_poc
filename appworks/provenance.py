"""
Provenance Tracker
------------------
Standardizes data citations for the LLM UI. 
Acts as a strict gatekeeper to filter out noisy backend AppWorks endpoints
using the centralized configuration.
"""

from datetime import datetime, timezone
from typing import Dict, Any, Set, Optional

# ── Import the Gatekeeper Allowlist from Central Config ──
from config.settings import ALLOWED_ENTITIES 

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
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": computed_by
        }