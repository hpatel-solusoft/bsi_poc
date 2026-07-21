"""
Owns: loading rule .cypher files off disk and executing them against
Neo4j with the right parameters, in the right order, returning one
execution record per rule.

Does NOT own: which rules belong to which wave (rule_registry.py), what
the rules say (rules/*.cypher), the six-step sequence (pipeline.py), or
the rules_fired contract (rules_fired.py).

Every rule is a write query (Principle 12: "rules are write operations,
not fetches") and every rule write is idempotent MERGE/SET (Principle
15), which is what makes the no-resume, full-re-run failure policy safe.

One rule per transaction, deliberately, rather than one transaction for
the whole wave: rules within a wave have real dependencies on each
other's writes (Rule 8 reads Rules 7 and 2/4/6/9), and a single
long-running transaction would not see its own earlier writes the way
these rules need to. A wave is not atomic; the graph is idempotent
instead, which is the trade the architecture already made.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from neo4j.exceptions import Neo4jError

from reasoning_layer import rule_registry
from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

_RULES_DIR = Path(__file__).parent / "rules"

# rule_id -> cypher file. Kept explicit rather than derived from a glob:
# a rule file appearing in the directory should not silently start
# executing, and a rule disappearing should break loudly at startup
# rather than quietly stop firing.
RULE_FILES: Dict[str, Path] = {
    "Rule_01_Shared_Employer":         _RULES_DIR / "wave1" / "rule_01_shared_employer.cypher",
    "Rule_03_Shared_Address":          _RULES_DIR / "wave1" / "rule_03_shared_address.cypher",
    "Rule_05_Alias_Identity":          _RULES_DIR / "wave1" / "rule_05_alias_identity.cypher",
    "Rule_10_Merged_Case_Propagation": _RULES_DIR / "wave1" / "rule_10_merged_case_propagation.cypher",
    "Rule_11_Cross_Case_Hub":          _RULES_DIR / "wave1" / "rule_11_cross_case_hub.cypher",

    "Rule_02_Employer_Fraud_Network":  _RULES_DIR / "wave2" / "rule_02_employer_fraud_network.cypher",
    "Rule_04_Address_Fraud_Network":   _RULES_DIR / "wave2" / "rule_04_address_fraud_network.cypher",
    "Rule_06_Identity_Fraud_Network":  _RULES_DIR / "wave2" / "rule_06_identity_fraud_network.cypher",
    "Rule_07_Prior_Guilty":            _RULES_DIR / "wave2" / "rule_07_prior_guilty.cypher",
    "Rule_08_Recidivist_Escalation":   _RULES_DIR / "wave2" / "rule_08_recidivist_escalation.cypher",
    "Rule_09_PCA_CheckSplit":          _RULES_DIR / "wave2" / "rule_09_pca_checksplit.cypher",
    "Rule_12_SLAM_Wage_Corroboration": _RULES_DIR / "wave2" / "rule_12_slam_wage_corroboration.cypher",
    "Rule_13_FastTrack_Escalation":    _RULES_DIR / "wave2" / "rule_13_fasttrack_escalation.cypher",

    "Rule_14_Confirmation_Elevation":  _RULES_DIR / "modifiers" / "rule_14_confirmation_elevation.cypher",
}

_cypher_cache: Dict[str, str] = {}


def _load_cypher(rule_id: str) -> str:
    if rule_id not in _cypher_cache:
        path = RULE_FILES[rule_id]
        if not path.exists():
            raise FileNotFoundError(f"Rule file missing for {rule_id}: {path}")
        _cypher_cache[rule_id] = path.read_text(encoding="utf-8")
        print(_cypher_cache[rule_id])
    return _cypher_cache[rule_id]


def verify_rule_files() -> List[str]:
    """Fail-fast check that every registered rule has a file on disk.
    Called at startup so a missing rule surfaces as a boot error rather
    than as a rule that mysteriously never fires in production."""
    missing = [rule_id for rule_id, path in RULE_FILES.items() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing rule .cypher files: {missing}")
    return sorted(RULE_FILES)


def execute_rules(rule_ids: List[str], scope: Dict[str, Any],
                  registry: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Execute `rule_ids` in the given order against the resolved scope.

    Returns one record per rule:
        {rule_id, rule_file, executed, writes, skipped_reason, duration_ms}

    A rule disabled in the registry is reported executed=False with a
    skipped_reason, never silently omitted — "the rule was turned off"
    and "the rule found nothing" must never look the same in the output,
    because an investigator seeing an empty result is entitled to know
    which one it was.

    Raises Neo4jError / GraphUnavailableError straight through: the
    caller (pipeline.py) owns Principle 15's failure handling and this
    module must not swallow a failure into a plausible-looking zero.
    """
    asserted_at = datetime.now(timezone.utc).isoformat()
    results: List[Dict[str, Any]] = []

    with get_session() as session:
        for rule_id in rule_ids:
            entry = registry.get(rule_id, {"enabled": True, "params": {}})

            if not entry.get("enabled", True):
                logger.info("rule_engine: %s SKIPPED (disabled in :InferenceRule registry)", rule_id)
                results.append({
                    "rule_id": rule_id, "rule_file": RULE_FILES[rule_id].name,
                    "executed": False, "writes": 0,
                    "skipped_reason": "disabled_in_registry", "duration_ms": 0,
                })
                continue

            params = {
                "case_id": scope["case_id"],
                "subject_id": scope["primary_subject_id"],
                "scope_subject_ids": scope["scope_subject_ids"],
                "scope_case_ids": scope["scope_case_ids"],
                "asserted_at": asserted_at,
                **entry.get("params", {}),
            }
            print(f"rule_engine: {rule_id} params={params}")
            started = datetime.now(timezone.utc)
            try:
                record = session.execute_write(
                    lambda tx, q=_load_cypher(rule_id), p=params: tx.run(q, **p).single()
                )
                print(lambda tx, q=_load_cypher(rule_id), p=params: tx.run(q, **p).single())
                print(record)
                
            except Neo4jError:
                logger.exception("rule_engine: %s FAILED", rule_id)
                raise

            writes = int(record["writes"]) if record and record["writes"] is not None else 0
            duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

            logger.info("rule_engine: %s executed writes=%d (%dms)", rule_id, writes, duration_ms)
            results.append({
                "rule_id": rule_id, "rule_file": RULE_FILES[rule_id].name,
                "executed": True, "writes": writes,
                "skipped_reason": None, "duration_ms": duration_ms,
            })

    return results


def run_wave1(scope: Dict[str, Any], registry: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Step 2 — structural rules. No LLM, no attribution dependency."""
    return execute_rules(rule_registry.WAVE_1_RULE_IDS, scope, registry)


def run_wave2(scope: Dict[str, Any], registry: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Step 5 — attribution-dependent rules, in dependency order (Rule 8
    reads Rule 7's and the network rules' writes; Rule 13 reads Rule 7's).
    Do not reorder."""
    return execute_rules(rule_registry.WAVE_2_RULE_IDS, scope, registry)


def run_modifier(scope: Dict[str, Any], registry: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rule 14 — the cross-cutting corroboration modifier. Runs last, once
    both waves have written their relationships and the Extraction Stage
    has recorded which of them the narrative independently confirms."""
    return execute_rules([rule_registry.MODIFIER_RULE_ID], scope, registry)