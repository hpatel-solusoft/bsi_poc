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
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from appworks.appworks_auth import fetch
from appworks.appworks_paths import AppWorksPaths as AW
from appworks.appworks_utils import embedded, extract_id_from_href, get_relationship_items, safe_fetch
from etl import normalizers as N
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
        addresses.append({
            "address_key": key,
            "street": street, "city": city, "state": state, "zip": zip_code,
            "street_normalized": N.normalize_street(street),
        })
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
        employers.append({
            "employer_key": key, "employer_name": name,
            "fein": fein, "employer_fid": fid,
            "start_date": N.to_iso_date(_first(props, "Job_StartDate", "Job_HireDate")),
            "end_date": N.to_iso_date(_first(props, "Job_EndDate", "Job_TerminationDate")),
        })
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
        period_start = N.to_iso_date(_first(props, "SubjectWages_PeriodStart", "SubjectWages_StartDate", "Wages_QuarterStart"))
        period_end = N.to_iso_date(_first(props, "SubjectWages_PeriodEnd", "SubjectWages_EndDate", "Wages_QuarterEnd"))
        year = N.clean_text(_first(props, "SubjectWages_Year", "Wages_Year"))
        quarter = N.clean_text(_first(props, "SubjectWages_Quarter", "Wages_Quarter"))
        wages.append({
            "employer_key": key, "employer_name": name, "fein": fein, "employer_fid": fid,
            "period_start": period_start, "period_end": period_end,
            "wage_year": year, "wage_quarter": quarter,
            "wage_amount": N.to_float(_first(props, "SubjectWages_Amount", "SubjectWages_WageAmount", "Wages_Amount")),
            # Distinguishes two wage rows for the same subject+employer in
            # different periods. Without it, MERGE collapses an entire
            # employment history into one relationship and Rule 12's date
            # overlap check has nothing left to compare against.
            "period_key": f"{year or ''}|{quarter or ''}|{period_start or ''}|{period_end or ''}",
        })
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
        "fraud_start_date": N.to_iso_date(_first(wf_props, "WorkfolderFraudStartDate", "Workfolder_FraudPeriodStart")),
        "fraud_end_date": N.to_iso_date(_first(wf_props, "WorkfolderFraudEndDate", "Workfolder_FraudPeriodEnd")),
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
                logger.warning("etl.graph_sync: allegation with no resolvable id skipped (case_id=%s)", case_id)
                continue

            comment_text = N.clean_text(_first(
                props, "Allegations_Comment", "Allegations_Comments",
                "Allegations_AllegationComment", "Allegations_Narrative", "Allegations_Description",
            ))

            allegations.append({
                "allegation_id": allegation_id,
                # Section 3.1: Allegation Type is a controlled-vocabulary STRING,
                # not a node — the nested AppWorks type object is flattened to
                # one descriptive string here.
                "allegation_type": N.clean_text(_first(
                    type_props,
                    "AllegationType_AllegationTypeDescription",
                    "AllegationType_AllegationTypeShortDesc",
                )),
                "status": N.clean_text(props.get("Allegations_AllegationStatus")),
                "record_status": N.clean_text(props.get("Allegations_Status")),
                "norris_code": N.clean_text(props.get("Allegations_DispositionNorrisCode")),
                "outcome": N.clean_text(_first(props, "Allegations_Outcome", "Allegations_Disposition")),
                "comment_text": comment_text,
                "source_table": "Allegations",
                "retrieved_at": retrieved_at,
                # wage_corroborated / corroborating_employer_fein deliberately
                # NOT set: Section 3.1 lists them on :Allegation, but they are
                # Rule 12's write targets. A rule concludes corroboration; ETL
                # does not fetch it.
            })

            if comment_text:
                commentary.append({
                    "comment_id": N.commentary_id(case_id, "Allegation_Comment", allegation_id, comment_text, None),
                    "comment_text": comment_text,
                    "comment_type": "Allegation_Comment",
                    "created_date": N.to_iso_date(props.get("S_CREATEDDATE")),
                    "attach_to": "allegation",
                    "attach_id": allegation_id,
                    "source_table": "Allegations",
                })

    # --- Case commentary ---
    comm_href = wf_links.get("relationship:Workfolder_WorkfolderCommentaryNewRelationship", {}).get("href")
    if comm_href:
        for item in get_relationship_items(comm_href, "Workfolder_WorkfolderCommentaryNewRelationship"):
            self_href = item.get("_links", {}).get("self", {}).get("href", "")
            props, links = (
                safe_fetch(self_href, "WorkfolderCommentary") if self_href
                else (item.get("Properties", {}), {})
            )
            type_href = links.get("relationship:WorkfolderCommentary_CommentaryTypeRelationship", {}).get("href", "")
            type_props, _ = safe_fetch(type_href, "CommentaryType") if type_href else ({}, {})

            text = N.clean_text(_first(props, "WorkfolderCommentary_Comment", "Commentary_Comment"))
            if not text:
                continue
            created = N.to_iso_date(props.get("S_CREATEDDATE"))
            commentary.append({
                "comment_id": N.commentary_id(
                    case_id, "Case_Commentary", extract_id_from_href(self_href), text, created,
                ),
                "comment_text": text,
                "comment_type": N.clean_text(type_props.get("Type")) or "Case_Commentary",
                "created_date": created,
                "attach_to": "case",
                "attach_id": case["case_id"],
                "source_table": "WorkfolderCommentary",
            })

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
                logger.warning("etl.graph_sync: subject with no resolvable subject_id skipped (case_id=%s)", case_id)
                continue
            detail_props, detail_links = safe_fetch(detail_href, "Subject")

            role_href = subj_links.get("relationship:Subjects_SubjectRoleRelationship", {}).get("href", "")
            role_props, _ = safe_fetch(role_href, "SubjectRole") if role_href else ({}, {})

            is_company = bool(N.clean_text(detail_props.get("Subject_CompanyName")))
            subject_comment = N.clean_text(_first(
                detail_props, "Subject_Comment", "Subject_Comments", "Subject_Notes",
            ))

            subjects.append({
                "subject_id": subject_id,
                "first_name": N.clean_text(detail_props.get("Subject_FirstName")),
                "last_name": N.clean_text(detail_props.get("Subject_LastName")),
                "company_name": N.clean_text(detail_props.get("Subject_CompanyName")) if is_company else None,
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
            })

            if subject_comment:
                commentary.append({
                    "comment_id": N.commentary_id(case_id, "Subject_Comment", subject_id, subject_comment, None),
                    "comment_text": subject_comment,
                    "comment_type": "Subject_Comment",
                    "created_date": None,
                    "attach_to": "subject",
                    "attach_id": subject_id,
                    "source_table": "Subject",
                })

    logger.info(
        "etl.graph_sync: FETCHED case_id=%s subjects=%d allegations=%d commentary=%d employers=%d wages=%d",
        case_id, len(subjects), len(allegations), len(commentary),
        sum(len(s["employers"]) for s in subjects),
        sum(len(s["wages"]) for s in subjects),
    )
    return {"case": case, "subjects": subjects, "allegations": allegations,
            "commentary": commentary, "retrieved_at": retrieved_at}


