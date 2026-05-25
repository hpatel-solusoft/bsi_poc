import logging
from datetime import datetime, timezone
from semantic_layer.appworks_auth import fetch

logger = logging.getLogger(__name__)

def _extract_id_from_href(href: str) -> str | None:
    if href:
        return href.rstrip("/").split("/")[-1]
    return None

def _safe_fetch(href: str) -> dict:
    try:
        return fetch(href)
    except Exception as exc:
        logger.warning(f"⚠️  fetch failed [{href}]: {exc}")
        return {}

def _fetch_props_and_links(href: str) -> tuple[dict, dict]:
    res = _safe_fetch(href)
    return res.get("Properties", {}), res.get("_links", {})

def _extract_workfolder_core_props(wf_props: dict) -> dict:
    """
    Step 1: Extracts core properties and extended text fields from a Workfolder properties dict.
    """
    return {
        "complaint_no":                      wf_props.get("WorkfolderComplaintNumber"),
        "status":                            wf_props.get("WorkfolderStatus"),
        "description":                       wf_props.get("WorkfolderDescription"),
        "case_description":                  wf_props.get("Workfolder_CaseDescription"),
        "date_received":                     wf_props.get("WorkfolderDateReceived"),
        "date_reported":                     wf_props.get("WorkfolderDateReported"),
        "allegation":                        wf_props.get("WorkFolderAllegation"),
        "team":                              wf_props.get("TEAM_DISPLAY_NAME"),
        "destination":                       wf_props.get("DESTINATION"),
        "workfolder_allegations_description": wf_props.get("WorkfolderAllegationsDescription"),
        "workfolder_reviewer_comments":       wf_props.get("WorkfolderReviewerComments"),
        "workfolder_analyst_comments":        wf_props.get("WorkfolderAnalystComments"),
    }


def _fetch_workfolder_commentary(wf_id: str) -> list[dict]:
    """
    Step 2: Fetches commentary entries for a Workfolder and returns a list of
    {commentary_text, commentary_type} dicts.
    """
    commentary_href = (
        f"/entities/Workfolder/items/{wf_id}"
        f"/relationships/Workfolder_WorkfolderCommentaryNewRelationship"
    )
    logger.info(f"💬 Fetching commentary for Workfolder: {wf_id}")
    res = _safe_fetch(commentary_href)

    items = (
        res
        .get("_embedded", {})
        .get("Workfolder_WorkfolderCommentaryNewRelationship", [])
    )

    commentary_list = []
    for item in items:
        props = item.get("Properties", {})
        commentary_list.append({
            "commentary_text": props.get("WorkfolderCommentaryText"),
            "commentary_type": props.get("WorkfolderCommentaryType"),
        })

    logger.info(f"  → {len(commentary_list)} commentary item(s) found for Workfolder {wf_id}")
    return commentary_list


def _fetch_workfolder_allegations(wf_id: str) -> list[str]:
    """
    Step 3: Fetches allegations for a Workfolder and returns a list of
    AllegationDescription strings.
    """
    allegations_href = (
        f"/entities/Workfolder/items/{wf_id}"
        f"/relationships/Workfolder_AllegationsRelationship"
    )
    logger.info(f"⚖️  Fetching allegations for Workfolder: {wf_id}")
    res = _safe_fetch(allegations_href)

    items = (
        res
        .get("_embedded", {})
        .get("Workfolder_AllegationsRelationship", [])
    )

    allegations_list = [
        item.get("Properties", {}).get("AllegationDescription")
        for item in items
        if item.get("Properties", {}).get("AllegationDescription")
    ]

    logger.info(f"  → {len(allegations_list)} allegation(s) found for Workfolder {wf_id}")
    return allegations_list

