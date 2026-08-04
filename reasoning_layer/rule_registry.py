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
this module never overwrites its params for a rule with no config_version
in YAML — an operator's tuning on those rules survives every deploy.

A rule that DOES declare a config_version opts into the versioned params
sync instead: bumping that number in YAML is how a genuine bug fix to a
rule's default params reaches a running environment without a manual
Neo4j edit, while still never clobbering a live node that is already
caught up to that version (see _SYNC_VERSIONED_PARAMS_QUERY below for the
exact guard). So: edit rules.yaml to change what a fresh environment
starts with; bump config_version + edit rules.yaml to ship a real fix to
a running one; edit the Neo4j node directly only for a one-off operator
tune that should NOT ship to other environments via Git.

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
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
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
_RULE_NAMES: Dict[str, str] = {rid: defn.get("name", rid) for rid, defn in _RULE_DEFS.items()}
# Opt-in only: a rule with no config_version key in YAML is not a
# candidate for the versioned sync below, so its behavior is completely
# unchanged from before this feature existed (seeded ON CREATE, then left
# alone forever). Only rules that explicitly declare a config_version
# participate — see the sync note above Rule_07 in rules.yaml for the
# reasoning.
_CONFIG_VERSIONS: Dict[str, int] = {
    rid: int(defn["config_version"]) for rid, defn in _RULE_DEFS.items() if "config_version" in defn
}


# --------------------------------------------------------------------
# Downstream dependents — which rule reads which OTHER rule's output.
# --------------------------------------------------------------------
#
# This is deliberately a small, fixed table, not something derived from
# the graph at runtime: the dependency between e.g. Rule_01 and Rule_02
# is a fact about the Cypher (Rule_02's MATCH clause reads the
# relationship type Rule_01 writes), not something that changes run to
# run. It exists so that when an investigator rejects an inference,
# rejection.py / rule_audit.py can tell them what ELSE to re-check —
# rejecting Rule_01's SHARES_EMPLOYER_WITH edge should also flag
# Rule_02's MEMBER_OF_FRAUD_NETWORK edge that was built on top of it,
# for example.
#
# Keyed by the UPSTREAM rule_id. Each entry is a
# {"rule_id": <downstream rule_id>, "relationship_type": <the relationship
# type the downstream rule reads FROM the upstream rule's output>} dict.
# relationship_type is what tests/verify.py greps the downstream rule's
# own .cypher file for — if a rule is edited (relationship renamed,
# dependency removed) and this map is not updated to match, the build
# fails loudly instead of this table silently going stale.
#
# Sources, one per edge, so this can be re-verified by hand against the
# .cypher files whenever either side changes:
#   Rule_01 writes SHARES_EMPLOYER_WITH      -> Rule_02 MATCHes it directly.
#   Rule_03 writes SHARES_ADDRESS_WITH       -> Rule_04 MATCHes it directly.
#   Rule_05 writes SHARES_ALIAS_PATTERN_WITH -> Rule_06 MATCHes it directly.
#   Rule_07 writes HAS_PRIOR_GUILTY_CASE     -> Rule_08 MATCHes it (as `pg`)
#                                             -> Rule_13 MATCHes it (as `pg`)
#   Rule_02/04/06/09 each write MEMBER_OF_FRAUD_NETWORK -> Rule_08 MATCHes
#     it (as `mem`), regardless of which of the four networks produced it.
#
# Rule_09_PCA_CheckSplit is NOT a key here and must never become one: it
# reads the RAW IS_CO_SUBJECT_WITH / HAS_WAGE_RECORD_WITH relationships
# loaded straight from ETL, not another rule's inferred output (see its
# own .cypher header — there is no Rule_XX -> Rule_09 edge in this
# ontology). It DOES appear as an upstream key below, since Rule_08
# reads what Rule_09 writes.
DOWNSTREAM_DEPENDENTS: Dict[str, List[Dict[str, str]]] = {
    "Rule_01_Shared_Employer": [
        {"rule_id": "Rule_02_Employer_Fraud_Network", "relationship_type": "SHARES_EMPLOYER_WITH"},
    ],
    "Rule_03_Shared_Address": [
        {"rule_id": "Rule_04_Address_Fraud_Network", "relationship_type": "SHARES_ADDRESS_WITH"},
    ],
    "Rule_05_Alias_Identity": [
        {"rule_id": "Rule_06_Identity_Fraud_Network", "relationship_type": "SHARES_ALIAS_PATTERN_WITH"},
    ],
    "Rule_07_Prior_Guilty": [
        {"rule_id": "Rule_08_Recidivist_Escalation", "relationship_type": "HAS_PRIOR_GUILTY_CASE"},
        {"rule_id": "Rule_13_FastTrack_Escalation", "relationship_type": "HAS_PRIOR_GUILTY_CASE"},
    ],
    "Rule_02_Employer_Fraud_Network": [
        {"rule_id": "Rule_08_Recidivist_Escalation", "relationship_type": "MEMBER_OF_FRAUD_NETWORK"},
    ],
    "Rule_04_Address_Fraud_Network": [
        {"rule_id": "Rule_08_Recidivist_Escalation", "relationship_type": "MEMBER_OF_FRAUD_NETWORK"},
    ],
    "Rule_06_Identity_Fraud_Network": [
        {"rule_id": "Rule_08_Recidivist_Escalation", "relationship_type": "MEMBER_OF_FRAUD_NETWORK"},
    ],
    "Rule_09_PCA_CheckSplit": [
        {"rule_id": "Rule_08_Recidivist_Escalation", "relationship_type": "MEMBER_OF_FRAUD_NETWORK"},
    ],
}

