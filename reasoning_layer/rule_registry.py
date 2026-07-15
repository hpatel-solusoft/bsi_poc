"""
Owns: the Rule Registry — loading rule definitions from config/rules.yaml
and seeding/reading the :InferenceRule nodes described in the Data
Persistence Specification, Section C.1 ("Rule logic stays in Git; rule
parameters live as updatable Neo4j properties").

Rule definitions (wave membership, execution order, default tunable
parameters, names, enabled defaults) live in config/rules.yaml, not in
this file — for the same reason tool config lives in manifest.yaml: it is
data BSI owns and will change, and it must not be buried in code.

Two-layer model, unchanged by the move to YAML:
  1. Rule LOGIC  -> Git, as .cypher files.
  2. Rule DEFAULT config -> Git, as config/rules.yaml (loaded here).
  3. Rule LIVE   config -> Neo4j, as :InferenceRule nodes.

The YAML defaults SEED the Neo4j nodes ON CREATE only. Once a node exists,
this module never overwrites its params — an operator's tuning survives
every deploy. So: edit rules.yaml to change what a fresh environment
starts with; edit the Neo4j node to change a running one.

Does NOT own: rule execution (rule_engine.py) or rule logic (*.cypher).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml

from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
# Accept either spelling of the file. "rules.yaml" is the canonical name,
# but "rule.yaml" (singular) is common enough as a typo/rename that failing
# the whole application boot over one character is not worth it — we look
# for the canonical name first, then fall back. Whichever is found is the
# one loaded; if both somehow exist, the canonical plural wins.
_CANDIDATE_CONFIG_FILES = [_CONFIG_DIR / "rules.yaml", _CONFIG_DIR / "rule.yaml"]


def _resolve_config_file() -> Path:
    for candidate in _CANDIDATE_CONFIG_FILES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Rule registry config not found. Expected one of: "
        + ", ".join(str(c) for c in _CANDIDATE_CONFIG_FILES)
    )


def _load_config() -> Dict[str, Any]:
    """Read and validate the rule registry YAML once at import. A malformed
    or missing rules file must fail loudly at startup — a registry that
    silently loses a rule is far worse than a boot error, because the rule
    just stops firing in production with no signal."""
    config_file = _resolve_config_file()
    config = yaml.safe_load(config_file.read_text())
    if not isinstance(config, dict) or "waves" not in config or "rules" not in config:
        raise ValueError(f"{config_file} must define top-level 'waves' and 'rules'")

    waves = config["waves"]
    for key in ("wave_1", "wave_2", "modifier"):
        if key not in waves:
            raise ValueError(f"{config_file} 'waves' is missing '{key}'")

    # Every rule referenced in the wave lists must have a definition, and vice
    # versa — a rule in a wave list with no definition (or a defined rule in
    # no wave) is a config mistake that would otherwise surface much later as
    # a KeyError deep in execution.
    listed = list(waves["wave_1"]) + list(waves["wave_2"]) + [waves["modifier"]]
    defined = set(config["rules"])
    missing_def = [rid for rid in listed if rid not in defined]
    if missing_def:
        raise ValueError(f"Rules listed in waves but not defined in 'rules': {missing_def}")
    orphan = defined - set(listed)
    if orphan:
        raise ValueError(f"Rules defined but not placed in any wave: {sorted(orphan)}")

    return config


_CONFIG = _load_config()

# --- public constants, derived from YAML, names unchanged from before ---
# Wave membership is by explicit list, NOT numeric range (Reference doc
# Section 5.4: Rules 7, 8, 10 and 11 all fall outside a clean split). The
# list order IS the execution order and encodes real dependencies (Rule 8
# reads Rule 7 and the network rules; Rule 13 reads Rule 7).
WAVE_1_RULE_IDS: List[str] = list(_CONFIG["waves"]["wave_1"])
WAVE_2_RULE_IDS: List[str] = list(_CONFIG["waves"]["wave_2"])
MODIFIER_RULE_ID: str = _CONFIG["waves"]["modifier"]
ALL_RULE_IDS: List[str] = WAVE_1_RULE_IDS + WAVE_2_RULE_IDS + [MODIFIER_RULE_ID]

# rule_id -> full definition dict {name, wave, enabled, params}
_RULE_DEFS: Dict[str, Dict[str, Any]] = _CONFIG["rules"]
# Convenience views kept for parity with the previous module surface.
_DEFAULT_PARAMS: Dict[str, Dict[str, Any]] = {
    rid: dict(defn.get("params") or {}) for rid, defn in _RULE_DEFS.items()
}
_RULE_NAMES: Dict[str, str] = {
    rid: defn.get("name", rid) for rid, defn in _RULE_DEFS.items()
}


_SEED_QUERY = """
UNWIND $rules AS rule
MERGE (r:InferenceRule {rule_id: rule.rule_id})
ON CREATE SET r += rule.params,
              r.rule_name  = rule.rule_name,
              r.wave       = rule.wave,
              r.enabled    = rule.enabled,
              r.created_at = datetime()
