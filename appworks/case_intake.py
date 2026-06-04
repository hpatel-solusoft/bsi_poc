import logging
from typing import Dict, List, Any

# Assuming these are imported from the new foundation files
from appworks.appworks_utils import safe_fetch, extract_id_from_href, get_relationship_items
from appworks.provenance import ProvenanceTracker
from appworks.entity_mappers import map_allegations
from appworks.appworks_paths import AppWorksPaths

logger = logging.getLogger(__name__)

def _parse_subjects(case_links: Dict, tracker: ProvenanceTracker) -> List[Dict]:
    """
    Parses all subjects, traversing deeply into details, addresses, and aliases.
    Records every visited entity into the provenance tracker.
    """
    logger.info("👤 Fetching subjects...")
    subjects_list = []
    
    rel_href = case_links.get("relationship:Workfolder_SubjectsRelationship", {}).get("href")
    if not rel_href:
        return subjects_list

    subject_items = get_relationship_items(rel_href, "Workfolder_SubjectsRelationship")
    logger.info(f"🔍 Found {len(subject_items)} subject(s)")

    for subj_item in subject_items:
        try:
            # 1. Base Subject Metadata
            subj_self_href = subj_item.get("_links", {}).get("self", {}).get("href", "")
            subj_props, subj_links = safe_fetch(subj_self_href, "Subject")
            tracker.add_source("Subject", extract_id_from_href(subj_self_href))

            # 2. Subject Details (The canonical identity record)
            detail_href = subj_links.get("relationship:Subjects_Subject", {}).get("href", "")
            detail_props, detail_links = safe_fetch(detail_href, "SubjectDetail")
            subject_detail_id = extract_id_from_href(detail_href)
            tracker.add_source("SubjectDetail", subject_detail_id)

            # 3. Subject Role
            role_href = subj_links.get("relationship:Subjects_SubjectRoleRelationship", {}).get("href", "")
            role_props, _ = safe_fetch(role_href, "SubjectRole")
            tracker.add_source("SubjectRole", extract_id_from_href(role_href))

            # 4. Nested Addresses
            addresses_list = []
            addr_rel_href = detail_links.get("relationship:Subject_Address", {}).get("href")
            if addr_rel_href:
                address_items = get_relationship_items(addr_rel_href, "Subject_Address")
                for addr_item in address_items:
                    try:
                        addr_self = addr_item.get("_links", {}).get("self", {}).get("href", "")
                        addr_props, addr_links = safe_fetch(addr_self, "Address")
                        tracker.add_source("Address", extract_id_from_href(addr_self))

                        type_href = addr_links.get("relationship:Address_AddressType_Relation", {}).get("href", "")
                        type_props, _ = safe_fetch(type_href, "AddressType")
                        tracker.add_source("AddressType", extract_id_from_href(type_href))

                        scz_href = addr_links.get("relationship:Address_StateCityZip_Relation", {}).get("href", "")
                        scz_props, _ = safe_fetch(scz_href, "StateCityZip")
                        tracker.add_source("StateCityZip", extract_id_from_href(scz_href))

                        addresses_list.append({
                            "address": addr_props.get("Address_Address"),
                            "apt_suite": addr_props.get("Address_AptSuite"),
                            "zipcode": addr_props.get("Address_Zipcode"),
                            "address_type": type_props.get("AddressType_Type"),
                            "city": scz_props.get("StateCityZip_City"),
                            "state": scz_props.get("StateCityZip_State"),
                            "county": scz_props.get("StateCityZip_County"),
                        })
                    except Exception as e:
                        logger.error(f"⚠️ Error mapping individual address: {str(e)}")

            # 5. Nested Aliases
            aliases_list = []
            alias_rel_href = detail_links.get("relationship:Subject_Alias", {}).get("href")
            if alias_rel_href:
                alias_items = get_relationship_items(alias_rel_href, "Subject_Alias")
                for alias_item in alias_items:
                    val = alias_item.get("Properties", {}).get("Alias")
                    if val:
                        aliases_list.append(val)

            # Assemble Subject Map
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
                    "subject_type": detail_props.get("Subject_SubjectType"),
                    "company_name": detail_props.get("Subject_CompanyName"),
                    "provider_number": detail_props.get("Subject_ProviderNumber"),
                    "pob": detail_props.get("Subject_POB"),
                    "comment": detail_props.get("Subject_Comment"),
                    "destination": detail_props.get("Subject_Destination"),
                    "date_entered": detail_props.get("Subject_Date_Entered"),
                    "aliases": detail_props.get("Subject_Aliases"), 
                },
                "addresses": addresses_list,
                "alias_records": aliases_list,
            })
        except Exception as e:
            logger.error(f"⚠️ Error mapping individual subject: {str(e)}")

    return subjects_list