# ============================================================
# LOAD — canonical dict -> Neo4j
# Section 3.1 node labels, 3.2 relationship types, 3.3 provenance.
# Every asserted relationship carries source_table + retrieved_at.
# Nodes additionally carry source_system, so an investigator can tell an
# ETL-sourced node from a rule-created one (:FraudNetwork) without
# inspecting its edges.
# ============================================================

_Q_CASE = """
MERGE (c:Case {case_id: $case.case_id})
SET c.complaint_number  = $case.complaint_number,
    c.status            = $case.status,
    c.fraud_amount      = $case.fraud_amount,
    c.is_fasttrack      = $case.is_fasttrack,
    c.is_dta_case       = $case.is_dta_case,
    c.disposition       = $case.disposition,
    c.fraud_start_date  = $case.fraud_start_date,
    c.fraud_end_date    = $case.fraud_end_date,
    c.opened_date       = $case.opened_date,
    c.closed_date       = $case.closed_date,
    c.source_system     = $source_system,
    c.source_table      = $case.source_table,
    c.retrieved_at      = $case.retrieved_at,
    c.stub              = false
RETURN 1 AS n
"""

_Q_MERGED_INTO = """
MATCH (c1:Case {case_id: $case_id})
UNWIND $target_case_ids AS target_id
MERGE (c2:Case {case_id: target_id})
  ON CREATE SET c2.source_system = $source_system,
                c2.source_table  = "Workfolder_MergeCases",
                c2.retrieved_at  = $retrieved_at,
                c2.stub          = true
MERGE (c1)-[r:MERGED_INTO_CASE]->(c2)
SET r.source_table = "Workfolder_MergeCases", r.retrieved_at = $retrieved_at
RETURN count(r) AS n
"""
# stub=true marks a Case node created only because another case merged
# into it, before that case's own ETL run has happened. Rule 10 reads it
# either way; the flag exists so nobody mistakes an empty Case node for a
# case AppWorks has no data on.

