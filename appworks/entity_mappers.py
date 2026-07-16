"""
Entity Mappers
--------------
Shared domain parsers to convert OpenText AppWorks REST payloads
into canonical BSI semantic dictionaries. Ensures DRY compliance across
agents — currently consumed by appworks/case_intake.py (verify_case_intake)
and appworks/subject_enrichment.py (fetch_subject_history). Any change here
must be checked against both callers, not just the one you're working on.

AS OF THIS ROUND: subjects, addresses, allegations, and financials are
fetched through the newer /lists/ endpoints, which embed each related
entity's Properties/Identity directly on the row (see
appworks_utils.embedded / embedded_id). One call now does what used to be
2-3 (item -> related item -> related item). Aliases and per-record child
tables AppWorks hasn't moved to a /lists/ endpoint (Subject_Alias,
Subject_Job) are untouched and still go through the old relationship/
childEntities href chase.

TIER 1 PII (reference doc Section 3.5): Subject_SSN and
Subject_DrivingLicenseNumber are present on the new Subjects_Subject$Properties
payload but are deliberately never read into any dict this module returns.
Every subject field this module extracts is named explicitly below — there
is no wholesale "spread the embedded Properties dict" anywhere in this file
— so a new AppWorks field appearing upstream (SSN or otherwise) cannot leak
through by accident. If you're adding a subject field, add it by name here;
never widen this to `**detail_props`.
"""

import logging
from typing import Dict, List, Any

from appworks.appworks_utils import (
    extract_id_from_href,
    get_relationship_items,
    embedded,
    embedded_id,
)
from appworks.provenance import ProvenanceTracker
from appworks.appworks_paths import AppWorksPaths as AW

logger = logging.getLogger(__name__)


def map_workfolder_core(wf_props: Dict[str, Any]) -> Dict[str, Any]:
    """Standardizes extraction of core workfolder properties."""
    return {
        "complaint_no":                      wf_props.get("WorkfolderComplaintNumber"),
        "status":                            wf_props.get("WorkfolderStatus"),
        "description":                       wf_props.get("WorkfolderDescription"),
        "case_description":                  wf_props.get("Workfolder_CaseDescription"),
        "date_received":                     wf_props.get("WorkfolderDateReceived"),
        "date_reported":                     wf_props.get("WorkfolderDateReported"),
        "allegation":                        wf_props.get("WorkFolderAllegation"),
        "workfolder_allegations_description": wf_props.get("WorkfolderAllegationsDescription"),
        "workfolder_reviewer_comments":       wf_props.get("WorkfolderReviewerComments"),
        "workfolder_analyst_comments":        wf_props.get("WorkfolderAnalystComments"),
    }


def fetch_commentary_rows(workfolder_id: str, tracker: ProvenanceTracker) -> List[Dict[str, Any]]:
    """
    Single call: returns the raw per-comment pieces (WorkfolderCommentary
    Properties, Tracking, embedded CommentaryType Properties) with no
    case_intake-specific shaping applied. Reused by etl/graph_sync.py for
    its own :Commentary node shaping (comment_id, created_date, etc.).
    """
    rows: List[Dict[str, Any]] = []
    if not workfolder_id:
        return rows

    href = AW.CommentaryList.by_workfolder(workfolder_id)
    for item in get_relationship_items(href, "WorkfolderCommentary_All"):
        item_id = item.get("Identity", {}).get("Id") or extract_id_from_href(
            item.get("_links", {}).get("item", {}).get("href", "")
        )
        if item_id:
            tracker.add_source("WorkfolderCommentary", item_id)

        rows.append({
            "comment_id": str(item_id) if item_id else None,
            "props": item.get("Properties", {}),
            "tracking": item.get("Tracking", {}),
            "type_props": embedded(item, "WorkfolderCommentary_CommentaryTypeRelationship"),
            # Case-level field, embedded on the relationship back to the
            # parent Workfolder — WorkfolderCommentary_All repeats the same
            # value on every row (it describes the case, not the comment).
            "workfolder_rel_props": embedded(item, "WorkfolderCommentary_WorkfolderRelationship"),
        })
    return rows