def _parse_financials(case_links: Dict, tracker: ProvenanceTracker) -> Dict:
    """
    Parses linked financial records and aggregates totals.
    """
    logger.info("💰 Fetching financials...")
    financials_list = []
    total_calculated = 0.0
    total_ordered = 0.0

    rel_href = case_links.get("relationship:Workfolder_FinancialRelationship", {}).get("href")
    if not rel_href:
        return {"records": [], "total_calculated": 0.0, "total_ordered": 0.0}

    fin_items = get_relationship_items(rel_href, "Workfolder_FinancialRelationship")
    logger.info(f"🔍 Found {len(fin_items)} financial record(s)")

    for item in fin_items:
        try:
            self_href = item.get("_links", {}).get("self", {}).get("href", "")
            fin_props, fin_links = safe_fetch(self_href, "Financial")
            tracker.add_source("Financial", extract_id_from_href(self_href))

            type_href = fin_links.get("relationship:Financial_PrimaryFraudTypeRelationShip", {}).get("href", "")
            type_props, _ = safe_fetch(type_href, "FraudTypeClassification")
            tracker.add_source("FraudTypeClassification", extract_id_from_href(type_href))

            calc = float(fin_props.get("Financial_Calculated") or 0.0)
            ordr = float(fin_props.get("Financial_Ordered") or 0.0)
            total_calculated += calc
            total_ordered += ordr

            financials_list.append({
                "calculated": calc,
                "ordered": ordr,
                "comment": fin_props.get("Financial_Comment"),
                "start_date": fin_props.get("Financial_RequestedStartDate"),
                "end_date": fin_props.get("Financial_RequestedEndDate"),
                "date": fin_props.get("Financial_Date"),
                "fraud_type": type_props.get("Classification_Name"),
            })
        except Exception as e:
            logger.error(f"⚠️ Error mapping individual financial record: {str(e)}")

    return {
        #"records": financials_list, ## [HP]Depending on needs, we can include the full list or just the totals in the final payload, No need to feed this to LLM except the amoun
        "total_calculated": total_calculated,
        "total_ordered": total_ordered,
    }


def _derive_fraud_types(allegations_list: List[Dict], case_props: Dict) -> List[str]:
    """
    Extracts a deduplicated list of fraud types from allegations, 
    falling back to legacy case properties if no allegations exist.
    """
    types = []
    for alleg in allegations_list:
        desc = (
            alleg.get("allegation_type", {}).get("description") or 
            alleg.get("allegation_type", {}).get("short_desc")
        )
        if desc and desc not in types:
            types.append(desc)
            
    if not types:
        # Fallback for dirty data / legacy schema
        fallback = (
            case_props.get("WorkfolderAllegation") or 
            case_props.get("WorkFolderAllegation") or 
            case_props.get("Workfolder_Allegation") or 
            case_props.get("WorkFolder_Allegation")
        )
        if isinstance(fallback, str) and fallback.strip():
            types.append(fallback.strip())
            
    return types


def build_case_header_data(case_id: str) -> Dict[str, Any]:
    """
    Main orchestrator for the verify_case_intake tool.
    Fetches the root Workfolder and delegates deep relationship traversal to parsers.
    Returns the strict architecture-compliant {result, provenance} envelope.
    """
    logger.info(f"🚀 [LIVE] Initiating deep fetch for Case ID: {case_id}")

    # 1. Initialize System-Wide Provenance Tracker (Principle 8)
    tracker = ProvenanceTracker("Workfolder", case_id)

    # 2. Fetch Root Workfolder
    endpoint = AppWorksPaths.Workfolder.item(case_id)
    logger.info(f"📡 Requesting Workfolder from: {endpoint}")
    props, links = safe_fetch(endpoint, "Workfolder")

    # Guard clause: If the core case doesn't exist or API is down, fail safely
    if not props:
        logger.error(f"❌ Critical Error: Could not fetch root Workfolder for {case_id}")
        return {
            "result": {"case_id": case_id, "error": "Case record not found or API unavailable"},
            "provenance": tracker.get_provenance_block(computed_by="Failed REST retrieval")
        }

    logger.info(f"✅ Successfully retrieved Workfolder for {case_id}")

    
    # Safely drill down to the actual string URL
    allegations_href = links.get("relationship:Workfolder_AllegationsRelationship", {}).get("href")

    allegations_list = map_allegations(allegations_href, tracker)
    subjects_list = _parse_subjects(links, tracker)
    financials = _parse_financials(links, tracker)

    # 4. Synthesize Derived Fields
    fraud_types = _derive_fraud_types(allegations_list, props)
    subject_ids = [s["subject_id"] for s in subjects_list if s.get("subject_id")]
    primary_subject_id = next((s["subject_id"] for s in subjects_list if s.get("is_primary_subject")), None)

    # 5. Build Final Semantic Payload
    clean_result = {
        "case_id": props.get("CASEID", case_id),
        "summary": {
            "complaint_no": props.get("WorkfolderComplaintNumber"),
            "description": props.get("WorkfolderDescription"),
            "case_description": props.get("Workfolder_CaseDescription"),
            "status": props.get("WorkfolderStatus"),
            "created": props.get("CREATION_DATE"),
        },
        "details": {
            "source": props.get("WorkfolderSource"),
            "identifier_name": props.get("IDENTIFIER_NAME"),
            "date_reported": props.get("WorkfolderDateReported"),
            "date_reported_age": props.get("WorkfolderDateReportedAge"),
            "date_received": props.get("WorkfolderDateReceived"),
            "date_received_age": props.get("WorkfolderDateReceivedAge"),
            "date_entered_age": props.get("WorkfolderDateEnteredAge"),
            "workfolder_allegation": props.get("WorkFolderAllegation"),
            "co_subject_name": props.get("WorkfolderCoSubjectName"),
            "subject_city": props.get("WorkfolderSubjectCity"),
        },
        "allegations": allegations_list,
        "subjects": subjects_list,
        "subject_ids": subject_ids,
        "subject_primary_id": primary_subject_id,
        "financials": financials,
        "fraud_types": fraud_types,
    }

    logger.info(
        f"✅ clean_result built — {len(allegations_list)} allegation(s), "
        f"{len(subjects_list)} subject(s)"
    )

    # 6. Return Architecture Envelope
    return {
        "result": clean_result,
        "provenance": tracker.get_provenance_block()
    }