# src/aiskel/parsers/python_parser.py
from typing import List, Set, Tuple
from .base_parser import BaseParser, RoleEntry, DependencyEntry
from .python_ast.ast_processor import process_code_all_in_one

class PythonParser(BaseParser):
    def parse_and_process(
        self, source_code: str, rel_file_path: str, keep_functions: Set[str], only_nodes: Set[str]
    ) -> Tuple[str, List[RoleEntry], DependencyEntry]:
        
        skeleton_code, ast_roles, ast_dep = process_code_all_in_one(
            source_code, rel_file_path, keep_functions, only_nodes
        )
        
        roles = [
            RoleEntry(
                file_path=r.file_path,
                element_type=r.element_type,
                name=r.name,
                signature=r.signature,
                description=r.description
            ) for r in ast_roles
        ]
        
        dependency = DependencyEntry(
            file_path=ast_dep.file_path,
            imported_modules=ast_dep.imported_modules
        )
        
        return skeleton_code, roles, dependency