_Q_ALLEGATIONS = """
MATCH (c:Case {case_id: $case_id})
UNWIND $allegations AS a
MERGE (al:Allegation {allegation_id: a.allegation_id})
SET al.allegation_type = a.allegation_type,
    al.status          = a.status,
    al.record_status   = a.record_status,
    al.norris_code     = a.norris_code,
    al.outcome         = a.outcome,
    al.comment_text    = a.comment_text,
    al.source_system   = $source_system,
    al.source_table    = a.source_table,
    al.retrieved_at    = a.retrieved_at
MERGE (c)-[r:HAS_ALLEGATION]->(al)
SET r.source_table = "Allegations_Workfolder_Id", r.retrieved_at = a.retrieved_at
RETURN count(al) AS n
"""

_Q_SUBJECTS = """
MATCH (c:Case {case_id: $case_id})
UNWIND $subjects AS s
MERGE (subj:Subject {subject_id: s.subject_id})
SET subj.first_name    = s.first_name,
    subj.last_name     = s.last_name,
    subj.company_name  = s.company_name,
    subj.fein          = s.fein,
    subj.subject_type  = s.subject_type,
    subj.source_system = $source_system,
    subj.source_table  = s.source_table,
    subj.retrieved_at  = s.retrieved_at
MERGE (subj)-[r:APPEARS_IN_CASE]->(c)
SET r.subject_role = s.subject_role,
    r.is_primary   = s.is_primary,
    r.source_table = "Workfolder_SubjectsRelationship",
    r.retrieved_at = s.retrieved_at
RETURN count(subj) AS n
"""

_Q_ADDRESSES = """
UNWIND $rows AS row
MATCH (s:Subject {subject_id: row.subject_id})
MERGE (addr:Address {address_key: row.address_key})
SET addr.street            = row.street,
    addr.city              = row.city,
    addr.state             = row.state,
    addr.zip               = row.zip,
    addr.street_normalized = row.street_normalized,
    addr.source_system     = $source_system,
    addr.source_table      = "Subject_Address",
    addr.retrieved_at      = $retrieved_at
MERGE (s)-[r:HAS_ADDRESS]->(addr)
SET r.source_table = "Subject_Address", r.retrieved_at = $retrieved_at
RETURN count(r) AS n
"""

_Q_ALIASES = """
UNWIND $rows AS row
MATCH (s:Subject {subject_id: row.subject_id})
MERGE (al:Alias {alias_value: row.alias_value})
SET al.source_system = $source_system,
    al.source_table  = "Subject_Alias",
    al.retrieved_at  = $retrieved_at
MERGE (s)-[r:HAS_ALIAS]->(al)
SET r.source_table = "Subject_Alias", r.retrieved_at = $retrieved_at
RETURN count(r) AS n
"""