def get_enriched_subject_profile(subject_id: str, case_id: str = None) -> dict:
    """
    Fetches deep subject history and prior cases for a given subject_id.
    The current case (case_id) is excluded from prior_cases and prior_case_count
    so the active investigation is not counted as its own prior history.

    Args:
        subject_id (str): The unique identifier for the AppWorks Subject entity.
        case_id (str, optional): The current case being investigated — excluded from results.

    Returns:
        dict: A structured dictionary containing the subject profile and data provenance.
    """
    logger.info(f"🚀 [LIVE] Context Enrichment for Subject ID: {subject_id}")

    # ── Step 1: Fetch Base Subject Info ──────────────────────────────────────
    subject_href = f"/entities/Subject/items/{subject_id}"
    subj_props, subj_links = _fetch_props_and_links(subject_href)

    first_name = subj_props.get("Subject_FirstName", "")
    last_name  = subj_props.get("Subject_LastName", "")
    dob        = subj_props.get("Subject_DOB")

    # ── Step 2: Get Subject_SubjectWorkfolderMapping list ───────────────
    mapping_href = (
        f"/entities/Subject/items/{subject_id}"
        f"/childEntities/Subject_SubjectWorkfolderMapping"
    )
    mapping_res  = _safe_fetch(mapping_href)
    mapping_items = (
        mapping_res
        .get("_embedded", {})
        .get("Subject_SubjectWorkfolderMapping", [])
    )

    logger.info(f"📋 Found {len(mapping_items)} Subject_SubjectWorkfolderMapping entry/entries for Subject {subject_id}")

    prior_cases = []

    for mapping_item in mapping_items:
        try:
            mapping_self_href = (
                mapping_item
                .get("_links", {})
                .get("self", {})
                .get("href", "")
            )
            is_primary = (
                mapping_item
                .get("Properties", {})
                .get("SubjectWorkfolderMapping_IsPrimary", False)
            )
            title_text = mapping_item.get("Title", {}).get("Title", "")

            mapping_detail = _safe_fetch(mapping_self_href)
            mapping_links  = mapping_detail.get("_links", {})

            # Extract linked Workfolder href & ID
            wf_href = (
                mapping_links
                .get("relationship:SubjectWorkfolderMapping_WorkfolderRelation", {})
                .get("href", "")
            )
            wf_id = _extract_id_from_href(wf_href)

            if not wf_id:
                continue

            # Exclude the current case — it is the case being investigated,
            # not a historical prior case.
            if case_id and str(wf_id) == str(case_id):
                logger.info(f"  Skipping current case {wf_id} from prior case history")
                continue

            # Fetch linked Workfolder summary
            logger.info(f"📂 Fetching linked Workfolder: {wf_id}")
            wf_props, _ = _fetch_props_and_links(wf_href)

            """
            TO DO :
            Create a seperate function for each of the following steps to keep the code clean and modular: 
            1) From the reponse of first call to Workfolder/item/658259969, extract the following properties: WorkfolderComplaintNumber, WorkfolderStatus, WorkfolderDescription, Workfolder_CaseDescription, WorkfolderDateReceived, WorkfolderDateReported, WorkFolderAllegation, TEAM_DISPLAY_NAME, DESTINATION
                - get the WorkfolderAllegationsDescription, WorkfolderReviewerComments , WorkfolderAnalystComments properties from the workolder properties
                - add to the prior_cases list as workfolder_allegations_description, workfolder_reviewer_comments, workfolder_analyst_comments keys respectively

            2) Call a function to fetch workfolder commentary with url "/entities/Workfolder/items/658259969/relationships/Workfolder_WorkfolderCommentaryNewRelationship"
                - iterate through the commentary items and extract the WorkfolderCommentaryText and WorkfolderCommentaryType properties
                - add to the prior_cases list as a list of dictionaries with keys "commentary_text" and "commentary_type"
            3) Call a function to fetch workfolder Allegations with url "/entities/Workfolder/items/658259969/relationships/Workfolder_AllegationsRelationship"
                - iterate through the allegations items and extract the AllegationDescription property
                - add to the prior_cases list as a list of allegation descriptions
            """
            
            # create a function to fetch workfolder commentary with url "/entities/Workfolder/items/658259969/relationships/Workfolder_WorkfolderCommentaryNewRelationship"
            # ite
            # prior_cases.append({
            #     "workfolder_id":     wf_id,
            #     "complaint_no":      wf_props.get("WorkfolderComplaintNumber"),
            #     "status":            wf_props.get("WorkfolderStatus"),
            #     "description":       wf_props.get("WorkfolderDescription"),
            #     "case_description":  wf_props.get("Workfolder_CaseDescription"),
            #     # "workfolder_allegations_description": "Grantee Denise M. Ferreira (AP-0044213) has been employed at BrightPath Home Health LLC (EIN: 04-7821334) since March 2023. DOR Wagematch confirms wages Q1 2023 through Q4 2025. Income was not reported to DTA at any point. Subject remains active on SNAP AU2 and MassHealth Standard. Prior employment closure BSI-2022-0614 on record.",
            #     # "workfolder_analyst_comments" : "FS AU2. Referral for subject working since Mar 2023. DOR confirms wages at BrightPath Home Health LLC: 2023 total $15,400 (Q1 $3,600 / Q2 $3,900 / Q3 $4,100 / Q4 $3,800). 2024 total $16,900 (Q1 $3,950 / Q2 $4,200 / Q3 $4,450 / Q4 $4,300). 2025 total $14,900 (Q1 $3,800 / Q2 $4,100 / Q3 $3,900 / Q4 $3,100). Subject did not report this income. Recommend assignment.",
            #     # "workfolder_reviewer_comments" :" Reviewer agrees with preliminary analysis. Prior case BSI-2022-0614 (civil recovery $8,450) confirms established pattern of unreported employment income. ",
            #     # "case_commentary" : [{'FastTrack':'DTA referral received 02/10/2026 – Employment/DOR case. Assign to Sandra Delgado AD queue. Subject Denise M. Ferreira AP-0044213. Unreported wages BrightPath Home Health LLC confirmed via DOR Wagematch.'},{'General':'Prior case BSI-2022-0614 (closed Aug 2022) – civil recovery $8,450 – unreported part-time wages Sunrise Care Services LLC. Same pattern confirmed.'},{'Financial':'DTA calculation request sent to DTA Calculation Unit. MH calc packet submitted to MassHealth HIX'},{'Financial':'DTA calc received. FS overpayment: $38,750. MH calc pending estimated $9,100'},{'General':'BrightPath Home Health LLC employment verification received. Confirms Ferreira employed as Home Health Aide since 03/14/2023. Employer not listed in DTA record at any point.'}],
            #     "date_received":     wf_props.get("WorkfolderDateReceived"),
            #     "date_reported":     wf_props.get("WorkfolderDateReported"),
            #     "allegation":        wf_props.get("WorkFolderAllegation"),
            #     "team":              wf_props.get("TEAM_DISPLAY_NAME"),
            #     "destination":       wf_props.get("DESTINATION"),
            #     "is_primary_subject": is_primary,
            #     "mapping_title":     title_text,
            # })
            # ── Step 1: Extract core + extended Workfolder properties ──────────
            core_props = _extract_workfolder_core_props(wf_props)

            # ── Step 2: Fetch commentary ───────────────────────────────────────
            commentary = _fetch_workfolder_commentary(wf_id)

            # ── Step 3: Fetch allegations ──────────────────────────────────────
            allegations = _fetch_workfolder_allegations(wf_id)

            prior_cases.append({
                "workfolder_id":      wf_id,
                "is_primary_subject": is_primary,
                "mapping_title":      title_text,
                **core_props,                        # unpacks all Step 1 fields
                "commentary":         commentary,    # list of {text, type} dicts
                "allegations":        allegations,   # list of description strings
            })
        except Exception as exc:
            logger.warning(f"⚠️  Failed processing mapping item: {exc}")

    logger.info(f"✅ {len(prior_cases)} prior case(s) found for Subject {subject_id} (current case excluded)")

    # Return our architectural envelope
    return {
        "result": {
            "subject_id": subject_id,
            "first_name": first_name,
            "last_name": last_name,
            "dob": dob,
            "prior_cases": prior_cases,
            "prior_case_count": len(prior_cases),
        },
        "provenance": {
            "sources": [
                f"AppWorks subject record {subject_id}",
                f"AppWorks Subject_SubjectWorkfolderMapping"
            ],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "AppWorks REST retrieval"
        }
    }