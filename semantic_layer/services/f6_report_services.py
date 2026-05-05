# semantic_layer/services/f6_report_services.py
# ----------------------------------------------------------------
# Agent 6: Final Investigation Report
# ----------------------------------------------------------------
# Every section is built from live AppWorks REST data.
# Manifest params: case_id, subject_id, fraud_types, risk_score,
#                  risk_tier, triggered_rules
# ----------------------------------------------------------------

import logging
from datetime import datetime, timezone
from semantic_layer.appworks_auth import fetch
from semantic_layer.semantic_model import FinalReport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------------------

def _fetch_props_links(href: str):
    try:
        res = fetch(href)
        return res.get("Properties", {}), res.get("_links", {})
    except Exception as e:
        logger.warning(f"⚠️  fetch failed [{href}]: {e}")
        return {}, {}


def _fetch_embedded(href: str, key: str) -> list:
    try:
        res = fetch(href)
        return res.get("_embedded", {}).get(key, [])
    except Exception as e:
        logger.warning(f"⚠️  embedded fetch failed [{href}]: {e}")
        return []


def _self_href(item: dict) -> str:
    return item.get("_links", {}).get("self", {}).get("href", "")


# ---------------------------------------------------------------
# SECTION BUILDERS
# ---------------------------------------------------------------

def _build_case_summary(case_id: str, wf_props: dict) -> str:
    complaint_no  = wf_props.get("WorkfolderComplaintNumber") or case_id
    description   = wf_props.get("WorkfolderDescription") or wf_props.get("Workfolder_CaseDescription") or "No description recorded"
    date_received = wf_props.get("WorkfolderDateReceived") or wf_props.get("CREATION_DATE") or "Unknown"
    date_reported = wf_props.get("WorkfolderDateReported") or "Unknown"
    source        = wf_props.get("WorkfolderSource") or "Unknown"
    referral_no   = wf_props.get("WorkfolderIntakeReferralNumber") or "N/A"
    team          = wf_props.get("TEAM_DISPLAY_NAME") or "Unassigned"
    destination   = wf_props.get("DESTINATION") or "Unknown"
    status        = wf_props.get("WorkfolderStatus") or "Open"
    return (
        f"Complaint #{complaint_no} (Case ID: {case_id}) was received on {date_received} "
        f"and reported on {date_reported}. Source: {source}. Referral number: {referral_no}. "
        f"Description: {description}. "
        f"Assigned team: {team}. Current destination: {destination}. Status: {status}."
    )


def _build_subject_history(case_id: str, wf_links: dict) -> str:
    lines = []
    subj_rel_href = wf_links.get("relationship:Workfolder_SubjectsRelationship", {}).get("href")
    if not subj_rel_href:
        return "No subject records found in AppWorks."
    subj_items = _fetch_embedded(subj_rel_href, "Workfolder_SubjectsRelationship")
    if not subj_items:
        return "No subject records found in AppWorks."

    for s_item in subj_items:
        s_props, s_links = _fetch_props_links(_self_href(s_item))
        is_primary = s_props.get("Subjects_IsPrimarySubject", False)
        subj_type  = s_props.get("Subjects_SubjectType") or "Individual"
        detail_href = s_links.get("relationship:Subjects_Subject", {}).get("href")
        if not detail_href:
            continue
        d_props, d_links = _fetch_props_links(detail_href)
        subject_id  = detail_href.rstrip("/").split("/")[-1]
        first_name  = d_props.get("Subject_FirstName") or ""
        last_name   = d_props.get("Subject_LastName") or ""
        full_name   = f"{first_name} {last_name}".strip() or "Unknown"
        dob         = d_props.get("Subject_DOB") or "Not recorded"
        ssn         = d_props.get("Subject_SSN") or ""
        ein         = d_props.get("Subject_EIN") or ""
        identifier  = ssn or ein or d_props.get("Subject_Identifier") or "Not recorded"
        company     = d_props.get("Subject_CompanyName") or ""
        provider_no = d_props.get("Subject_ProviderNumber") or ""
        role_href = s_links.get("relationship:Subjects_SubjectRoleRelationship", {}).get("href")
        role_name = "Subject"
        if role_href:
            role_props, _ = _fetch_props_links(role_href)
            role_name = role_props.get("RoleName") or "Subject"
        mapping_href = d_links.get("relationship:Subject_SubjectWorkfolderMapping", {}).get("href")
        prior_case_ids = []
        if mapping_href:
            mappings = _fetch_embedded(mapping_href, "Subject_SubjectWorkfolderMapping")
            for m in mappings:
                wf_rel_href = m.get("_links", {}).get(
                    "relationship:SubjectWorkfolderMapping_WorkfolderRelation", {}
                ).get("href", "")
                if wf_rel_href:
                    wf_id = wf_rel_href.rstrip("/").split("/")[-1]
                    title = m.get("Title", {}).get("Title") or f"Case {wf_id}"
                    prior_case_ids.append(title)
        addr_href = d_links.get("relationship:Subject_Address", {}).get("href")
        address_lines = []
        if addr_href:
            addrs = _fetch_embedded(addr_href, "Subject_Address")
            for a_item in addrs:
                a_props, a_links = _fetch_props_links(_self_href(a_item))
                scz_href = a_links.get("relationship:Address_StateCityZip_Relation", {}).get("href")
                scz_props = {}
                if scz_href:
                    scz_props, _ = _fetch_props_links(scz_href)
                city    = scz_props.get("StateCityZip_City") or ""
                state   = scz_props.get("StateCityZip_State") or ""
                country = scz_props.get("StateCityZip_County") or ""
                zipcode = a_props.get("Address_Zipcode") or ""
                apt     = a_props.get("Address_AptSuite") or ""
                addr_str = ", ".join(filter(None, [apt, city, state, zipcode, country]))
                if addr_str:
                    address_lines.append(addr_str)
        alias_href = d_links.get("relationship:Subject_Alias", {}).get("href")
        aliases = []
        if alias_href:
            alias_items = _fetch_embedded(alias_href, "Subject_Alias")
            for a in alias_items:
                alias_val = a.get("Properties", {}).get("Alias")
                if alias_val:
                    aliases.append(alias_val)
        primary_label = "PRIMARY" if is_primary else "SECONDARY"
        lines.append(
            f"[{primary_label}] {full_name} (Role: {role_name}, Type: {subj_type}, "
            f"Subject ID: {subject_id}). "
            + (f"Company: {company}. " if company else "")
            + (f"Provider No: {provider_no}. " if provider_no else "")
            + f"Identifier: {identifier}. DOB: {dob}. "
            + (f"Addresses: {'; '.join(address_lines)}. " if address_lines else "No address recorded. ")
            + (f"Aliases: {', '.join(aliases)}. " if aliases else "")
            + (f"Prior case mappings: {'; '.join(prior_case_ids)}." if prior_case_ids
               else "No prior cases found.")
        )
    return " | ".join(lines) if lines else "No subject history available."


