"""
AppWorks REST path registry.
Single source of truth for every AppWorks endpoint path in the system.
No path string is written in any other file — call these methods instead.

Usage:
    from appworks.appworks_paths import AW
    fetch(AW.Workfolder.item(case_id))
    fetch(AW.Subject.workfolder_mappings(subject_id))
    fetch(AW.Lists.allegations_by_type(type_id))
"""


class AppWorksPaths:
    class FraudRules:
        @staticmethod
        def risk_rules_all() -> str:
            return "/OSABSIACM/entities/FraudRiskRules/lists/FraudRiskRules_FraudRiskRulesListInternal"
        
        @staticmethod
        def risk_rules_by_id(id: str) -> str:
            return f"/entities/FraudRiskRules/items/{id}/childEntities/Rules"
        
    class Workfolder:
        @staticmethod
        def item(id: str) -> str:
            return f"/entities/Workfolder/items/{id}"

        # NOTE: allegations(), commentary(), financial(), and subjects() were
        # removed here — each was a single-item relationship chase (1 call to
        # get the relationship, then 1 fetch per child, then 1 more per
        # grandchild). They're fully replaced by the /lists/ endpoints below
        # (Allegations.by_workfolder, CommentaryList.by_workfolder,
        # FinancialList.by_workfolder, Subjects.by_workfolder), which return
        # every child row with its related entities already embedded in one
        # call. Nothing in the active codebase referenced the old methods.

    class Subject:
        @staticmethod
        def item(id: str) -> str:
            return f"/entities/Subject/items/{id}"

        @staticmethod
        def workfolder_mappings(id: str) -> str:
            return f"/entities/Subject/items/{id}/childEntities/Subject_SubjectWorkfolderMapping"

        @staticmethod
        def workfolder_mapping_item(subject_id: str, mapping_id: str) -> str:
            return (
                f"/entities/Subject/items/{subject_id}"
                f"/childEntities/Subject_SubjectWorkfolderMapping/items/{mapping_id}"
                
            )

        @staticmethod
        def addresses(id: str) -> str:
            return f"/entities/Subject/items/{id}/relationships/Subject_Address"

        @staticmethod
        def aliases(id: str) -> str:
            return f"/entities/Subject/items/{id}/childEntities/Subject_Alias"

        @staticmethod
        def jobs(id: str) -> str:
            # Sources EMPLOYED_BY (Section 3.2) — employer_name/fein per employment
            # record. Never registered before this round: Phase 1's case_intake
            # only needed Address/Alias, not Employer, so this path was never
            # added even though the endpoint itself isn't new or AppWorks-side
            # missing — see etl/GAP_ANALYSIS.md.
            return f"/entities/Subject/items/{id}/childEntities/Subject_Job"

        @staticmethod
        def wages(id: str) -> str:
            # Sources HAS_WAGE_RECORD_WITH (Section 3.2) — a separate,
            # independent path to Subject-Employer linkage from the Job table,
            # per the reference doc's own "(independent path, better coverage)"
            # note. See etl/GAP_ANALYSIS.md for the FEIN-matching caveat on
            # this specific endpoint.
            return f"/entities/Subject/items/{id}/childEntities/Subject_SubjectWages"

        @staticmethod
        def assets(id: str) -> str:
            # :Asset (Section 3.1) — "modeled but disabled in the reasoner".
            # Path registered for completeness / future use; etl/graph_sync.py
            # does not call it yet (see GAP_ANALYSIS.md, Section 3.2 has no
            # defined Subject-to-Asset relationship type to write into).
            return f"/entities/Subject/items/{id}/childEntities/Subject_Asset"

    class Allegations:

        @staticmethod
        def case_allegations_by_type_id(type_id: str) -> str:
            return f"/entities/Allegations/lists/Allegations_All?Allegations_AllegationsType$Identity.Id={type_id}"

        @staticmethod
        def by_workfolder(workfolder_id: str) -> str:
            # Single-call list endpoint: one round trip returns Allegation +
            # AllegationType + Agency embedded per row (Allegations_AllegationsType$Properties,
            # Allegations_Source$Properties) — replaces the old 3-call chase
            # (Allegation item -> AllegationType item -> Agency item) that
            # map_allegations() used to do per allegation.
            return f"/entities/Allegations/lists/Allegations_All?Allegations_Workfolder$Identity.Id={workfolder_id}"

        @staticmethod
        def allegation_type_manage() -> str:
            return "/entities/AllegationType/lists/AllegationType_ManageAllegationType"

    class Subjects:
        """
        List endpoint (plural 'Subjects', not the item-level 'Subject' class
        above) — one call returns the Subjects bridge row + the Subject
        detail record + the SubjectRole name embedded per row
        (Subjects_Subject$Properties, Subjects_SubjectRoleRelationship$Properties).
        Replaces the old 3-call chase in case_intake._parse_subjects
        (Subjects item -> Subject detail item -> SubjectRole item).
        """
        @staticmethod
        def by_workfolder(workfolder_id: str) -> str:
            return f"/entities/Subjects/lists/All_Subjects?Subjects_Workfolder$Identity.Id={workfolder_id}"

    class AddressList:
        """
        List endpoint for addresses — one call returns Address + AddressType
        + StateCityZip embedded per row (Address_AddressType_Relation$Properties,
        Address_StateCityZip_Relation$Properties). Replaces the old 3-call
        chase per address (Address item -> AddressType item -> StateCityZip item).
        Named AddressList to avoid colliding with Subject.addresses(), which
        still returns the relationship href form used elsewhere.
        """
        @staticmethod
        def by_subject(subject_id: str) -> str:
            return f"/entities/Address/lists/Address_All?Address_Subject$Identity.Id={subject_id}"

    class FinancialList:
        """
        List endpoint — one call returns Financial + the per-record primary
        fraud type embedded (Financial_PrimaryFraudTypeRelationShip$Properties/
        $Identity). Replaces the old 2-call chase per financial record
        (Financial item -> FraudTypeClassification item).
        """
        @staticmethod
        def by_workfolder(workfolder_id: str) -> str:
            return f"/entities/Financial/lists/Financial_All?Financial_WorkfolderRelationship$Identity.Id={workfolder_id}"

    class CommentaryList:
        """
        List endpoint — one call returns WorkfolderCommentary + CommentaryType
        embedded per row (WorkfolderCommentary_CommentaryTypeRelationship$Properties).
        Replaces the old 2-call chase per comment (Commentary item -> CommentaryType item).
        """
        @staticmethod
        def by_workfolder(workfolder_id: str) -> str:
            return (
                "/entities/WorkfolderCommentary/lists/WorkfolderCommentary_All"
                f"?WorkfolderCommentary_WorkfolderRelationship$Identity.Id={workfolder_id}"
            )