def map_commentary(workfolder_id: str, tracker: ProvenanceTracker) -> Dict[str, Any]:
    """
    Shared mapper to fetch and normalize Workfolder Commentary for the
    agent-facing (subject_enrichment) shape. Single call via
    fetch_commentary_rows: WorkfolderCommentary_All embeds CommentaryType
    per row, so this no longer does a second fetch per comment.

    Takes a bare workfolder_id (not an href) — matches how
    subject_enrichment.py already calls this.

    Every row on WorkfolderCommentary_All also embeds
    WorkfolderCommentary_WorkfolderRelationship$Properties, which carries
    WorkfolderAllegationsDescription — a case-level field, not a per-comment
    one, so AppWorks repeats the identical value on every single row. Naively
    copying it onto every item in the returned list would duplicate the same
    string N times for N comments. Instead this reads it once, from the
    first row that has a non-empty value, and returns it as a separate
    top-level field alongside the (deduplicated) commentary items.

    Returns {"items": [...], "count": int, "allegation_description": str|None}
    rather than a bare list — this is a return-shape change from the prior
    round. map_commentary's only caller is subject_enrichment.py (checked at
    the time of this change); etl/graph_sync.py builds its own Commentary
    shaping from fetch_commentary_rows directly and does not call this
    function.
    """
    logger.info("💬 Fetching commentary...")
    items: List[Dict[str, Any]] = []
    allegation_description: Any = None
    if not workfolder_id:
        return {"items": items, "count": 0, "allegation_description": allegation_description}

    for row in fetch_commentary_rows(workfolder_id, tracker):
        try:
            items.append({
                "commentary_text": row["props"].get("WorkfolderCommentary_Comment"),
                "commentary_type": row["type_props"].get("Type"),
            })

            if allegation_description is None:
                rel_props = row.get("workfolder_rel_props") or {}
                desc = rel_props.get("WorkfolderAllegationsDescription")
                if desc:
                    allegation_description = desc
        except Exception as exc:
            logger.warning(f"⚠️ Failed processing commentary item: {exc}")

    return {
        "items": items,
        "count": len(items),
        "allegation_description": allegation_description,
    }


def fetch_allegation_rows(workfolder_id: str, tracker: ProvenanceTracker) -> List[Dict[str, Any]]:
    """
    Single call: returns the raw per-allegation pieces (Allegation Properties,
    embedded AllegationType Properties/id, embedded Agency Properties/id)
    with no case_intake-specific shaping applied.

    This is the layer etl/graph_sync.py builds on, same split as
    fetch_subject_rows/map_subjects: one fetch, two independent shapers
    (map_allegations for the agent-facing shape, graph_sync's own transform
    for Section 3.1's canonical Neo4j field names).
    """
    rows: List[Dict[str, Any]] = []
    if not workfolder_id:
        return rows

    href = AW.Allegations.by_workfolder(workfolder_id)
    for item in get_relationship_items(href, "Allegations_All"):
        alleg_id = item.get("Identity", {}).get("Id") or extract_id_from_href(
            item.get("_links", {}).get("item", {}).get("href", "")
        )
        if not alleg_id:
            logger.warning("⚠️ Allegation row with no resolvable id skipped")
            continue
        tracker.add_source("Allegation", alleg_id)

        type_id = embedded_id(item, "Allegations_AllegationsType")
        if type_id:
            tracker.add_source("AllegationType", type_id)

        agency_id = embedded_id(item, "Allegations_Source")
        if agency_id:
            tracker.add_source("Agency", agency_id)

        rows.append({
            "allegation_id": str(alleg_id),
            "alleg_props": item.get("Properties", {}),
            "type_props": embedded(item, "Allegations_AllegationsType"),
            "type_id": type_id,
            "agency_props": embedded(item, "Allegations_Source"),
            "agency_id": agency_id,
        })
    return rows


