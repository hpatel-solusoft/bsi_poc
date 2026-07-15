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
            # return "/entities/AgentRulesTable/lists/AgentRulesTable_AgentRulesTableListInternal"
            return "/OSABSIACM/entities/FraudRiskRules/lists/FraudRiskRules_FraudRiskRulesListInternal"
        
        @staticmethod
        def risk_rules_by_id(id: str) -> str:
            # return f"/entities/AgentRulesTable/items/{id}/childEntities/Rules"
            return f"/entities/FraudRiskRules/items/{id}/childEntities/Rules"
        
    class Workfolder:
        @staticmethod
        def item(id: str) -> str:
            return f"/entities/Workfolder/items/{id}"

        @staticmethod
        def allegations(id: str) -> str:
            return f"/entities/Workfolder/items/{id}/relationships/Workfolder_AllegationsRelationship"
            

        @staticmethod
        def commentary(id: str) -> str:
            return f"/entities/Workfolder/items/{id}/relationships/Workfolder_WorkfolderCommentaryNewRelationship"
        
        @staticmethod
        def financial(id: str) -> str:
            return f"/entities/Workfolder/items/{id}/relationships/Workfolder_FinancialRelationship"
        
        @staticmethod
        def subjects(id: str) -> str:
            return f"/entities/Workfolder/items/{id}/relationships/Workfolder_SubjectsRelationship"

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
        def case_allegations_by_type_id(type_id: str) -> str:   # Updated parameters needs to passed to fetch allegations by type with status and top  
            return f"/entities/Allegations/lists/Allegations_All?Allegations_AllegationsType$Identity.Id={type_id}"
            #return f"http://processsuite-cm.localdomain.com:81/home/BSIDev/app/entityRestService/api/OSABSIACM/entities/Allegations/lists/Allegations_All?$top={top}&Properties.Allegations_AllegationStatus={status}"

        @staticmethod
        def case_allegations_with_fileter(type_id: str, top: int, status: str) -> str:   # Updated parameters needs to passed to fetch allegations by type with status and top  
            # return f"/entities/Allegations/lists/Allegations_All?Allegations_AllegationsType$Identity.Id={type_id}"
            return f"/entities/Allegations/lists/Allegations_All?$Identity.Id={type_id}&top={top}&Properties.Allegations_AllegationStatus={status}"

        @staticmethod
        def allegation_type_all() -> str:
            return "/entities/AllegationType/lists/AllegationType_All"

        @staticmethod
        def allegation_type_manage() -> str:
            return "/entities/AllegationType/lists/AllegationType_ManageAllegationType"