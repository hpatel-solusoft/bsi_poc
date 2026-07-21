"""
Owns: the command-line entry point for an ingest. Imported by nothing —
this is how a developer, a cron job, or a demo setup script actually runs
the AppWorks -> Neo4j -> rules pipeline before AppWorks is wired up to
fire the lifecycle event that will call POST /graph/ingest.

It calls the SAME etl.ingest_service.ingest() the endpoint calls. There
is deliberately no separate "manual mode" implementation that could drift
from the production path.

Usage:
    # Schema first, once per environment (idempotent, safe to repeat)
    python -m reasoning_layer.apply_schema

    # Load cases and run the full rule pipeline over them
    python -m etl.run_sync CASE-1001 CASE-1002 CASE-1003

    # POC/demo preload: reason for every subject on every case, not just
    # the primary — so any test subject can be opened with a completed run
    # already behind them
    python -m etl.run_sync --subjects all --file demo_cases.txt

    # Load only, no reasoning (useful when staging a large backfill and
    # reasoning separately)
    python -m etl.run_sync --no-reason CASE-1001

    # What is actually in the graph right now?
    python -m etl.run_sync --status
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List

from core import graph_ingest_repository
from core.db import init_pool
from etl.ingest_service import ingest
from reasoning_layer.neo4j_client import init_driver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _case_ids_from_args(args) -> List[str]:
    case_ids = list(args.case_ids)
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"File not found: {path}")
            sys.exit(1)
        case_ids += [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.startswith("#")]
    # De-duplicate while preserving order: ingesting the same case twice in
    # one batch is harmless (every write is a MERGE) but wastes an entire
    # AppWorks fetch, and in a backfill that is not nothing.
    seen, unique = set(), []
    for case_id in case_ids:
        if case_id not in seen:
            seen.add(case_id)
            unique.append(case_id)
    return unique


def _print_status() -> int:
    rows = graph_ingest_repository.list_states()
    if not rows:
        print("No cases ingested yet.")
        return 0
    print(f"{'CASE_ID':<20} {'STATUS':<10} {'ATTEMPTS':<9} LAST_ERROR")
    for row in rows:
        error = (row.get("last_error") or "")[:60]
        print(f"{row['case_id']:<20} {row['status']:<10} {row['attempts']:<9} {error}")
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m etl.run_sync",
        description="Ingest AppWorks cases into Neo4j and run the reasoning rule pipeline.",
    )
    parser.add_argument("case_ids", nargs="*", help="One or more AppWorks case ids")
    parser.add_argument("--file", help="Path to a newline-separated file of case ids")
    parser.add_argument("--no-reason", action="store_true",
                        help="Load into Neo4j only; do not run Wave 1/2 rules")
    parser.add_argument("--subjects", choices=["primary", "all"], default="primary",
                        help="Which subjects to run the pipeline for. 'primary' mirrors what an "
                             "investigator opening the case triggers; 'all' is the POC/demo preload.")
    parser.add_argument("--status", action="store_true",
                        help="Print what is currently in the graph and exit")
    args = parser.parse_args(argv)

    # Both stores are needed: Neo4j to write the graph, Postgres for
    # pipeline_execution_state (Principle 10's idempotency) and
    # graph_ingest_state. Fail loudly at the top rather than half-way
    # through case eleven of eighteen.
    try:
        init_pool()
    except Exception as exc:  # noqa: BLE001
        print(f"PostgreSQL unavailable: {exc}")
        return 1

    if args.status:
        return _print_status()

    try:
        init_driver()
    except Exception as exc:  # noqa: BLE001
        print(f"Neo4j unavailable: {exc}")
        return 1

    case_ids = _case_ids_from_args(args)
    if not case_ids:
        parser.print_help()
        return 1

    report = ingest(
        case_ids,
        run_reasoning=not args.no_reason,
    )

    print()
    print(json.dumps(report, indent=2, default=str))
    print()
    print(f"Loaded {report['cases_loaded']}/{report['cases_requested']} case(s); "
          f"reasoned over {report['pipeline_reasoned']} subject(s); "
          f"{report['pipeline_reasoning_failed']} reasoning failure(s).")

    # Non-zero on any failure so a cron job or CI step notices.
    failed = report["cases_load_failed"] or report["pipeline_reasoning_failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))