def map_allegations(workfolder_id: str, tracker: ProvenanceTracker) -> List[Dict]:
    """
    Shared mapper to fetch and normalize allegations for the agent-facing
    (case_intake) shape. Single call via fetch_allegation_rows: Allegations_All
    embeds AllegationType and Agency per row, so this no longer does two
    extra fetch calls per allegation.

    Takes a bare workfolder_id (not an href) — matches how
    subject_enrichment.py already calls this.
    """
    logger.info("📋 Fetching allegations...")
    allegations_list = []
    if not workfolder_id:
        return allegations_list

    for row in fetch_allegation_rows(workfolder_id, tracker):
        try:
            alleg_props = row["alleg_props"]
            type_props = row["type_props"]
            type_id = row["type_id"]
            agency_props = row["agency_props"]

            allegation_description = (
                type_props.get("AllegationType_AllegationTypeDescription")
                or type_props.get("AllegationType_AllegationTypeShortDesc")
                or type_props.get("AllegationType_AllegationTypeDefaults")
                or alleg_props.get("Allegations_AllegationType")
                or alleg_props.get("Allegations_Comment")
                or f"Unknown allegation type {type_id or 'unknown'}"
            )

            allegations_list.append({
                "allegation_id":           row["allegation_id"],
                "status":                  alleg_props.get("Allegations_Status"),
                "allegation_status":       alleg_props.get("Allegations_AllegationStatus"),
                "date_received":           alleg_props.get("Allegations_DateReceived"),
                "date_reported":           alleg_props.get("Allegations_DateReported"),
                "date_closed":             alleg_props.get("Allegations_DateClosed"),
                "closure_date_reported":   alleg_props.get("Allegations_ClosureDateReported"),
                "close_comment":           alleg_props.get("Allegations_AllegationCloseComment"),
                "comment":                 alleg_props.get("Allegations_Comment"),
                # New fields confirmed on the Allegations_All payload — not
                # captured anywhere before this round.
                "agency_referral_number":  alleg_props.get("Allegations_AgencyReferralNumber"),
                "completed_date":          alleg_props.get("Allegations_AllegationDateCompleted"),
                "norris_code":             alleg_props.get("Allegations_DispositionNorrisCode"),
                "allegation_type": {
                    "id":          type_id,
                    "description": allegation_description,
                    "short_desc":  type_props.get("AllegationType_AllegationTypeShortDesc"),
                    "defaults":    type_props.get("AllegationType_AllegationTypeDefaults"),
                },
                "source_agency": {
                    "name":              agency_props.get("Agency_AgencyName"),
                    "short_description": agency_props.get("Agency_AgencyShortDescription"),
                } if agency_props else None,
            })
        except Exception as exc:
            logger.error(f"⚠️ Error mapping individual allegation: {exc}")

    return allegations_list


def map_financials(workfolder_id: str, tracker: ProvenanceTracker) -> Dict[str, Any]:
    """
    Fetches and normalizes linked financial records, keeping the per-record
    primary fraud type (Financial_PrimaryFraudTypeRelationShip$Properties/
    $Identity) that the new Financial_All list endpoint embeds on every row.
    Previously this identity was fetched but discarded per record — only the
    case-level aggregate totals were kept.

    Single call: no more per-record fetch to resolve FraudTypeClassification.
    """
    logger.info("💰 Fetching financials...")
    records: List[Dict[str, Any]] = []
    total_calculated = 0.0
    total_ordered = 0.0

    if not workfolder_id:
        return {"records": records, "total_calculated": 0.0, "total_ordered": 0.0}

    href = AW.FinancialList.by_workfolder(workfolder_id)
    items = get_relationship_items(href, "Financial_All")
    logger.info(f"🔍 Found {len(items)} financial record(s)")

    for item in items:
        try:
            props = item.get("Properties", {})
            fin_id = item.get("Identity", {}).get("Id") or extract_id_from_href(
                item.get("_links", {}).get("item", {}).get("href", "")
            )
            if fin_id:
                tracker.add_source("Financial", fin_id)

            fraud_type_props = embedded(item, "Financial_PrimaryFraudTypeRelationShip")
            fraud_type_id = embedded_id(item, "Financial_PrimaryFraudTypeRelationShip")

            calc = float(props.get("Financial_Calculated") or 0.0)
            ordr = float(props.get("Financial_Ordered") or 0.0)
            total_calculated += calc
            total_ordered += ordr

            records.append({
                "calculated":    calc,
                "ordered":       ordr,
                "comment":       props.get("Financial_Comment"),
                "start_date":    props.get("Financial_RequestedStartDate"),
                "end_date":      props.get("Financial_RequestedEndDate"),
                "date":          props.get("Financial_Date"),
                "fraud_type":    fraud_type_props.get("Classification_Name"),
                "fraud_type_id": fraud_type_id,
            })
        except Exception as e:
            logger.error(f"⚠️ Error mapping individual financial record: {str(e)}")

    return {
        "records":          records,
        "total_calculated": total_calculated,
        "total_ordered":    total_ordered,
    }


