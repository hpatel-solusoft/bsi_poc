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


def _fetch_commentary_type(type_href: str) -> str | None:
    """
    Fetches a CommentaryType entity and returns its type name.
    URL pattern: /OSABSIACM/entities/CommentaryType/items/{id}
    """
    if not type_href:
        return None
    props, _ = _fetch_props_and_links(type_href)
    # CommentaryType entity uses "Type" as the name field.
    return props.get("Type")


def _fetch_workfolder_commentary(wf_id: str) -> list[dict]:
    """
    Step 2: Fetches commentary entries for a Workfolder and returns a list of
    {commentary_text, commentary_type} dicts.

    Strategy:
      - The relationship list already embeds full Properties, so
        WorkfolderCommentary_Comment is read directly from each embedded item.
      - Each item's self href is fetched individually to obtain the full
        _links block, which contains the CommentaryType relationship link.
      - That link is followed to resolve the human-readable type name
        (e.g. "Disposition", "Analyst Notes", "Reviewer Notes").
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
        try:
            # ── Step A: read comment text from the embedded Properties ─────────
            # The relationship list already includes full Properties, so no
            # extra fetch is needed just for the text.
            embedded_props = item.get("Properties", {})
            comment_text = embedded_props.get("WorkfolderCommentary_Comment")

            # ── Step B: fetch the individual item to get its full _links ───────
            # The embedded item only carries a self link; the CommentaryType
            # relationship link is only present on the fully-fetched entity.
            self_href = item.get("_links", {}).get("self", {}).get("href", "")
            commentary_type = None
            if self_href:
                _, item_links = _fetch_props_and_links(self_href)
                type_href = (
                    item_links
                    .get("relationship:WorkfolderCommentary_CommentaryTypeRelationship", {})
                    .get("href", "")
                )
                if type_href:
                    logger.info(f"  🏷️  Fetching CommentaryType from: {type_href}")
                    commentary_type = _fetch_commentary_type(type_href)

            commentary_list.append({
                "commentary_text": comment_text,
                "commentary_type": commentary_type,
            })
        except Exception as exc:
            logger.warning(f"⚠️  Failed processing commentary item for Workfolder {wf_id}: {exc}")

    logger.info(f"  → {len(commentary_list)} commentary item(s) found for Workfolder {wf_id}")
    return commentary_list


def _fetch_workfolder_allegations(wf_id: str) -> list[dict]:
    """
    Step 3: Fetches full allegation details for a prior-case Workfolder.

    Mirrors the allegation-fetching logic in f1_intake_services.build_case_header_data
    exactly, so prior cases carry the same allegation structure as the active case.

    For each allegation:
      - Fetch the Allegations entity via its self link  → full props + _links
      - Follow relationship:Allegations_AllegationsType → allegation type name/desc
      - Follow relationship:Allegations_Source          → source agency name
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

    logger.info(f"  → {len(items)} allegation(s) found for Workfolder {wf_id}")
    allegations_list = []

    for alleg_item in items:
        try:
            alleg_self_href = alleg_item.get("_links", {}).get("self", {}).get("href", "")

            # AllegationType href may be present on the embedded item directly
            alleg_type_href = alleg_item.get("_links", {}).get(
                "relationship:Allegations_AllegationsType", {}
            ).get("href", "")

            # Fetch the full Allegations entity to get props + complete _links
            alleg_props, alleg_links = (
                _fetch_props_and_links(alleg_self_href) if alleg_self_href else ({}, {})
            )

            # Fall back to the fetched entity's _links if not on the embedded item
            if not alleg_type_href:
                alleg_type_href = alleg_links.get(
                    "relationship:Allegations_AllegationsType", {}
                ).get("href", "")

            allegation_type_id = (
                alleg_type_href.rstrip("/").split("/")[-1] if alleg_type_href else None
            )
            alleg_type_props, _ = (
                _fetch_props_and_links(alleg_type_href) if alleg_type_href else ({}, {})
            )

            agency_href = alleg_links.get("relationship:Allegations_Source", {}).get("href", "")
            agency_props, _ = (
                _fetch_props_and_links(agency_href) if agency_href else ({}, {})
            )

            allegation_description = (
                alleg_type_props.get("AllegationType_AllegationTypeDescription")
                or alleg_type_props.get("AllegationType_AllegationTypeShortDesc")
                or alleg_type_props.get("AllegationType_AllegationTypeDefaults")
                or alleg_props.get("Allegations_AllegationType")
                or alleg_props.get("Allegations_Comment")
                or f"Unknown allegation type {allegation_type_id or 'unknown'}"
            )

            allegations_list.append({
                "status":                  alleg_props.get("Allegations_Status"),
                "allegation_status":       alleg_props.get("Allegations_AllegationStatus"),
                "date_received":           alleg_props.get("Allegations_DateReceived"),
                "date_reported":           alleg_props.get("Allegations_DateReported"),
                "date_closed":             alleg_props.get("Allegations_DateClosed"),
                "closure_date_reported":   alleg_props.get("Allegations_ClosureDateReported"),
                "close_comment":           alleg_props.get("Allegations_AllegationCloseComment"),
                "comment":                 alleg_props.get("Allegations_Comment"),
                "agency_referral_no":      alleg_props.get("Allegations_AgencyReferralNumber"),
                "is_intake":               alleg_props.get("Allegations_IsIntakeAllegation"),
                "disposition_norris_code": alleg_props.get("Allegations_DispositionNorrisCode"),
                "dta_closure_report":      alleg_props.get("Allegations_DTAClosureReport"),
                "allegation_type": {
                    "id":          allegation_type_id,
                    "description": allegation_description,
                    "short_desc":  alleg_type_props.get("AllegationType_AllegationTypeShortDesc"),
                    "defaults":    alleg_type_props.get("AllegationType_AllegationTypeDefaults"),
                },
                "source_agency": {
                    "name":              agency_props.get("Agency_AgencyName"),
                    "short_description": agency_props.get("Agency_AgencyShortDescription"),
                },
            })
        except Exception as exc:
            logger.warning(f"⚠️  Failed processing allegation for Workfolder {wf_id}: {exc}")

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