# Fail at import time, not at first use, if this table references a rule_id
# that does not even exist in the wave lists — the same "fail loudly at
# startup" posture _load_config() already takes for rules.yaml itself.
# This cannot check the .cypher CONTENT (that needs rule_engine.RULE_FILES,
# and rule_engine imports THIS module, so importing it back here would be
# circular) — the .cypher content check lives in tests/verify.py instead,
# in the same style as the existing wave-order checks.
_unknown_dependency_rule_ids = sorted(
    {upstream for upstream in DOWNSTREAM_DEPENDENTS if upstream not in set(ALL_RULE_IDS)}
    | {
        entry["rule_id"]
        for entries in DOWNSTREAM_DEPENDENTS.values()
        for entry in entries
        if entry["rule_id"] not in set(ALL_RULE_IDS)
    }
)
if _unknown_dependency_rule_ids:
    raise ValueError(
        "DOWNSTREAM_DEPENDENTS references rule_id(s) not defined in "
        f"config/rules.yaml: {_unknown_dependency_rule_ids}"
    )


def get_rule_names() -> Dict[str, str]:
    """
    Public {rule_id: human-readable name} accessor, sourced from the
    same YAML-loaded _RULE_NAMES every other rule_registry consumer
    uses. Added for reasoning_layer/rule_audit.py's rule_description
    field (Functional Specification D4) — callers outside this module
    read rule names through here rather than reaching into the private
    _RULE_NAMES dict directly, so config/rules.yaml stays the one place
    a rule's display name is defined.
    """
    return dict(_RULE_NAMES)


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

# Versioned params sync (opt-in via config_version — see rules.yaml).
#
# This is the actual fix for the gap the ON-CREATE-only seed query leaves:
# there was previously no way to ship a genuine bug fix to a rule's default
# params (as opposed to an operator's live retuning) without a manual
# Cypher edit against every environment, by hand, forever. That is not
# something a deploy should depend on.
#
# The guard is params_config_version, not a value comparison against the
# YAML defaults — comparing values would be unable to tell "an operator
# deliberately tuned this away from default" apart from "this predates the
# fix and needs it", and those two cases must be handled oppositely. A
# monotonic version stamp makes that distinction explicit instead of
# guessed at: r.params_config_version IS NULL catches every node seeded
# before this feature existed (all of them, right now); r.params_config_version
# < rule.config_version catches a node already synced to an OLDER fix that
# needs a NEWER one. Either way, once synced, the node is stamped with the
# new version, so the same fix is never silently re-applied and an
# operator's *subsequent* tuning (done after that sync) is not clobbered by
# a re-run of ensure_registry() with the same YAML.
#
# A rule with no config_version in YAML never appears in $versioned_rules
# (see _CONFIG_VERSIONS above) and this query touches nothing for it — the
# ON-CREATE-only behavior for every other rule is completely unchanged.
_SYNC_VERSIONED_PARAMS_QUERY = """
UNWIND $versioned_rules AS rule
MATCH (r:InferenceRule {rule_id: rule.rule_id})
WHERE r.params_config_version IS NULL
   OR r.params_config_version < rule.config_version
WITH r, rule, r.params_config_version AS previous_version
SET r += rule.params,
    r.params_config_version = rule.config_version,
    r.params_synced_at      = datetime(),
    r.params_synced_reason  = "config_version_sync"
RETURN r.rule_id AS rule_id, previous_version AS previous_version,
       rule.config_version AS new_version
"""


def ensure_registry() -> int:
    """Seed any missing :InferenceRule node from the YAML defaults, then
    apply any pending versioned params sync (see _SYNC_VERSIONED_PARAMS_QUERY
    above). Idempotent — safe on every startup and every pipeline run.
    """
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

    if _CONFIG_VERSIONS:
        versioned_rules = [
            {
                "rule_id": rule_id,
                "config_version": version,
                "params": _DEFAULT_PARAMS.get(rule_id, {}),
            }
            for rule_id, version in _CONFIG_VERSIONS.items()
        ]
        with get_session() as session:
            synced = session.run(_SYNC_VERSIONED_PARAMS_QUERY, versioned_rules=versioned_rules).data()
        for row in synced:
            logger.info(
                "rule_registry: synced params for %s (params_config_version %s -> %s)",
                row["rule_id"],
                row["previous_version"],
                row["new_version"],
            )
        if not synced:
            logger.debug(
                "rule_registry: versioned params already up to date for %s", list(_CONFIG_VERSIONS.keys())
            )

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
            rule_id: {"enabled": bool(_RULE_DEFS[rule_id].get("enabled", True)), "params": dict(params)}
            for rule_id, params in _DEFAULT_PARAMS.items()
        }

    registry: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        props = dict(row["props"])
        for meta in (
            "rule_id",
            "rule_name",
            "wave",
            "enabled",
            "created_at",
            "params_config_version",
            "params_synced_at",
            "params_synced_reason",
        ):
            props.pop(meta, None)
        registry[row["rule_id"]] = {
            "enabled": row["enabled"] if row["enabled"] is not None else True,
            "params": props,
        }
    return registry