def _build_allegation_summary(wf_links: dict) -> str:
    rel_href = wf_links.get("relationship:Workfolder_AllegationsRelationship", {}).get("href")
    if not rel_href:
        return "No allegations recorded in AppWorks."
    items = _fetch_embedded(rel_href, "Workfolder_AllegationsRelationship")
    if not items:
        return "No allegations recorded in AppWorks."
    parts = []
    for item in items:
        a_props, a_links = _fetch_props_links(_self_href(item))
        status     = a_props.get("Allegations_AllegationStatus") or a_props.get("Allegations_Status") or "Unknown"
        date_recv  = a_props.get("Allegations_DateReceived") or "Unknown"
        date_rep   = a_props.get("Allegations_DateReported") or "Unknown"
        date_close = a_props.get("Allegations_DateClosed") or "Open"
        comment    = a_props.get("Allegations_Comment") or ""
        agency_ref = a_props.get("Allegations_AgencyReferralNumber") or ""
        type_href = a_links.get("relationship:Allegations_AllegationsType", {}).get("href")
        type_desc = "Unknown type"
        if type_href:
            t_props, _ = _fetch_props_links(type_href)
            type_desc = (
                t_props.get("AllegationType_AllegationTypeDescription")
                or t_props.get("AllegationType_AllegationTypeDefaults")
                or "Unknown type"
            )
        source_href = a_links.get("relationship:Allegations_Source", {}).get("href")
        agency_name = ""
        if source_href:
            src_props, _ = _fetch_props_links(source_href)
            agency_name = src_props.get("Agency_AgencyName") or src_props.get("Agency_AgencyShortDescription") or ""
        parts.append(
            f"Type: {type_desc} | Status: {status} | "
            f"Received: {date_recv} | Reported: {date_rep} | Closed: {date_close}"
            + (f" | Agency: {agency_name}" if agency_name else "")
            + (f" | Ref: {agency_ref}" if agency_ref else "")
            + (f" | Note: {comment}" if comment else "")
        )
    return "; ".join(parts) if parts else "No allegation details available."


def _build_financial_summary(wf_links: dict) -> str:
    fin_href = wf_links.get("relationship:Workfolder_FinancialRelationship", {}).get("href")
    if not fin_href:
        return "No financial records recorded in AppWorks."
    items = _fetch_embedded(fin_href, "Workfolder_FinancialRelationship")
    if not items:
        return "No financial records recorded in AppWorks."
    parts = []
    total_ordered    = 0.0
    total_calculated = 0.0
    for item in items:
        f_props, f_links = _fetch_props_links(_self_href(item))
        ordered    = f_props.get("Financial_Ordered") or "0"
        calculated = f_props.get("Financial_Calculated") or "0"
        start_date = f_props.get("Financial_RequestedStartDate") or "Unknown"
        end_date   = f_props.get("Financial_RequestedEndDate") or "Unknown"
        comment    = f_props.get("Financial_Comment") or ""
        try:
            total_ordered    += float(ordered)
            total_calculated += float(calculated)
        except (ValueError, TypeError):
            pass
        class_href = f_links.get("relationship:Financial_PrimaryFraudTypeRelationShip", {}).get("href")
        fraud_label = "General"
        if class_href:
            c_props, _ = _fetch_props_links(class_href)
            fraud_label = c_props.get("Classification_Name") or "General"
        parts.append(
            f"Type: {fraud_label} | Ordered: {ordered} | Calculated: {calculated} | "
            f"Period: {start_date} to {end_date}"
            + (f" | Note: {comment}" if comment else "")
        )
    summary = "; ".join(parts)
    summary += (
        f" | TOTALS — Ordered: {round(total_ordered, 2)}, "
        f"Calculated: {round(total_calculated, 2)}"
    )
    return summary


