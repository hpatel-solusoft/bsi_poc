"""
AppWorks REST path registry.
Single source of truth for every AppWorks endpoint path in the system.
No path string is written in any other file — call these methods instead.

Usage:
    from appworks.appworks_paths import AW
    fetch(AW.Workfolder.item(case_id))
    fetch(AW.Subjects.by_subject(subject_id))
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
        # NOTE: item(), workfolder_mappings(), and workfolder_mapping_item()
        # were removed here. They cost 3 round trips (item -> mapping list ->
        # per-mapping item re-fetch, since childEntities list rows only carry
        # a 'self' href, not the parent Workfolder relationship) just to find
        # which other cases a subject appears in. Subjects.by_subject() below
        # replaces all three with a single call: the same Subjects/lists/
        # All_Subjects endpoint used for case->subjects, filtered by
        # Subjects_Subject$Identity.Id instead of Subjects_Workfolder$Identity.Id,
        # which returns one row per case the subject is on — Subject detail
        # Properties, own Workfolder id, and IsPrimarySubject flag all embedded.


        @staticmethod
        def aliases(id: str) -> str:
            return f"/entities/Subject/items/{id}/childEntities/Subject_Alias"

        @staticmethod
        def jobs(id: str) -> str:
            # Sources EMPLOYED_BY (Section 3.2) — employer_name/fein per employment
            # record. Use the actual AppWorks service path for the Job list
            # endpoint filtered by subject.
            return f"entityRestService/api/OSABSIACM/entities/Job/lists/AllJobs?Job_Subject$Identity.Id={id}"

        @staticmethod
        def wages(id: str) -> str:
            # Sources HAS_WAGE_RECORD_WITH (Section 3.2) — a separate,
            # independent path to Subject-Employer linkage from the Job table,
            # per the reference doc's own "(independent path, better coverage)"
            # note. See etl/GAP_ANALYSIS.md for the FEIN-matching caveat on
            # this specific endpoint.
            return f"/entities/Subject/items/{id}/relationships/Subject_SubjectWages"

        @staticmethod
        def assets(id: str) -> str:
            # :Asset (Section 3.1) — "modeled but disabled in the reasoner".
            # Confirmed against the live AppWorks response: this is a
            # 'relationships' endpoint, not 'childEntities'. Path registered
            # for completeness / future use; etl/graph_sync.py does not call
            # it yet (see GAP_ANALYSIS.md, Section 3.2 has no defined
            # Subject-to-Asset relationship type to write into).
            return f"/entities/Subject/items/{id}/relationships/Subject_Asset"

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

    class AllegationTypeTask:
        """BSI standard investigative task catalogue (AI-16 / Section 8.5)."""

        @staticmethod
        def manage_allegation_type_tasks() -> str:
            # Flat catalogue of every configured BSI task type. Each row
            # carries TaskName, AllegationTypeTask_IsDefaultTask and
            # Show_IN_UI. The list is global — it does not associate tasks
            # with allegation types.
            return "/entities/AllegationTypeTask/lists/AllegationTypeTask_ManageAllegationTypeTasks"

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

        @staticmethod
        def by_subject(subject_id: str) -> str:
            # Same All_Subjects list endpoint as by_workfolder(), filtered the
            # other direction: every Subjects row for this Subject detail id,
            # across every case they're on. Each row embeds its own
            # Subjects_Workfolder$Identity (the case id) and
            # Subjects_IsPrimarySubject — replaces the old Subject.item() +
            # workfolder_mappings() + workfolder_mapping_item() 3-call chase
            # used to discover a subject's prior cases.
            return f"/entities/Subjects/lists/All_Subjects?Subjects_Subject$Identity.Id={subject_id}"

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