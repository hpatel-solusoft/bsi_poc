"""
Intake Services
---------------
Data functions for the Complaint Intelligence Agent (Agent 1).
Manifest tool: verify_case_intake
"""

import json
import logging
from datetime import datetime, timezone

from semantic_layer.appworks_auth import fetch

logger = logging.getLogger(__name__)


def build_case_header_data(case_id: str) -> dict:
    logger.info(f"🚀 [LIVE] Initiating deep fetch for Case ID: {case_id}")

    def fetch_entity(href):
        try:
            res = fetch(href)
            return res.get("Properties", {}), res.get("_links", {})
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch entity [{href}]: {str(e)}")
            return {}, {}

    def fetch_relationship_items(href, embedded_key):
        """Returns full embedded item list, preserving per-item _links."""
        try:
            res = fetch(href)
            if not res:
                return []
            
            # If the response itself is an entity item (has Properties and self link)
            # then it's likely a to-one relationship.
            props = res.get("Properties")
            links = res.get("_links", {})
            if props is not None and "self" in links:
                logger.info(f"Detected to-one relationship for {href}")
                return [res]

            embedded = res.get("_embedded", {})
            items = embedded.get(embedded_key)
            
            if isinstance(items, list):
                return items
            if isinstance(items, dict):
                return [items]
            
            # Fallback to _links.item (User guide pattern)
            if isinstance(links, dict):
                l_items = links.get("item")
                if isinstance(l_items, list):
                    return l_items
                if isinstance(l_items, dict):
                    return [l_items]
            
            # If still nothing, try any key in _embedded that is a list
            if isinstance(embedded, dict):
                for val in embedded.values():
                    if isinstance(val, list):
                        return val
                    if isinstance(val, dict):
                        return [val]

            return []
        except Exception as e:
            logger.warning(f"⚠️ fetch_relationship_items failed for {href}: {str(e)}")
            return []

    # 1. Fetch Main Workfolder
    try:
        endpoint = f"/entities/Workfolder/items/{case_id}"
        logger.info(f"📡 Requesting Workfolder from: {endpoint}")
        workfolder = fetch(endpoint)
        props = workfolder.get("Properties", {})
        links = workfolder.get("_links", {})
        logger.info(f"✅ Successfully retrieved Workfolder for {case_id}")
    except Exception as e:
        logger.error(f"❌ Critical Error fetching Workfolder: {str(e)}")
        props, links = {}, {}

    # 2. Classification Links
    logger.info("🔗 Fetching classification metadata...")

    def fetch_linked_props(link_key):
        try:
            href = links.get(link_key, {}).get("href")
            if href:
                p, _ = fetch_entity(href)
                return p
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch linked data [{link_key}]: {str(e)}")
        return {}

    entity_props   = fetch_linked_props("SolusoftACMConfig-relationship:EntityType")
    category_props = fetch_linked_props("SolusoftACMConfig-relationship:Category")
    request_props  = fetch_linked_props("SolusoftACMConfig-relationship:RequestType")

    # 3. Fetch Allegations
    # Each embedded item carries relationship:Allegations_AllegationsType in _links,
    # so we read the type ID directly from the list without an extra fetch per allegation.
    logger.info("📋 Fetching allegations...")
    allegations_list = []

    allegations_rel_href = links.get("relationship:Workfolder_AllegationsRelationship", {}).get("href")
    if allegations_rel_href:
        allegation_items = fetch_relationship_items(
            allegations_rel_href, "Workfolder_AllegationsRelationship"
        )
        logger.info(f"🔍 Found {len(allegation_items)} allegation(s)")

        for alleg_item in allegation_items:
            try:
                alleg_self_href = alleg_item.get("_links", {}).get("self", {}).get("href", "")

                # AllegationType href is on the embedded item _links directly
                alleg_type_href = alleg_item.get("_links", {}).get(
                    "relationship:Allegations_AllegationsType", {}
                ).get("href", "")

                alleg_props, alleg_links = fetch_entity(alleg_self_href) if alleg_self_href else ({}, {})

                # Fall back to fetched entity's _links if not in embedded item
                if not alleg_type_href:
                    alleg_type_href = alleg_links.get(
                        "relationship:Allegations_AllegationsType", {}
                    ).get("href", "")

                allegation_type_id = (
                    alleg_type_href.rstrip("/").split("/")[-1] if alleg_type_href else None
                )
                alleg_type_props, _ = fetch_entity(alleg_type_href) if alleg_type_href else ({}, {})

                agency_props, _ = fetch_entity(
                    alleg_links.get("relationship:Allegations_Source", {}).get("href", "")
                ) if alleg_links.get("relationship:Allegations_Source") else ({}, {})

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
                        "description": alleg_type_props.get("AllegationType_AllegationTypeDescription"),
                        "short_desc":  alleg_type_props.get("AllegationType_AllegationTypeShortDesc"),
                        "defaults":    alleg_type_props.get("AllegationType_AllegationTypeDefaults"),
                    },
                    "source_agency": {
                        "name":              agency_props.get("Agency_AgencyName"),
                        "short_description": agency_props.get("Agency_AgencyShortDescription"),
                    },
                })
            except Exception as e:
                logger.warning(f"⚠️ Failed processing allegation: {str(e)}")

    # 4. Fetch Subjects
    logger.info("👤 Fetching subjects...")
    subjects_list = []

    subjects_rel_href = links.get("relationship:Workfolder_SubjectsRelationship", {}).get("href")
    if subjects_rel_href:
        subject_items = fetch_relationship_items(
            subjects_rel_href, "Workfolder_SubjectsRelationship"
        )
        logger.info(f"🔍 Found {len(subject_items)} subject(s)")

        for subj_item in subject_items:
            try:
                subj_self_href = subj_item.get("_links", {}).get("self", {}).get("href", "")
                subj_props, subj_links = fetch_entity(subj_self_href) if subj_self_href else ({}, {})

                subject_detail_href = subj_links.get("relationship:Subjects_Subject", {}).get("href", "")
                subject_id = subject_detail_href.rstrip("/").split("/")[-1] if subject_detail_href else None
                subject_detail_props, subject_detail_links = (
                    fetch_entity(subject_detail_href) if subject_detail_href else ({}, {})
                )

                role_props, _ = fetch_entity(
                    subj_links.get("relationship:Subjects_SubjectRoleRelationship", {}).get("href", "")
                ) if subj_links.get("relationship:Subjects_SubjectRoleRelationship") else ({}, {})

                # Addresses
                addresses_list = []
                addr_rel_href = subject_detail_links.get("relationship:Subject_Address", {}).get("href")
                if addr_rel_href:
                    address_items = fetch_relationship_items(addr_rel_href, "Subject_Address")
                    logger.info(f"   📍 Found {len(address_items)} address(es) for subject")

                    for addr_item in address_items:
                        try:
                            addr_self = addr_item.get("_links", {}).get("self", {}).get("href", "")
                            addr_props, addr_links = fetch_entity(addr_self) if addr_self else ({}, {})

                            addr_type_props, _ = fetch_entity(
                                addr_links.get("relationship:Address_AddressType_Relation", {}).get("href", "")
                            ) if addr_links.get("relationship:Address_AddressType_Relation") else ({}, {})

                            scz_props, _ = fetch_entity(
                                addr_links.get("relationship:Address_StateCityZip_Relation", {}).get("href", "")
                            ) if addr_links.get("relationship:Address_StateCityZip_Relation") else ({}, {})

                            addresses_list.append({
                                "address":      addr_props.get("Address_Address"),
                                "apt_suite":    addr_props.get("Address_AptSuite"),
                                "zipcode":      addr_props.get("Address_Zipcode"),
                                "address_type": addr_type_props.get("AddressType_Type"),
                                "city":         scz_props.get("StateCityZip_City"),
                                "state":        scz_props.get("StateCityZip_State"),
                                "county":       scz_props.get("StateCityZip_County"),
                            })
                        except Exception as e:
                            logger.warning(f"⚠️ Failed processing address: {str(e)}")

                # Aliases
                aliases_list = []
                alias_rel_href = subject_detail_links.get("relationship:Subject_Alias", {}).get("href")
                if alias_rel_href:
                    try:
                        alias_res = fetch(alias_rel_href)
                        for alias_item in alias_res.get("_embedded", {}).get("Subject_Alias", []):
                            alias_val = alias_item.get("Properties", {}).get("Alias")
                            if alias_val:
                                aliases_list.append(alias_val)
                    except Exception as e:
                        logger.warning(f"⚠️ Failed fetching aliases: {str(e)}")

                subjects_list.append({
                    "subject_id":         subject_id,
                    "subject_type":       subj_props.get("Subjects_SubjectType"),
                    "is_primary_subject": subj_props.get("Subjects_IsPrimarySubject"),
                    "role":               role_props.get("RoleName"),
                    "details": {
                        "identifier":             subject_detail_props.get("Subject_Identifier"),
                        "first_name":             subject_detail_props.get("Subject_FirstName"),
                        "middle_initial":         subject_detail_props.get("Subject_MiddleInitial"),
                        "last_name":              subject_detail_props.get("Subject_LastName"),
                        "ssn":                    subject_detail_props.get("Subject_SSN"),
                        "ein":                    subject_detail_props.get("Subject_EIN"),
                        "gender":                 subject_detail_props.get("Subject_Gender"),
                        "dob":                    subject_detail_props.get("Subject_DOB"),
                        "dod":                    subject_detail_props.get("Subject_DOD"),
                        "phone_number":           subject_detail_props.get("Subject_PhoneNumber"),
                        "subject_type":           subject_detail_props.get("Subject_SubjectType"),
                        "company_name":           subject_detail_props.get("Subject_CompanyName"),
                        "provider_number":        subject_detail_props.get("Subject_ProviderNumber"),
                        "pob":                    subject_detail_props.get("Subject_POB"),
                        "driving_license_number": subject_detail_props.get("Subject_DrivingLicenseNumber"),
                        "comment":                subject_detail_props.get("Subject_Comment"),
                        "destination":            subject_detail_props.get("Subject_Destination"),
                        "date_entered":           subject_detail_props.get("Subject_Date_Entered"),
                        "aliases":                subject_detail_props.get("Subject_Aliases"),
                    },
                    "addresses":    addresses_list,
                    "alias_records": aliases_list,
                })
            except Exception as e:
                logger.warning(f"⚠️ Failed processing subject: {str(e)}")

    # 5. Fetch Financials
    logger.info("💰 Fetching financials...")
    financials_list = []
    total_calculated = 0.0
    total_ordered    = 0.0

    financials_rel_href = links.get("relationship:Workfolder_FinancialRelationship", {}).get("href")
    if financials_rel_href:
        financial_items = fetch_relationship_items(
            financials_rel_href, "Workfolder_FinancialRelationship"
        )
        logger.info(f"🔍 Found {len(financial_items)} financial record(s)")

        for fin_item in financial_items:
            try:
                fin_self_href = fin_item.get("_links", {}).get("self", {}).get("href", "")
                fin_props, fin_links = fetch_entity(fin_self_href) if fin_self_href else ({}, {})

                # Fraud type link from financial
                type_href = fin_links.get("relationship:Financial_PrimaryFraudTypeRelationShip", {}).get("href", "")
                type_props, _ = fetch_entity(type_href) if type_href else ({}, {})
                fraud_type = type_props.get("Classification_Name")

                calc = float(fin_props.get("Financial_Calculated") or 0.0)
                ordr = float(fin_props.get("Financial_Ordered") or 0.0)
                total_calculated += calc
                total_ordered    += ordr

                financials_list.append({
                    "calculated":  calc,
                    "ordered":     ordr,
                    "comment":     fin_props.get("Financial_Comment"),
                    "start_date":  fin_props.get("Financial_RequestedStartDate"),
                    "end_date":    fin_props.get("Financial_RequestedEndDate"),
                    "date":        fin_props.get("Financial_Date"),
                    "fraud_type":  fraud_type,
                })
            except Exception as e:
                logger.warning(f"⚠️ Failed processing financial: {str(e)}")

    # 6. Build Clean Result
    clean_result = {
        "case_id": props.get("CASEID", case_id),
        "summary": {
            "complaint_no":     props.get("WorkfolderComplaintNumber"),
            "description":      props.get("WorkfolderDescription"),
            "case_description": props.get("Workfolder_CaseDescription"),
            "status":           props.get("WorkfolderStatus"),
            "destination":      props.get("DESTINATION"),
            "team":             props.get("TEAM_DISPLAY_NAME"),
            "created":          props.get("CREATION_DATE"),
        },
        "classification": {
            "entity_text":   entity_props.get("ENTITY_TEXT"),
            "entity_code":   entity_props.get("ENTITY_CODE"),
            "category_text": category_props.get("CATEGORY_TEXT"),
            "category_code": category_props.get("CATEGORY_CODE"),
            "request_type":  request_props.get("REQUEST_TYPE"),
        },
        "details": {
            "intake_referral_no":    props.get("WorkfolderIntakeReferralNumber"),
            "source":                props.get("WorkfolderSource"),
            "identifier_name":       props.get("IDENTIFIER_NAME"),
            "identifier_ssn_or_ein": props.get("IDENTIFIER_SSNorEIN"),
            "date_reported":         props.get("WorkfolderDateReported"),
            "date_reported_age":     props.get("WorkfolderDateReportedAge"),
            "date_received":         props.get("WorkfolderDateReceived"),
            "date_received_age":     props.get("WorkfolderDateReceivedAge"),
            "date_entered_age":      props.get("WorkfolderDateEnteredAge"),
            "workfolder_allegation": props.get("WorkFolderAllegation"),
            "co_subject_name":       props.get("WorkfolderCoSubjectName"),
            "subject_city":          props.get("WorkfolderSubjectCity"),
        },
        "allegations": allegations_list,
        "subjects":    subjects_list,
        "financials": {
            "records":          financials_list,
            "total_calculated": total_calculated,
            "total_ordered":    total_ordered,
        },
        "subject_primary_id": next((s["subject_id"] for s in subjects_list if s.get("is_primary_subject")), None),
        "fraud_types": list(set(a["allegation_type"]["description"] for a in allegations_list if a.get("allegation_type"))),
    }

    logger.info(
        f"✅ clean_result built — {len(allegations_list)} allegation(s), "
        f"{len(subjects_list)} subject(s)"
    )
    logger.info(
        f"📦 Payload size: {len(json.dumps(clean_result))} bytes"
    )

    return {
        "result": clean_result,
        "provenance": {
            "sources":      [f"AppWorks case record {case_id}"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by":  "AppWorks REST retrieval",
        },
    }