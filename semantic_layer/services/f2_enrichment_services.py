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

def get_enriched_subject_profile(subject_id: str) -> dict:
    """
    Fetches deep subject history and prior cases for a given subject_id.
    
    Args:
        subject_id (str): The unique identifier for the AppWorks Subject entity.
        
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

            # Fetch linked Workfolder summary
            logger.info(f"📂 Fetching linked Workfolder: {wf_id}")
            wf_props, _ = _fetch_props_and_links(wf_href)

            prior_cases.append({
                "workfolder_id":     wf_id,
                "complaint_no":      wf_props.get("WorkfolderComplaintNumber"),
                "status":            wf_props.get("WorkfolderStatus"),
                "description":       wf_props.get("WorkfolderDescription"),
                "case_description":  wf_props.get("Workfolder_CaseDescription"),
                "date_received":     wf_props.get("WorkfolderDateReceived"),
                "date_reported":     wf_props.get("WorkfolderDateReported"),
                "allegation":        wf_props.get("WorkFolderAllegation"),
                "team":              wf_props.get("TEAM_DISPLAY_NAME"),
                "destination":       wf_props.get("DESTINATION"),
                "is_primary_subject": is_primary,
                "mapping_title":     title_text,
            })

        except Exception as exc:
            logger.warning(f"⚠️  Failed processing mapping item: {exc}")

    logger.info(f"✅ {len(prior_cases)} prior case(s) found for Subject {subject_id}")

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