ON MATCH SET  r.rule_name = rule.rule_name,
              r.wave      = rule.wave
RETURN count(r) AS n
"""
# ON MATCH refreshes only the descriptive fields. Parameters and the enabled
# flag are never overwritten — an operator who retuned a threshold or
# disabled a noisy rule keeps that change across deploys. This is the whole
# point of the registry pattern, and it is why the YAML defaults only ever
# apply ON CREATE.

_READ_QUERY = """
MATCH (r:InferenceRule)
RETURN r.rule_id AS rule_id, r.enabled AS enabled, properties(r) AS props
"""


def ensure_registry() -> int:
    """Seed any missing :InferenceRule node from the YAML defaults.
    Idempotent — safe on every startup and every pipeline run."""
    rules = [
        {
            "rule_id": rule_id,
            "rule_name": _RULE_NAMES[rule_id],
            "wave": _RULE_DEFS[rule_id].get("wave", 0),
            "enabled": bool(_RULE_DEFS[rule_id].get("enabled", True)),
            "params": _DEFAULT_PARAMS.get(rule_id, {}),
        }
        for rule_id in ALL_RULE_IDS
    ]
    with get_session() as session:
        record = session.run(_SEED_QUERY, rules=rules).single()
    count = int(record["n"]) if record else 0
    logger.info("rule_registry: %d :InferenceRule nodes present (seeded from rules config)", count)
    return count


def load_registry() -> Dict[str, Dict[str, Any]]:
    """
    Return {rule_id: {"enabled": bool, "params": {...}}} for every
    registered rule, reading the LIVE values from Neo4j (not the YAML
    defaults) so an operator's tuning takes effect on the next run with no
    deploy.

    Falls back to the YAML defaults if the registry is empty — which
    happens exactly once, on a graph where ensure_registry() has never run.
    Never silently runs a rule with no parameters at all, because a rule
    whose allegation-type list is empty matches nothing and would look like
    "the rule ran and found nothing" rather than "the rule was never
    configured".
    """
    with get_session() as session:
        rows = session.run(_READ_QUERY).data()

    if not rows:
        logger.warning("rule_registry: empty — seeding from rules config defaults")
        ensure_registry()
        return {
            rule_id: {"enabled": bool(_RULE_DEFS[rule_id].get("enabled", True)),
                      "params": dict(params)}
            for rule_id, params in _DEFAULT_PARAMS.items()
        }

    registry: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        props = dict(row["props"])
        for meta in ("rule_id", "rule_name", "wave", "enabled", "created_at"):
            props.pop(meta, None)
        registry[row["rule_id"]] = {
            "enabled": row["enabled"] if row["enabled"] is not None else True,
            "params": props,
        }
    return registry