"""
Owns: the AppWorks -> Neo4j ETL for one case's full entity graph — the
fetch (AppWorks REST -> canonical dict) and the load (canonical dict ->
Neo4j nodes/relationships with Section 3.3 provenance).

Does NOT own: orchestration (etl/ingest_service.py), normalisation
(etl/normalizers.py), rule execution (reasoning_layer/), or any AppWorks
path string (appworks/appworks_paths.py).

Deliberately its own top-level layer rather than folded into appworks/
or reasoning_layer/: it reads AppWorks (Layer 3's domain) and writes
Neo4j (Layer 4's domain), so it has no single owner in the existing
file-split table. It reuses appworks_auth.fetch / appworks_paths.AW /
appworks_utils exactly as case_intake.py does, and touches no protected
file.

WHAT CHANGED FROM THE FIRST ETL ROUND (and why):
  1. Idempotent. Every write is a MERGE on a stable key, including
     :Commentary (previously CREATE — a re-sync duplicated every
     comment, which makes lifecycle-event-triggered re-sync unusable).
  2. Batched. One write transaction per case, ~12 UNWIND statements,
     instead of one Bolt round-trip per address/alias/employer/comment.
  3. Atomic. The whole case loads in a single transaction: a mid-load
     failure leaves NO partial case in the graph, rather than a subject
     with allegations but no commentary that the Extraction Stage would
     then read as "nobody ever commented" — a wrong answer that looks
     like a right one.
  4. Employers are no longer dropped when AppWorks has no FEIN for them
     (see normalizers.employer_key) — that silently starved Rules 1, 9
     and 12 of most of their data.
  5. Wage records (HAS_WAGE_RECORD_WITH) are loaded. Rules 9 and 12
     cannot fire without them and previously had no data source at all.
  6. Allegation-comment and Subject_Comment narrative fields are loaded
     as :Commentary. Section 5.3 Step 3 names all three narrative
     sources; only Case commentary was ever loaded.
  7. Node-level provenance (source_system / source_table / retrieved_at)
     in addition to Section 3.3's relationship-level pair.
  8. Reconciled, not just additive. Every write above is a MERGE, which
     handles create/update but never removal — a record deleted at the
     AppWorks source used to stay in the graph forever. The RECONCILE
     section (below the _Q_* write queries) closes that gap: it uses
     the retrieved_at stamp every write above already sets to detect an
     ETL-owned edge this run's fetch no longer reports, and retires it
     after two consecutive misses (never on the first, since a
     transient AppWorks fetch failure and a genuine deletion look
     identical from here — see that section's own docstring). Scoped
     strictly to the eight relationship types this file writes; never
     touches a rule-inferred relationship, a :Rejection record, or
     :FraudNetwork, and never a full-case wipe.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from appworks.appworks_auth import fetch
from appworks.appworks_paths import AppWorksPaths as AW
from appworks.appworks_utils import (
    embedded,
    extract_id_from_href,
    get_relationship_items,
    safe_fetch,
)
from etl import normalizers as N
from etl.graph_sync_queries import (
    Q_ADDRESSES,
    Q_ALIASES,
    Q_ALLEGATIONS,
    Q_CASE,
    Q_CO_SUBJECTS,
    Q_COMMENTARY,
    Q_EMPLOYERS,
    Q_MERGED_INTO,
    Q_SUBJECTS,
    Q_WAGES,
)
from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "AppWorks"


# ============================================================
# FETCH — AppWorks REST -> canonical Section 3.1 field names
# ============================================================


def _first(props: Dict[str, Any], *names: str) -> Optional[Any]:
    """Property-name fallback chain. AppWorks exposes the same logical
    field under different names on different entities, so every read with
    more than one known spelling goes through here rather than picking
    one and failing silently."""
    for name in names:
        value = props.get(name)
        if value not in (None, ""):
            return value
    return None


def _merge_target_case_ids(raw: Any) -> List[str]:
    """Workfolder_MergeCases is free text with no AppWorks-documented
    delimiter convention (GAP_ANALYSIS.md) — split on every separator
    seen in practice rather than assuming one."""
    if not raw:
        return []
    text = str(raw)
    for sep in (";", "|", "\n"):
        text = text.replace(sep, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _fetch_subject_addresses(subject_id: str) -> List[Dict[str, Any]]:
    addresses: List[Dict[str, Any]] = []
    href = AW.AddressList.by_subject(subject_id)
    for item in get_relationship_items(href, "Address_All"):
        props = item.get("Properties", {})
        scz_props = embedded(item, "Address_StateCityZip_Relation")

        street = N.clean_text(_first(props, "Address_Address", "Address_AddressLine1"))
        city = N.clean_text(scz_props.get("StateCityZip_City") or props.get("Address_City"))
        state = N.clean_text(scz_props.get("StateCityZip_State") or props.get("Address_State"))
        zip_code = N.normalize_zip(
            _first(props, "Address_Zipcode", "Address_Zip") or scz_props.get("StateCityZip_Zip")
        )

        key = N.address_key(street, city, state, zip_code)
        if not key:
            continue
        addresses.append(
            {
                "address_key": key,
                "street": street,
                "city": city,
                "state": state,
                "zip": zip_code,
                "street_normalized": N.normalize_street(street),
            }
        )
    return addresses


def _fetch_subject_aliases(subject_id: str, detail_links: Dict[str, Any]) -> List[str]:
    href = detail_links.get("relationship:Subject_Alias", {}).get("href") or AW.Subject.aliases(subject_id)
    values: List[str] = []
    for item in get_relationship_items(href, "Subject_Alias"):
        props = item.get("Properties", item)
        value = N.alias_value(_first(props, "Alias", "Subject_Alias", "Alias_Name"))
        if value:
            values.append(value)
    return values


def _fetch_subject_employers(subject_id: str) -> List[Dict[str, Any]]:
    """EMPLOYED_BY source — the Job list endpoint filtered by subject."""
    employers: List[Dict[str, Any]] = []
    for item in get_relationship_items(AW.Subject.jobs(subject_id), "AllJobs"):
        props = item.get("Properties", item)
        name = N.clean_text(_first(props, "Job_EmployerName", "Job_Employer"))
        fein = N.normalize_fein(_first(props, "Job_FeinNumber", "Job_Fein"))
        fid = N.clean_text(_first(props, "Job_EmployerId", "Job_EmployerFid"))
        key = N.employer_key(fein, fid, name)
        if not key:
            continue
        employers.append(
            {
                "employer_key": key,
                "employer_name": name,
                "fein": fein,
                "employer_fid": fid,
                "start_date": N.to_iso_date(_first(props, "Job_StartDate", "Job_HireDate")),
                "end_date": N.to_iso_date(_first(props, "Job_EndDate", "Job_TerminationDate")),
            }
        )
    return employers


def _fetch_subject_wages(subject_id: str) -> List[Dict[str, Any]]:
    """
    HAS_WAGE_RECORD_WITH source — the Wage child entity. Section 3.2
    calls this "an independent path, better coverage" than the Job
    table; Rules 9 and 12 both depend on it and previously had no data.

    The Wage table carries employer_name/employer_fid but NO FEIN
    (GAP_ANALYSIS.md's standing ask). That no longer blocks the load:
    employer_key() falls back to the AppWorks employer id, so two
    subjects whose wage rows point at the same employer land on the same
    :Employer node — exactly the join Rule 9 needs. What it still means
    is that a wage-only employer will not unify with a job-sourced
    employer that has a FEIN, even when they are the same company. That
    is a data-quality ceiling, not a code bug, and is why the FEIN ask
    stays open.
    """
    wages: List[Dict[str, Any]] = []
    for item in get_relationship_items(AW.Subject.wages(subject_id), "Subject_SubjectWages"):
        props = item.get("Properties", item)
        name = N.clean_text(_first(props, "SubjectWages_EmployerName", "Wages_EmployerName", "employer_name"))
        fid = N.clean_text(_first(props, "SubjectWages_EmployerFid", "Wages_EmployerFid", "employer_fid"))
        fein = N.normalize_fein(_first(props, "SubjectWages_Fein", "Wages_FeinNumber"))
        key = N.employer_key(fein, fid, name)
        if not key:
            continue
        period_start = N.to_iso_date(
            _first(props, "SubjectWages_PeriodStart", "SubjectWages_StartDate", "Wages_QuarterStart")
        )
        period_end = N.to_iso_date(
            _first(props, "SubjectWages_PeriodEnd", "SubjectWages_EndDate", "Wages_QuarterEnd")
        )
        year = N.clean_text(_first(props, "SubjectWages_Year", "Wages_Year"))
        quarter = N.clean_text(_first(props, "SubjectWages_Quarter", "Wages_Quarter"))
        wages.append(
            {
                "employer_key": key,
                "employer_name": name,
                "fein": fein,
                "employer_fid": fid,
                "period_start": period_start,
                "period_end": period_end,
                "wage_year": year,
                "wage_quarter": quarter,
                "wage_amount": N.to_float(
                    _first(props, "SubjectWages_Amount", "SubjectWages_WageAmount", "Wages_Amount")
                ),
                # Distinguishes two wage rows for the same subject+employer in
                # different periods. Without it, MERGE collapses an entire
                # employment history into one relationship and Rule 12's date
                # overlap check has nothing left to compare against.
                "period_key": f"{year or ''}|{quarter or ''}|{period_start or ''}|{period_end or ''}",
            }
        )
    return wages


def fetch_case_graph(case_id: str) -> Dict[str, Any]:
    """
    Fetch one case's full entity graph, shaped into canonical Section 3.1
    field names — never AppWorks' own property names. Keeping that
    translation in one place means the load side (and anything else that
    ever reads this dict) only has to know the canonical schema.

    Raises whatever appworks_auth.fetch raises on transport/auth failure;
    retry policy belongs to etl/ingest_service.py.
    """
    logger.info("etl.graph_sync: FETCH case_id=%s", case_id)
    retrieved_at = N.now_iso()

    workfolder = fetch(AW.Workfolder.item(case_id))
    wf_props = workfolder.get("Properties", {})
    wf_links = workfolder.get("_links", {})

    case = {
        "case_id": str(wf_props.get("CASEID", case_id)),
        "complaint_number": N.clean_text(wf_props.get("WorkfolderComplaintNumber")),
        "status": N.clean_text(_first(wf_props, "WorkfolderStatus", "Workfolder_Status")),
        "is_fasttrack": N.to_bool(_first(wf_props, "WorkfolderFastTrack", "FAST_TRACK", "FastTrack")),
        "fraud_amount": N.to_float(wf_props.get("WorkfolderFraudAmount")),
        # Rule 12 compares a wage period against the case's fraud date range.
        # No confirmed AppWorks source for these (GAP_ANALYSIS.md); the
        # fallback chain is a best effort and will often resolve to None,
        # which Rule 12 handles explicitly rather than matching everything.
        "fraud_start_date": N.to_iso_date(
            _first(wf_props, "WorkfolderFraudStartDate", "Workfolder_FraudPeriodStart")
        ),
        "fraud_end_date": N.to_iso_date(
            _first(wf_props, "WorkfolderFraudEndDate", "Workfolder_FraudPeriodEnd")
        ),
        # is_dta_case / disposition: still no confirmed source on the
        # Workfolder entity in this codebase or the standalone test app.
        # Read optimistically through a fallback chain, left null when
        # absent — never guessed.
        "is_dta_case": N.to_bool(_first(wf_props, "WorkfolderIsDTACase", "Workfolder_DTACase")),
        "disposition": N.clean_text(_first(wf_props, "WorkfolderDisposition", "Workfolder_Disposition")),
        "opened_date": N.to_iso_date(_first(wf_props, "WorkfolderOpenDate", "S_CREATEDDATE")),
        "closed_date": N.to_iso_date(_first(wf_props, "WorkfolderCloseDate", "Workfolder_ClosedDate")),
        "merge_target_case_ids": _merge_target_case_ids(wf_props.get("Workfolder_MergeCases")),
        "source_table": "Workfolder",
        "retrieved_at": retrieved_at,
    }

    allegations: List[Dict[str, Any]] = []
    commentary: List[Dict[str, Any]] = []

    # --- Allegations (+ the allegation comment narrative field) ---
    alleg_href = wf_links.get("relationship:Workfolder_AllegationsRelationship", {}).get("href")
    if alleg_href:
        for item in get_relationship_items(alleg_href, "Workfolder_AllegationsRelationship"):
            self_href = item.get("_links", {}).get("self", {}).get("href", "")
            props, links = safe_fetch(self_href, "Allegations") if self_href else ({}, {})
            type_href = links.get("relationship:Allegations_AllegationsType", {}).get("href", "")
            type_props, _ = safe_fetch(type_href, "AllegationType") if type_href else ({}, {})

            allegation_id = extract_id_from_href(self_href)
            if not allegation_id:
                logger.warning(
                    "etl.graph_sync: allegation with no resolvable id skipped (case_id=%s)", case_id
                )
                continue

            comment_text = N.clean_text(
                _first(
                    props,
                    "Allegations_Comment",
                    "Allegations_Comments",
                    "Allegations_AllegationComment",
                    "Allegations_Narrative",
                    "Allegations_Description",
                )
            )

            allegations.append(
                {
                    "allegation_id": allegation_id,
                    # Section 3.1: Allegation Type is a controlled-vocabulary STRING,
                    # not a node — the nested AppWorks type object is flattened to
                    # one descriptive string here.
                    "allegation_type": N.clean_text(
                        _first(
                            type_props,
                            "AllegationType_AllegationTypeDescription",
                            "AllegationType_AllegationTypeShortDesc",
                        )
                    ),
                    "status": N.clean_text(props.get("Allegations_AllegationStatus")),
                    "record_status": N.clean_text(props.get("Allegations_Status")),
                    "norris_code": N.clean_text(props.get("Allegations_DispositionNorrisCode")),
                    "outcome": N.clean_text(_first(props, "Allegations_Outcome", "Allegations_Disposition")),
                    # Closure date of the allegation itself. Synced because it
                    # is the ONLY closure date present on older AppWorks cases
                    # (e.g. 658423812), where the workfolder-level
                    # WorkfolderCloseDate is never populated. Rule 7 falls back
                    # to this when :Case.closed_date is null, so that a prior
                    # guilty case can still be DATED rather than only detected
                    # — without it, prior-guilt recency scoring in
                    # reasoning_layer/risk_signals.py has no input at all on
                    # exactly the historical cases it most needs to weigh.
                    "date_closed": N.to_iso_date(
                        _first(
                            props,
                            "Allegations_DateClosed",
                            "Allegations_ClosedDate",
                        )
                    ),
                    "comment_text": comment_text,
                    "source_table": "Allegations",
                    "retrieved_at": retrieved_at,
                    # wage_corroborated / corroborating_employer_fein deliberately
                    # NOT set: Section 3.1 lists them on :Allegation, but they are
                    # Rule 12's write targets. A rule concludes corroboration; ETL
                    # does not fetch it.
                }
            )

            if comment_text:
                commentary.append(
                    {
                        "comment_id": N.commentary_id(
                            case_id, "Allegation_Comment", allegation_id, comment_text, None
                        ),
                        "comment_text": comment_text,
                        "comment_type": "Allegation_Comment",
                        "created_date": N.to_iso_date(props.get("S_CREATEDDATE")),
                        "attach_to": "allegation",
                        "attach_id": allegation_id,
                        "source_table": "Allegations",
                    }
                )

    # --- Case commentary ---
    comm_href = wf_links.get("relationship:Workfolder_WorkfolderCommentaryNewRelationship", {}).get("href")
    if comm_href:
        for item in get_relationship_items(comm_href, "Workfolder_WorkfolderCommentaryNewRelationship"):
            self_href = item.get("_links", {}).get("self", {}).get("href", "")
            props, links = (
                safe_fetch(self_href, "WorkfolderCommentary")
                if self_href
                else (item.get("Properties", {}), {})
            )
            type_href = links.get("relationship:WorkfolderCommentary_CommentaryTypeRelationship", {}).get(
                "href", ""
            )
            type_props, _ = safe_fetch(type_href, "CommentaryType") if type_href else ({}, {})

            text = N.clean_text(_first(props, "WorkfolderCommentary_Comment", "Commentary_Comment"))
            if not text:
                continue
            created = N.to_iso_date(props.get("S_CREATEDDATE"))
            commentary.append(
                {
                    "comment_id": N.commentary_id(
                        case_id,
                        "Case_Commentary",
                        extract_id_from_href(self_href),
                        text,
                        created,
                    ),
                    "comment_text": text,
                    "comment_type": N.clean_text(type_props.get("Type")) or "Case_Commentary",
                    "created_date": created,
                    "attach_to": "case",
                    "attach_id": case["case_id"],
                    "source_table": "WorkfolderCommentary",
                }
            )

    # --- Subjects (+ address / alias / employer / wage / Subject_Comment) ---
    subjects: List[Dict[str, Any]] = []
    subj_href = wf_links.get("relationship:Workfolder_SubjectsRelationship", {}).get("href")
    if subj_href:
        for item in get_relationship_items(subj_href, "Workfolder_SubjectsRelationship"):
            self_href = item.get("_links", {}).get("self", {}).get("href", "")
            subj_props, subj_links = safe_fetch(self_href, "Subjects") if self_href else ({}, {})

            detail_href = subj_links.get("relationship:Subjects_Subject", {}).get("href", "")
            subject_id = extract_id_from_href(detail_href)
            if not subject_id:
                logger.warning(
                    "etl.graph_sync: subject with no resolvable subject_id skipped (case_id=%s)", case_id
                )
                continue
            detail_props, detail_links = safe_fetch(detail_href, "Subject")

            role_href = subj_links.get("relationship:Subjects_SubjectRoleRelationship", {}).get("href", "")
            role_props, _ = safe_fetch(role_href, "SubjectRole") if role_href else ({}, {})

            is_company = bool(N.clean_text(detail_props.get("Subject_CompanyName")))
            subject_comment = N.clean_text(
                _first(
                    detail_props,
                    "Subject_Comment",
                    "Subject_Comments",
                    "Subject_Notes",
                )
            )

            subjects.append(
                {
                    "subject_id": subject_id,
                    "first_name": N.clean_text(detail_props.get("Subject_FirstName")),
                    "last_name": N.clean_text(detail_props.get("Subject_LastName")),
                    "company_name": (
                        N.clean_text(detail_props.get("Subject_CompanyName")) if is_company else None
                    ),
                    # Subject_EIN is AppWorks' name for what Section 3.1 calls a
                    # company Subject's `fein` — same concept, different label.
                    "fein": N.normalize_fein(detail_props.get("Subject_EIN")) if is_company else None,
                    "subject_type": "Company" if is_company else "Individual",
                    # subject_role is case-specific (Section 3.2 makes it a property
                    # ON APPEARS_IN_CASE, not a permanent trait) — carried on this
                    # dict only to be written onto the relationship, never the node.
                    "subject_role": N.clean_text(
                        role_props.get("RoleName") or subj_props.get("Subjects_SubjectType")
                    ),
                    "is_primary": N.to_bool(subj_props.get("Subjects_IsPrimarySubject")),
                    "addresses": _fetch_subject_addresses(subject_id),
                    "aliases": _fetch_subject_aliases(subject_id, detail_links),
                    "employers": _fetch_subject_employers(subject_id),
                    "wages": _fetch_subject_wages(subject_id),
                    "source_table": "Subject",
                    "retrieved_at": retrieved_at,
                    # ssn is Tier 1 PII (Section 3.5) — never fetched, never stored.
                }
            )

            if subject_comment:
                commentary.append(
                    {
                        "comment_id": N.commentary_id(
                            case_id, "Subject_Comment", subject_id, subject_comment, None
                        ),
                        "comment_text": subject_comment,
                        "comment_type": "Subject_Comment",
                        "created_date": None,
                        "attach_to": "subject",
                        "attach_id": subject_id,
                        "source_table": "Subject",
                    }
                )

    logger.info(
        "etl.graph_sync: FETCHED case_id=%s subjects=%d allegations=%d commentary=%d employers=%d wages=%d",
        case_id,
        len(subjects),
        len(allegations),
        len(commentary),
        sum(len(s["employers"]) for s in subjects),
        sum(len(s["wages"]) for s in subjects),
    )
    return {
        "case": case,
        "subjects": subjects,
        "allegations": allegations,
        "commentary": commentary,
        "retrieved_at": retrieved_at,
    }


# Aliased under their original "_Q_*" names (rather than only the public
# Q_* names imported above) because tests/test_ai04_graph_sync.py and
# tests/test_graph_sync_reconcile.py both discover every write query by
# introspecting vars(graph_sync) for a "_Q_" prefix — deliberately, so a
# NEW query is automatically covered by both suites' audits without
# editing the test. Moving the query text itself to
# etl/graph_sync_queries.py must not silently drop that coverage, so the
# alias stays. NOTE FOR THE NEXT QUERY ADDED: add it to
# etl/graph_sync_queries.py, import it above, AND alias it here — the
# "automatic" part of the audit only covers names introspectable on
# THIS module.
_Q_CASE = Q_CASE
_Q_MERGED_INTO = Q_MERGED_INTO
_Q_ALLEGATIONS = Q_ALLEGATIONS
_Q_SUBJECTS = Q_SUBJECTS
_Q_ADDRESSES = Q_ADDRESSES
_Q_ALIASES = Q_ALIASES
_Q_EMPLOYERS = Q_EMPLOYERS
_Q_WAGES = Q_WAGES
_Q_COMMENTARY = Q_COMMENTARY
_Q_CO_SUBJECTS = Q_CO_SUBJECTS


# ============================================================
# RECONCILE — retire ETL-owned edges AppWorks no longer reports
#
# Every _Q_* query above is additive only: it is a MERGE, so a
# HAS_ADDRESS / HAS_ALIAS / EMPLOYED_BY / HAS_WAGE_RECORD_WITH /
# HAS_ALLEGATION / HAS_COMMENTARY / APPEARS_IN_CASE / IS_CO_SUBJECT_WITH
# edge that existed from a prior sync and simply is not present in
# THIS run's fetch is left completely untouched above. Left alone
# forever, that is the gap this section closes: an investigator removes
# a stale address, alias, allegation, employer record, wage record, or
# comment in AppWorks, and — before this section existed — the graph
# went on reporting it as current indefinitely, because nothing ever
# looked at what used to be there and is now gone.
#
# NOT "clear the case subgraph and reload". That would be both wrong
# and dangerous here: the rule-inferred relationships
# (SHARES_EMPLOYER_WITH, SHARES_ADDRESS_WITH, SHARES_ALIAS_PATTERN_WITH,
# MEMBER_OF_FRAUD_NETWORK, HAS_PRIOR_GUILTY_CASE,
# ALLEGATION_LIKELY_AGAINST_SUBJECT — see reasoning_layer/rules/*.cypher)
# and every :Rejection record an investigator has ever filed live in the
# SAME graph as this case's source data, and Principle 14 makes a
# rejection a PERMANENT record, never a silent deletion. A full-case
# wipe-and-reingest would take all of that out with it on every single
# sync. So this section is scoped, by relationship TYPE, to exactly the
# eight relationship types the _Q_* queries above write — it never
# references a rule-created type or :Rejection or :FraudNetwork — and it
# only ever deletes the specific stale EDGE, never a shared node.
# :Address / :Alias / :Employer can legitimately be referenced by other
# subjects or other cases and are never deleted here, only unlinked from
# the one subject whose record went away. Only owned, single-parent
# child records — :Allegation, :Commentary — are ever removed as nodes,
# and only once orphaned (see the GC queries and _tx_prune_stale below).
#
# TWO-SYNC CONFIRMATION, NOT DELETE-ON-FIRST-MISS:
# appworks_utils.safe_fetch / get_relationship_items both swallow a
# transient AppWorks failure (a timed-out call, an expired token
# mid multi-call fetch) into an EMPTY result — logged, not raised, by
# design, so one bad sub-fetch does not fail the whole case (see that
# module's own docstring). That means "this row is missing from today's
# fetch" is not reliably "AppWorks deleted this row"; it can just as
# easily be "AppWorks hiccupped while this run happened to fetch it."
# Deleting on the first miss would let one transient failure silently
# erase real investigative data — a worse failure mode than "a genuine
# deletion takes one extra sync to fully catch up." So a relationship
# missing from this run's fetch is first FLAGGED (r._prune_pending =
# true, r._prune_flagged_at = $retrieved_at) rather than deleted, and is
# only actually deleted once it is confirmed missing AGAIN on a later
# run while still flagged. Any relationship the next successful MERGE
# touches again clears the flag itself (every _Q_* query's SET clause
# above now sets r._prune_pending = null, r._prune_flagged_at = null) —
# a transient failure heals itself on the very next successful sync,
# before it could ever reach the second miss that would delete anything.
# ============================================================

_RECONCILE_APPEARS_IN_CASE = """
MATCH (s:Subject)-[r:APPEARS_IN_CASE]->(c:Case {case_id: $case_id})
WHERE r.retrieved_at <> $retrieved_at
WITH r, coalesce(r._prune_pending, false) AS already_flagged
FOREACH (_ IN CASE WHEN already_flagged THEN [1] ELSE [] END | DELETE r)
FOREACH (_ IN CASE WHEN NOT already_flagged THEN [1] ELSE [] END |
    SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at)
RETURN count(CASE WHEN already_flagged THEN 1 END) AS deleted,
       count(CASE WHEN NOT already_flagged THEN 1 END) AS flagged
"""

_RECONCILE_ALLEGATIONS = """
MATCH (c:Case {case_id: $case_id})-[r:HAS_ALLEGATION]->(al:Allegation)
WHERE r.retrieved_at <> $retrieved_at
WITH r, al, coalesce(r._prune_pending, false) AS already_flagged
FOREACH (_ IN CASE WHEN already_flagged THEN [1] ELSE [] END | DELETE r)
FOREACH (_ IN CASE WHEN NOT already_flagged THEN [1] ELSE [] END |
    SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at)
RETURN count(CASE WHEN already_flagged THEN 1 END) AS deleted,
       count(CASE WHEN NOT already_flagged THEN 1 END) AS flagged,
       collect(CASE WHEN already_flagged THEN al.allegation_id END) AS retired_candidate_ids
"""

# Address/Alias/Employer nodes are shared reference data (Section 3.1's
# match-key nodes), never owned by one subject or one case — only the
# edge is scoped to $subject_ids (this run's fetched subjects; a
# subject who fell off the case entirely this run was never re-fetched
# at all, so their edges are correctly left untouched rather than
# guessed at) and only the edge is ever deleted, never the node.
_RECONCILE_ADDRESSES = """
MATCH (s:Subject)-[r:HAS_ADDRESS]->(addr:Address)
WHERE s.subject_id IN $subject_ids AND r.retrieved_at <> $retrieved_at
WITH r, coalesce(r._prune_pending, false) AS already_flagged
FOREACH (_ IN CASE WHEN already_flagged THEN [1] ELSE [] END | DELETE r)
FOREACH (_ IN CASE WHEN NOT already_flagged THEN [1] ELSE [] END |
    SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at)
RETURN count(CASE WHEN already_flagged THEN 1 END) AS deleted,
       count(CASE WHEN NOT already_flagged THEN 1 END) AS flagged
"""

_RECONCILE_ALIASES = """
MATCH (s:Subject)-[r:HAS_ALIAS]->(al:Alias)
WHERE s.subject_id IN $subject_ids AND r.retrieved_at <> $retrieved_at
WITH r, coalesce(r._prune_pending, false) AS already_flagged
FOREACH (_ IN CASE WHEN already_flagged THEN [1] ELSE [] END | DELETE r)
FOREACH (_ IN CASE WHEN NOT already_flagged THEN [1] ELSE [] END |
    SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at)
RETURN count(CASE WHEN already_flagged THEN 1 END) AS deleted,
       count(CASE WHEN NOT already_flagged THEN 1 END) AS flagged
"""

_RECONCILE_EMPLOYERS = """
MATCH (s:Subject)-[r:EMPLOYED_BY]->(e:Employer)
WHERE s.subject_id IN $subject_ids AND r.retrieved_at <> $retrieved_at
WITH r, coalesce(r._prune_pending, false) AS already_flagged
FOREACH (_ IN CASE WHEN already_flagged THEN [1] ELSE [] END | DELETE r)
FOREACH (_ IN CASE WHEN NOT already_flagged THEN [1] ELSE [] END |
    SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at)
RETURN count(CASE WHEN already_flagged THEN 1 END) AS deleted,
       count(CASE WHEN NOT already_flagged THEN 1 END) AS flagged
"""

_RECONCILE_WAGES = """
MATCH (s:Subject)-[r:HAS_WAGE_RECORD_WITH]->(e:Employer)
WHERE s.subject_id IN $subject_ids AND r.retrieved_at <> $retrieved_at
WITH r, coalesce(r._prune_pending, false) AS already_flagged
FOREACH (_ IN CASE WHEN already_flagged THEN [1] ELSE [] END | DELETE r)
FOREACH (_ IN CASE WHEN NOT already_flagged THEN [1] ELSE [] END |
    SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at)
RETURN count(CASE WHEN already_flagged THEN 1 END) AS deleted,
       count(CASE WHEN NOT already_flagged THEN 1 END) AS flagged
"""

# Commentary can hang off Case, Subject, or Allegation (Section 5.3 Step
# 3's three narrative sources). Scoped to this case's own Case node,
# this run's fetched subjects, and this run's fetched allegations —
# an allegation already gone this run (see _RECONCILE_ALLEGATIONS above)
# is not in $allegation_ids, so its own commentary edges are correctly
# left for a later run to reconcile once that allegation's HAS_ALLEGATION
# edge has actually been confirmed-deleted, not guessed at here.
_RECONCILE_COMMENTARY = """
MATCH (parent)-[r:HAS_COMMENTARY]->(comm:Commentary)
WHERE r.retrieved_at <> $retrieved_at
  AND (
    (parent:Case AND parent.case_id = $case_id)
    OR (parent:Subject AND parent.subject_id IN $subject_ids)
    OR (parent:Allegation AND parent.allegation_id IN $allegation_ids)
  )
WITH r, comm, coalesce(r._prune_pending, false) AS already_flagged
FOREACH (_ IN CASE WHEN already_flagged THEN [1] ELSE [] END | DELETE r)
FOREACH (_ IN CASE WHEN NOT already_flagged THEN [1] ELSE [] END |
    SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at)
RETURN count(CASE WHEN already_flagged THEN 1 END) AS deleted,
       count(CASE WHEN NOT already_flagged THEN 1 END) AS flagged,
       collect(CASE WHEN already_flagged THEN comm.comment_id END) AS retired_candidate_ids
"""

# id(), not elementId(): the FOREACH-conditional idiom above this
# section was chosen specifically to stay valid on both 4.x and 5.x
# while the target Neo4j version is still settling (see Q_COMMENTARY's
# own comment); elementId() does not exist before 5.0, so id() is used
# here for the same cross-version reason, deprecated-but-functional on
# 5.x rather than unavailable on 4.x. Needed because IS_CO_SUBJECT_WITH
# is stored undirected (MERGE (a)-[r]-(b) with no arrow, see
# Q_CO_SUBJECTS) — an undirected MATCH on the same pattern would
# otherwise visit the one stored relationship twice, once from each
# endpoint, and attempt to DELETE it twice in the same query.
_RECONCILE_CO_SUBJECTS = """
MATCH (a:Subject)-[r:IS_CO_SUBJECT_WITH]-(b:Subject)
WHERE r.case_id = $case_id AND r.retrieved_at <> $retrieved_at AND id(a) < id(b)
WITH r, coalesce(r._prune_pending, false) AS already_flagged
FOREACH (_ IN CASE WHEN already_flagged THEN [1] ELSE [] END | DELETE r)
FOREACH (_ IN CASE WHEN NOT already_flagged THEN [1] ELSE [] END |
    SET r._prune_pending = true, r._prune_flagged_at = $retrieved_at)
RETURN count(CASE WHEN already_flagged THEN 1 END) AS deleted,
       count(CASE WHEN NOT already_flagged THEN 1 END) AS flagged
"""

# Orphan cleanup for owned, single-parent child node types only — never
# for :Address / :Alias / :Employer (see the section docstring above).
# Takes the specific candidate ids the confirmed-delete queries above
# just collected, rather than scanning either label globally, and
# re-checks degree at cleanup time rather than trusting the earlier
# collect(): if some OTHER still-live edge reaches this exact node
# (e.g. a :Commentary shared narrative source, or an :Allegation whose
# HAS_COMMENTARY edge has not yet been confirmed-deleted this same
# pass), it is left alone rather than detached from data it is still
# actually attached to.
_RECONCILE_GC_ALLEGATIONS = """
UNWIND $ids AS aid
MATCH (al:Allegation {allegation_id: aid})
WHERE NOT (al)--()
DETACH DELETE al
RETURN count(al) AS n
"""

_RECONCILE_GC_COMMENTARY = """
UNWIND $ids AS cid
MATCH (comm:Commentary {comment_id: cid})
WHERE NOT (comm)--()
DETACH DELETE comm
RETURN count(comm) AS n
"""


def _tx_prune_stale(
    tx,
    case_id: str,
    subject_ids: List[str],
    allegation_ids: List[str],
    retrieved_at: str,
) -> Dict[str, Any]:
    """
    Second half of the same write transaction _tx_load runs: retire the
    ETL-owned edges this run's fetch no longer reports. See the
    RECONCILE section docstring above for what this does and does not
    touch, and why a miss is flagged before it is ever deleted.

    Deliberately degenerate-safe on empty input: if $subject_ids or
    $allegation_ids come back empty (e.g. a transient failure at the
    top of fetch_case_graph rather than in one sub-fetch), every
    `IN $subject_ids` / `IN $allegation_ids` predicate below matches
    nothing, so this call flags and deletes nothing that run, rather
    than a subject-scoped or allegation-scoped WHERE clause silently
    turning into "matches everything".
    """
    common = {"case_id": case_id, "subject_ids": subject_ids, "retrieved_at": retrieved_at}
    result: Dict[str, Any] = {}

    def run_prune(query: str, key: str, **params):
        record = tx.run(query, **common, **params).single()
        result[key] = {
            "deleted": int(record["deleted"]) if record and record["deleted"] is not None else 0,
            "flagged": int(record["flagged"]) if record and record["flagged"] is not None else 0,
        }
        return record

    run_prune(_RECONCILE_APPEARS_IN_CASE, "appears_in_case")
    alleg_record = run_prune(_RECONCILE_ALLEGATIONS, "allegations")
    run_prune(_RECONCILE_ADDRESSES, "addresses")
    run_prune(_RECONCILE_ALIASES, "aliases")
    run_prune(_RECONCILE_EMPLOYERS, "employers")
    run_prune(_RECONCILE_WAGES, "wage_records")
    comm_record = run_prune(_RECONCILE_COMMENTARY, "commentary", allegation_ids=allegation_ids)
    run_prune(_RECONCILE_CO_SUBJECTS, "co_subject_pairs")

    retired_allegation_ids = list((alleg_record["retired_candidate_ids"] if alleg_record else []) or [])
    retired_comment_ids = list((comm_record["retired_candidate_ids"] if comm_record else []) or [])

    gc_allegations = 0
    if retired_allegation_ids:
        rec = tx.run(_RECONCILE_GC_ALLEGATIONS, ids=retired_allegation_ids).single()
        gc_allegations = int(rec["n"]) if rec and rec["n"] is not None else 0

    gc_commentary = 0
    if retired_comment_ids:
        rec = tx.run(_RECONCILE_GC_COMMENTARY, ids=retired_comment_ids).single()
        gc_commentary = int(rec["n"]) if rec and rec["n"] is not None else 0

    result["allegations_removed"] = gc_allegations
    result["commentary_removed"] = gc_commentary
    return result


def _flatten(subjects: List[Dict[str, Any]], child_key: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for subject in subjects:
        for child in subject.get(child_key, []):
            if isinstance(child, dict):
                rows.append({**child, "subject_id": subject["subject_id"]})
            else:  # aliases arrive as plain strings
                rows.append({"alias_value": child, "subject_id": subject["subject_id"]})
    return rows


def _tx_load(tx, data: Dict[str, Any]) -> Dict[str, Any]:
    case = data["case"]
    case_id = case["case_id"]
    retrieved_at = data["retrieved_at"]
    common = {"source_system": SOURCE_SYSTEM, "retrieved_at": retrieved_at, "case_id": case_id}
    # int for every entity-count key; "pruned" (added at the end,
    # below) is itself a Dict[str, Any] of per-relationship-type
    # deleted/flagged sub-counts, so the value type here is Any, not
    # int.
    counts: Dict[str, Any] = {}

    def run(query: str, key: str, **params) -> None:
        """Execute one Cypher write and record its affected-node count under key."""
        record = tx.run(query, **common, **params).single()
        counts[key] = int(record["n"]) if record and record["n"] is not None else 0

    run(Q_CASE, "cases", case=case)

    targets = case.get("merge_target_case_ids") or []
    if targets:
        run(Q_MERGED_INTO, "merged_into_case", target_case_ids=targets)
    else:
        counts["merged_into_case"] = 0

    subjects = data.get("subjects", [])
    allegations = data.get("allegations", [])
    commentary = data.get("commentary", [])

    if allegations:
        run(Q_ALLEGATIONS, "allegations", allegations=allegations)
    else:
        counts["allegations"] = 0

    if subjects:
        run(Q_SUBJECTS, "subjects", subjects=subjects)
    else:
        counts["subjects"] = 0

    for count_key, query, child_key in (
        ("addresses", Q_ADDRESSES, "addresses"),
        ("aliases", Q_ALIASES, "aliases"),
        ("employers", Q_EMPLOYERS, "employers"),
        ("wage_records", Q_WAGES, "wages"),
    ):
        rows = _flatten(subjects, child_key)
        if rows:
            run(query, count_key, rows=rows)
        else:
            counts[count_key] = 0

    if commentary:
        run(Q_COMMENTARY, "commentary", rows=commentary)
    else:
        counts["commentary"] = 0

    # IS_CO_SUBJECT_WITH — asserted, pairwise across every subject on this
    # case. Section 3.2 says "derived from the structured Subject Role field,
    # not extraction" without spelling out the rule; every pair on one
    # Workfolder is treated as co-subjects, the standard investigative sense
    # of the term. Flagged in GAP_ANALYSIS.md — Rules 9 and 11 both consume it.
    ids = [s["subject_id"] for s in subjects]
    pairs = [{"a": ids[i], "b": ids[j]} for i in range(len(ids)) for j in range(i + 1, len(ids))]
    if pairs:
        run(Q_CO_SUBJECTS, "co_subject_pairs", pairs=pairs)
    else:
        counts["co_subject_pairs"] = 0

    # RECONCILE — same write transaction, so a mid-prune failure leaves
    # the case exactly as it was before this sync started, same
    # atomicity guarantee load_case_graph's docstring makes for the
    # writes above. Runs last and reads $subject_ids / $allegation_ids
    # from what THIS run's fetch actually returned (ids, not the raw
    # objects — the RECONCILE section's own docstring covers why every
    # predicate below is scoped to these rather than a full-case sweep).
    counts["pruned"] = _tx_prune_stale(tx, case_id, ids, [a["allegation_id"] for a in allegations], retrieved_at)

    return counts


def load_case_graph(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Write fetch_case_graph()'s output into Neo4j in ONE write transaction.
    Atomic by design: a case is either fully in the graph or not in it at
    all. A half-loaded case is worse than no case — the Extraction Stage
    would read it as complete and produce confidently wrong output.
    """
    case_id = data["case"]["case_id"]
    with get_session() as session:
        counts = session.execute_write(_tx_load, data)
    pruned = counts.get("pruned") or {}
    deleted_total = sum(v.get("deleted", 0) for v in pruned.values() if isinstance(v, dict))
    flagged_total = sum(v.get("flagged", 0) for v in pruned.values() if isinstance(v, dict))
    logger.info("etl.graph_sync: LOADED case_id=%s %s", case_id, counts)
    if deleted_total or flagged_total:
        logger.info(
            "etl.graph_sync: RECONCILE case_id=%s retired=%d newly_flagged=%d "
            "allegations_removed=%d commentary_removed=%d — see counts.pruned for the per-type breakdown",
            case_id,
            deleted_total,
            flagged_total,
            pruned.get("allegations_removed", 0),
            pruned.get("commentary_removed", 0),
        )
    return counts


def sync_case(case_id: str) -> Dict[str, Any]:
    """Fetch then load, for one case. Retry policy belongs to the caller
    (etl/ingest_service.py), not here."""
    return load_case_graph(fetch_case_graph(case_id))