def _build_analyst_notes(wf_links: dict) -> str:
    rel_href = wf_links.get(
        "relationship:Workfolder_WorkfolderCommentaryNewRelationship", {}
    ).get("href")
    if not rel_href:
        return "No analyst commentary recorded in AppWorks."
    items = _fetch_embedded(rel_href, "Workfolder_WorkfolderCommentaryNewRelationship")
    if not items:
        return "No analyst commentary recorded in AppWorks."
    notes = []
    for item in items:
        c_props, c_links = _fetch_props_links(_self_href(item))
        comment = c_props.get("WorkfolderCommentary_Comment") or ""
        ct_href = c_links.get(
            "relationship:WorkfolderCommentary_CommentaryTypeRelationship", {}
        ).get("href")
        ct_label = "Note"
        if ct_href:
            ct_props, _ = _fetch_props_links(ct_href)
            ct_label = ct_props.get("Type") or "Note"
        if comment:
            notes.append(f"[{ct_label}] {comment}")
    return " | ".join(notes) if notes else "No analyst commentary recorded."


# ---------------------------------------------------------------
# TOOL: compile_and_render_report
# ---------------------------------------------------------------

def compile_and_render_report(
    case_id: str,
    subject_id: str,
    fraud_types: list,
    risk_score: float,
    risk_tier: str,
    triggered_rules: list,
) -> dict:
    """
    Compiles a structured investigation report from live AppWorks data
    enriched with risk assessment context from prior tool calls.

    Manifest params provide grounding context:
      case_id, subject_id, fraud_types, risk_score, risk_tier, triggered_rules

    Five AppWorks data sections — all text derived from field values:
      case_summary        ← Workfolder properties
      subject_history     ← Subject entity + address + prior cases + aliases
      allegation_summary  ← Allegations + AllegationType + Agency
      financial_summary   ← Financial amounts + fraud type + period
      analyst_notes       ← WorkfolderCommentary + CommentaryType

    Plus risk context injected from manifest params.
    """
    logger.info(f"📄 Compiling report data for Case: {case_id}")

    # Coerce risk_score
    if isinstance(risk_score, str):
        try:
            risk_score = float(risk_score)
        except (ValueError, TypeError):
            risk_score = 0.0

    # Root workfolder fetch
    try:
        wf_res  = fetch(f"/entities/Workfolder/items/{case_id}")
        wf_props = wf_res.get("Properties", {})
        wf_links = wf_res.get("_links", {})
    except Exception as e:
        raise ValueError(f"Could not fetch Workfolder {case_id}: {e}")

    if not wf_props:
        raise ValueError(f"Case {case_id} not found in AppWorks")

    # Build each section from live data
    case_summary       = _build_case_summary(case_id, wf_props)
    subject_history    = _build_subject_history(case_id, wf_links)
    allegation_summary = _build_allegation_summary(wf_links)
    financial_summary  = _build_financial_summary(wf_links)
    analyst_notes      = _build_analyst_notes(wf_links)

    # Build risk assessment section from manifest params
    triggered_display = []
    for tr in (triggered_rules or []):
        if isinstance(tr, dict):
            triggered_display.append(
                f"{tr.get('rule_id', 'Unknown')}: {tr.get('rule_name', '')} "
                f"({tr.get('display', '')})"
            )
        elif isinstance(tr, str):
            triggered_display.append(tr)

    risk_assessment_text = (
        f"Risk Score: {risk_score} | Risk Tier: {risk_tier} | "
        f"Triggered Rules: {'; '.join(triggered_display) if triggered_display else 'None'}"
    )

    sections = {
        "case_summary":        case_summary,
        "subject_history":     subject_history,
        "allegation_summary":  allegation_summary,
        "financial_summary":   financial_summary,
        "risk_assessment":     risk_assessment_text,
        "recommended_actions": (
            "Refer to risk tier recommendation and investigation playbook steps "
            "from the preceding workflow phases."
        ),
        "analyst_notes":       analyst_notes,
    }

    report_id    = f"REP-{case_id}-{datetime.now().strftime('%Y%m%d')}"
    generated_at = datetime.now(timezone.utc).isoformat()

    validated = FinalReport(
        report_id    = report_id,
        case_id      = case_id,
        generated_at = generated_at,
        sections     = sections,
        status       = "DRAFT",
    )

    logger.info(f"✅ Report compiled for Case {case_id}")

    return {
        "result": validated.model_dump(),
        "provenance": {
            "sources": [
                f"AppWorks case record {case_id}",
                f"AppWorks subject record {subject_id}",
            ],
            "retrieved_at":  generated_at,
            "computed_by": "AppWorks REST retrieval",
        },
    }