def fetch_subject_rows(workfolder_id: str, tracker: ProvenanceTracker) -> List[Dict[str, Any]]:
    """
    Single call: returns the raw per-subject pieces (the Subjects bridge row
    Properties, the embedded Subject detail Properties, the embedded
    SubjectRole Properties, and the resolved subject_detail_id) with no
    case_intake-specific shaping applied.

    This is the layer etl/graph_sync.py builds on — it needs the same raw
    fields map_subjects() does (to avoid re-fetching), but shapes them into
    Section 3.1's canonical Neo4j field names rather than case_intake's
    agent-facing 'details' nesting. map_subjects() is the shaped, agent-
    facing version of this same fetch.

    TIER 1 GUARD applies here too: detail_props is returned whole (unlike
    map_subjects, which whitelists fields), so Subject_SSN and
    Subject_DrivingLicenseNumber ARE present in the returned detail_props
    dict. Callers of this function must not persist those two keys anywhere
    — see reference doc Section 3.5. map_subjects() is the safe,
    field-whitelisted entry point; use fetch_subject_rows() only when you
    are about to apply your own explicit whitelist immediately after.
    """
    rows: List[Dict[str, Any]] = []
    if not workfolder_id:
        return rows

    href = AW.Subjects.by_workfolder(workfolder_id)
    for item in get_relationship_items(href, "All_Subjects"):
        subject_detail_id = embedded_id(item, "Subjects_Subject")
        if not subject_detail_id:
            logger.warning("⚠️ Subject row with no resolvable Subjects_Subject id skipped")
            continue

        subject_row_id = item.get("Identity", {}).get("Id")
        tracker.add_source("Subject", subject_row_id)
        tracker.add_source("SubjectDetail", subject_detail_id)

        role_props = embedded(item, "Subjects_SubjectRoleRelationship")
        role_id = embedded_id(item, "Subjects_SubjectRoleRelationship")
        if role_id:
            tracker.add_source("SubjectRole", role_id)

        rows.append({
            "subject_row_id": subject_row_id,
            "subject_id": subject_detail_id,
            "subj_props": item.get("Properties", {}),
            "detail_props": embedded(item, "Subjects_Subject"),
            "role_props": role_props,
        })
    return rows


def map_subject_addresses(subject_id: str, tracker: ProvenanceTracker) -> List[Dict[str, Any]]:
    """
    Single call: Address_All embeds AddressType and StateCityZip per row
    (Address_AddressType_Relation$Properties, Address_StateCityZip_Relation$Properties).
    Replaces the old per-address chase of 2 extra fetch calls.
    """
    addresses_list: List[Dict[str, Any]] = []
    if not subject_id:
        return addresses_list

    href = AW.AddressList.by_subject(subject_id)
    items = get_relationship_items(href, "Address_All")

    for item in items:
        try:
            props = item.get("Properties", {})
            addr_id = item.get("Identity", {}).get("Id") or extract_id_from_href(
                item.get("_links", {}).get("item", {}).get("href", "")
            )
            if addr_id:
                tracker.add_source("Address", addr_id)

            type_props = embedded(item, "Address_AddressType_Relation")
            scz_props = embedded(item, "Address_StateCityZip_Relation")

            addresses_list.append({
                "address": props.get("Address_Address"),
                "apt_suite": props.get("Address_AptSuite"),
                "zipcode": props.get("Address_Zipcode"),
                "address_type": type_props.get("AddressType_Type"),
                "city": scz_props.get("StateCityZip_City"),
                "state": scz_props.get("StateCityZip_State"),
                "county": scz_props.get("StateCityZip_County"),
            })
        except Exception as e:
            logger.error(f"⚠️ Error mapping individual address: {str(e)}")

    return addresses_list