# coalesce(row.x, e.x) on every Employer property: a wage-sourced Employer
# node may be created with an AppWorks id and no FEIN, and a later
# job-sourced row may supply the FEIN for the same key. Overwriting with
# NULL would erase it. Never blank out a field the graph already knows.
_Q_EMPLOYERS = """
UNWIND $rows AS row
MATCH (s:Subject {subject_id: row.subject_id})
MERGE (e:Employer {employer_key: row.employer_key})
SET e.employer_name = coalesce(row.employer_name, e.employer_name),
    e.fein          = coalesce(row.fein, e.fein),
    e.employer_fid  = coalesce(row.employer_fid, e.employer_fid),
    e.source_system = $source_system,
    e.source_table  = "Subject_Job",
    e.retrieved_at  = $retrieved_at
MERGE (s)-[r:EMPLOYED_BY]->(e)
SET r.start_date   = row.start_date,
    r.end_date     = row.end_date,
    r.source_table = "Subject_Job",
    r.retrieved_at = $retrieved_at
RETURN count(r) AS n
"""

_Q_WAGES = """
UNWIND $rows AS row
MATCH (s:Subject {subject_id: row.subject_id})
MERGE (e:Employer {employer_key: row.employer_key})
SET e.employer_name = coalesce(row.employer_name, e.employer_name),
    e.fein          = coalesce(row.fein, e.fein),
    e.employer_fid  = coalesce(row.employer_fid, e.employer_fid),
    e.source_system = $source_system,
    e.retrieved_at  = $retrieved_at
MERGE (s)-[r:HAS_WAGE_RECORD_WITH {period_key: row.period_key}]->(e)
SET r.period_start = row.period_start,
    r.period_end   = row.period_end,
    r.wage_year    = row.wage_year,
    r.wage_quarter = row.wage_quarter,
    r.wage_amount  = row.wage_amount,
    r.source_table = "Subject_SubjectWages",
    r.retrieved_at = $retrieved_at
RETURN count(r) AS n
"""

# HAS_COMMENTARY appears in the reference doc's own Rule 14 worked example
# ((Case)-[:HAS_COMMENTARY]->(:Commentary)) but is absent from Section
# 3.2's relationship table. Loaded here from all three narrative sources
# Section 5.3 Step 3 names — Case commentary, Subject_Comment, and the
# Allegation comment field. Flagged in GAP_ANALYSIS.md as a relationship
# type the reference doc should state explicitly rather than leave
# implicit in an example.
_Q_COMMENTARY = """
UNWIND $rows AS row
MERGE (comm:Commentary {comment_id: row.comment_id})
SET comm.comment_text  = row.comment_text,
    comm.comment_type  = row.comment_type,
    comm.created_date  = row.created_date,
    comm.case_id       = $case_id,
    comm.source_system = $source_system,
    comm.source_table  = row.source_table,
    comm.retrieved_at  = $retrieved_at
WITH comm, row
// Attach each comment to whichever of Case/Subject/Allegation its attach_to
// names. Written with OPTIONAL MATCH + FOREACH rather than CALL subqueries:
// the bare `CALL { WITH ... }` form is deprecated in Neo4j 5.23+ (it wants
// the scoped `CALL (comm,row) {}` form, which in turn is not available on
// older 5.x). FOREACH-over-a-conditional-list is the one conditional-write
// idiom that is idempotent AND valid on every 4.x/5.x version, so it is the
// safe choice while the target Neo4j version is still settling. Two of the
// three OPTIONAL MATCHes bind null for any given row; the matching FOREACH
// writes exactly one HAS_COMMENTARY edge, the other two are no-ops.
OPTIONAL MATCH (c:Case {case_id: row.attach_id})
  WHERE row.attach_to = "case"
OPTIONAL MATCH (s:Subject {subject_id: row.attach_id})
  WHERE row.attach_to = "subject"
OPTIONAL MATCH (al:Allegation {allegation_id: row.attach_id})
  WHERE row.attach_to = "allegation"
FOREACH (x IN CASE WHEN c IS NOT NULL THEN [c] ELSE [] END |
    MERGE (x)-[r:HAS_COMMENTARY]->(comm)
    SET r.source_table = row.source_table, r.retrieved_at = $retrieved_at)
FOREACH (x IN CASE WHEN s IS NOT NULL THEN [s] ELSE [] END |
    MERGE (x)-[r:HAS_COMMENTARY]->(comm)
    SET r.source_table = row.source_table, r.retrieved_at = $retrieved_at)
FOREACH (x IN CASE WHEN al IS NOT NULL THEN [al] ELSE [] END |
    MERGE (x)-[r:HAS_COMMENTARY]->(comm)
    SET r.source_table = row.source_table, r.retrieved_at = $retrieved_at)
RETURN count(DISTINCT comm) AS n
"""

