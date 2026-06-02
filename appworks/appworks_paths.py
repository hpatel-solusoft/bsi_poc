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

    class Allegations:

        @staticmethod
        def allegations_by_type(type_id: str) -> str:
            return f"/entities/Allegations/lists/Allegations_All?Allegations_AllegationsType$Identity.Id={type_id}"

        @staticmethod
        def allegation_type_all() -> str:
            return "/entities/AllegationType/lists/AllegationType_All"

        @staticmethod
        def allegation_type_manage() -> str:
            return "/entities/AllegationType/lists/AllegationType_ManageAllegationType"