def map_subject_aliases(subject_id: str) -> List[str]:
    """Unchanged endpoint shape (childEntities/Subject_Alias) — AppWorks has
    not moved this one to a /lists/ endpoint, so it was already a single call."""
    aliases_list: List[str] = []
    if not subject_id:
        return aliases_list

    href = AW.Subject.aliases(subject_id)
    for alias_item in get_relationship_items(href, "Subject_Alias"):
        val = alias_item.get("Properties", {}).get("Alias")
        if val:
            aliases_list.append(val)
    return aliases_list


def map_subjects(workfolder_id: str, tracker: ProvenanceTracker) -> List[Dict[str, Any]]:
    """
    Fetches and normalizes every subject on a case.
    Single call for the subject row itself: All_Subjects embeds the Subject
    detail record and SubjectRole name per row (Subjects_Subject$Properties,
    Subjects_SubjectRoleRelationship$Properties) — replaces the old 3-call
    chase (Subjects item -> Subject detail item -> SubjectRole item).
    Addresses are a second call per subject (still single-call each, see
    map_subject_addresses); aliases a third (unchanged, see map_subject_aliases).

    TIER 1 GUARD: Subject_SSN and Subject_DrivingLicenseNumber are present on
    detail_props but are never read below. Do not add them here — see this
    module's docstring.
    """
    logger.info("👤 Fetching subjects...")
    subjects_list: List[Dict[str, Any]] = []
    if not workfolder_id:
        return subjects_list

    rows = fetch_subject_rows(workfolder_id, tracker)
    logger.info(f"🔍 Found {len(rows)} subject(s)")

    for row in rows:
        try:
            subject_detail_id = row["subject_id"]
            subj_props = row["subj_props"]
            detail_props = row["detail_props"]
            role_props = row["role_props"]

            addresses_list = map_subject_addresses(subject_detail_id, tracker)
            alias_records = map_subject_aliases(subject_detail_id)

            subjects_list.append({
                "subject_id": subject_detail_id,
                "subject_type": subj_props.get("Subjects_SubjectType"),
                "is_primary_subject": subj_props.get("Subjects_IsPrimarySubject"),
                "role": role_props.get("RoleName"),
                "details": {
                    "identifier": detail_props.get("Subject_Identifier"),
                    "first_name": detail_props.get("Subject_FirstName"),
                    "middle_initial": detail_props.get("Subject_MiddleInitial"),
                    "last_name": detail_props.get("Subject_LastName"),
                    "gender": detail_props.get("Subject_Gender"),
                    "dob": detail_props.get("Subject_DOB"),
                    "dod": detail_props.get("Subject_DOD"),
                    "phone_number": detail_props.get("Subject_PhoneNumber"),
                    "subject_type": detail_props.get("Subject_SubjectType"),
                    "company_name": detail_props.get("Subject_CompanyName"),
                    "provider_number": detail_props.get("Subject_ProviderNumber"),
                    "pob": detail_props.get("Subject_POB"),
                    "comment": detail_props.get("Subject_Comment"),
                    "destination": detail_props.get("Subject_Destination"),
                    "date_entered": detail_props.get("Subject_Date_Entered"),
                    "aliases": detail_props.get("Subject_Aliases"),
                    # Subject_SSN / Subject_DrivingLicenseNumber intentionally
                    # omitted — Tier 1 PII, reference doc Section 3.5.
                },
                "addresses": addresses_list,
                "alias_records": alias_records,
            })
        except Exception as e:
            logger.error(f"⚠️ Error mapping individual subject: {str(e)}")

    return subjects_list