_Q_CO_SUBJECTS = """
UNWIND $pairs AS pair
MATCH (a:Subject {subject_id: pair.a})
MATCH (b:Subject {subject_id: pair.b})
MERGE (a)-[r:IS_CO_SUBJECT_WITH]-(b)
SET r.case_id      = $case_id,
    r.source_table = "Workfolder_SubjectsRelationship",
    r.retrieved_at = $retrieved_at
RETURN count(r) AS n
"""


def _flatten(subjects: List[Dict[str, Any]], child_key: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for subject in subjects:
        for child in subject.get(child_key, []):
            if isinstance(child, dict):
                rows.append({**child, "subject_id": subject["subject_id"]})
            else:  # aliases arrive as plain strings
                rows.append({"alias_value": child, "subject_id": subject["subject_id"]})
    return rows


def _tx_load(tx, data: Dict[str, Any]) -> Dict[str, int]:
    case = data["case"]
    case_id = case["case_id"]
    retrieved_at = data["retrieved_at"]
    common = {"source_system": SOURCE_SYSTEM, "retrieved_at": retrieved_at, "case_id": case_id}
    counts: Dict[str, int] = {}

    def run(query: str, key: str, **params) -> None:
        record = tx.run(query, **common, **params).single()
        counts[key] = int(record["n"]) if record and record["n"] is not None else 0

    run(_Q_CASE, "cases", case=case)

    targets = case.get("merge_target_case_ids") or []
    if targets:
        run(_Q_MERGED_INTO, "merged_into_case", target_case_ids=targets)
    else:
        counts["merged_into_case"] = 0

    subjects = data.get("subjects", [])
    allegations = data.get("allegations", [])
    commentary = data.get("commentary", [])

    if allegations:
        run(_Q_ALLEGATIONS, "allegations", allegations=allegations)
    else:
        counts["allegations"] = 0

    if subjects:
        run(_Q_SUBJECTS, "subjects", subjects=subjects)
    else:
        counts["subjects"] = 0

    for count_key, query, child_key in (
        ("addresses", _Q_ADDRESSES, "addresses"),
        ("aliases", _Q_ALIASES, "aliases"),
        ("employers", _Q_EMPLOYERS, "employers"),
        ("wage_records", _Q_WAGES, "wages"),
    ):
        rows = _flatten(subjects, child_key)
        if rows:
            run(query, count_key, rows=rows)
        else:
            counts[count_key] = 0

    if commentary:
        run(_Q_COMMENTARY, "commentary", rows=commentary)
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
        run(_Q_CO_SUBJECTS, "co_subject_pairs", pairs=pairs)
    else:
        counts["co_subject_pairs"] = 0

    return counts


def load_case_graph(data: Dict[str, Any]) -> Dict[str, int]:
    """
    Write fetch_case_graph()'s output into Neo4j in ONE write transaction.
    Atomic by design: a case is either fully in the graph or not in it at
    all. A half-loaded case is worse than no case — the Extraction Stage
    would read it as complete and produce confidently wrong output.
    """
    case_id = data["case"]["case_id"]
    with get_session() as session:
        counts = session.execute_write(_tx_load, data)
    logger.info("etl.graph_sync: LOADED case_id=%s %s", case_id, counts)
    return counts


def sync_case(case_id: str) -> Dict[str, int]:
    """Fetch then load, for one case. Retry policy belongs to the caller
    (etl/ingest_service.py), not here."""
    return load_case_graph(fetch_case_graph(case_id))