"""
Neo4j driver lifecycle for the Reasoning Layer.

Owns: driver init/close and a single session-scoped context manager.
Does not own: Cypher queries, rule logic, or the graph schema — those
belong to reasoning_layer/pipeline.py, reasoning_layer/rules/*.cypher,
and reasoning_layer/schema.cypher respectively.

Mirrors core/db.py's pattern deliberately: same shape (init_driver/
close_driver/get_session, same DatabaseUnavailableError-style contract)
so callers can reason about the two backing stores identically, even
though the transport (Bolt vs. libpq) is completely different.
"""

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

from neo4j import GraphDatabase, Driver
from neo4j.exceptions import ServiceUnavailable, AuthError

logger = logging.getLogger(__name__)

_driver: Optional[Driver] = None


class GraphUnavailableError(RuntimeError):
    """
    Raised when the Neo4j driver cannot be reached. Named distinctly
    from core.db.DatabaseUnavailableError (rather than reusing it)
    because the two backing stores have different failure semantics for
    callers: a Postgres outage degrades case_ai_summary_store/
    conversation_history to "no fallback data" (Section D.6-adjacent
    behavior already shipped); a Neo4j outage means the reasoning
    pipeline cannot run at all for this request — there is no
    fallback graph. Callers must not conflate the two.
    """


def _build_uri() -> str:
    return os.getenv("NEO4J_URI", "bolt://localhost:7687")


def init_driver() -> None:
    """Initialize the module-level Neo4j driver. Safe to call more than once."""
    global _driver
    if _driver is not None:
        return

    uri = _build_uri()
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")

    try:
        # notifications_disabled_classifications=["UNRECOGNIZED"] silences the
        # "property/label/relationship-type does not exist" warnings the server
        # emits when a query filters on a property key that no node has created
        # YET. The rules_fired read-back (reasoning_layer/rules_fired.py) filters
        # on properties that only come into existence once a given rule has fired
        # at least once — e.g. cross_case_source_rule, risk_escalation_status,
        # fasttrack_recommendation_status. On any case where those rules do not
        # fire (the common case), the keys legitimately do not exist and the
        # server warns once per query, spamming the log with non-actionable
        # noise. This is a read-side artifact of a still-sparse graph, not a bug,
        # so it is suppressed at the source.
        #
        # Deliberately NOT suppressing DEPRECATION or any other classification:
        # a real deprecation (like the CALL-subquery one this codebase just
        # fixed at source in etl/graph_sync.py) must stay visible.
        driver = GraphDatabase.driver(
            uri, auth=(user, password),
            notifications_disabled_classifications=["UNRECOGNIZED"],
        )
        driver.verify_connectivity()
        _driver = driver
        logger.info("Neo4j driver initialized (uri=%s)", uri)
    except (ServiceUnavailable, AuthError) as exc:
        logger.error("Failed to initialize Neo4j driver (uri=%s): %s", uri, exc)
        _driver = None
        raise GraphUnavailableError(str(exc)) from exc


def close_driver() -> None:
    """Close the Neo4j driver. Called on application shutdown."""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
        logger.info("Neo4j driver closed")


@contextmanager
def get_session(database: Optional[str] = None) -> Iterator["neo4j.Session"]:
    """
    Yield a Neo4j session from the driver. Sessions are cheap in the
    Neo4j Python driver (unlike a pooled Postgres connection) — the
    driver itself maintains the connection pool underneath, so a new
    session per call is the documented usage pattern, not an
    inefficiency to work around.
    """
    if _driver is None:
        init_driver()
    assert _driver is not None

    db_name = database or os.getenv("NEO4J_DATABASE", "neo4j")
    session = _driver.session(database=db_name)
    try:
        yield session
    finally:
        session.close()
