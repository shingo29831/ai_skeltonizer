# src/aiskel/parsers/cpp_parser.py
import tree_sitter_cpp as tscpp
import tree_sitter_c as tsc
from tree_sitter import Language, Parser
from typing import List, Set, Tuple
from .base_parser import BaseParser, RoleEntry, DependencyEntry

class CppParser(BaseParser):
    def __init__(self, is_c: bool = False):
        if is_c:
            self.language = Language(tsc.language())
        else:
            self.language = Language(tscpp.language())
        self.parser = Parser(self.language)

    def _get_node_text(self, node, source_bytes: bytes) -> str:
        return source_bytes[node.start_byte:node.end_byte].decode('utf-8', errors='replace')

    def _get_leading_comments(self, node, source_bytes: bytes) -> str:
        prev = node.prev_sibling
        comments = []
        while prev and prev.type == 'comment':
            comments.insert(0, self._get_node_text(prev, source_bytes).strip())
            prev = prev.prev_sibling
        return " / ".join(comments) if comments else ""

    def parse_and_process(
        self, source_code: str, rel_file_path: str, keep_functions: Set[str], only_nodes: Set[str]
    ) -> Tuple[str, List[RoleEntry], DependencyEntry]:
        
        source_bytes = source_code.encode('utf-8')
        tree = self.parser.parse(source_bytes)
        
        roles: List[RoleEntry] = []
        deps: Set[str] = set()
        replacements = []

        def get_function_name(node) -> str:
            declarator = node.child_by_field_name('declarator')
            if not declarator:
                return "anonymous"
            
            curr = declarator
            # ポインタや参照の宣言子を剥がし、最も内側の識別子を取得する
            while curr:
                if curr.type in {'identifier', 'field_identifier', 'destructor_name', 'operator_name'}:
                    return self._get_node_text(curr, source_bytes)
                elif curr.type == 'scoped_identifier':
                    return self._get_node_text(curr, source_bytes)
                elif curr.type == 'function_declarator':
                    curr = curr.child_by_field_name('declarator')
                elif curr.type in {'pointer_declarator', 'reference_declarator'}:
                    curr = curr.child_by_field_name('declarator')
                else:
                    break
            return self._get_node_text(declarator, source_bytes)

        def traverse(node, current_class=None):
            if node.type == 'preproc_include':
                path_node = node.child_by_field_name('path')
                if path_node:
                    deps.add(self._get_node_text(path_node, source_bytes).strip('<>"'))
            
            if node.type in {'class_specifier', 'struct_specifier'}:
                name_node = node.child_by_field_name('name')
                class_name = self._get_node_text(name_node, source_bytes) if name_node else "Anonymous"
                roles.append(RoleEntry(
                    file_path=rel_file_path, 
                    element_type="Class" if node.type == 'class_specifier' else "Struct", 
                    name=class_name,
                    signature=f"{'class' if node.type == 'class_specifier' else 'struct'} {class_name}", 
                    description=self._get_leading_comments(node, source_bytes)
                ))
                for child in node.children:
                    traverse(child, current_class=class_name)
                return

            if node.type == 'function_definition':
                name = get_function_name(node)
                is_method = current_class is not None or '::' in name
                
                roles.append(RoleEntry(
                    file_path=rel_file_path, 
                    element_type="Method" if is_method else "Function",
                    name=name, 
                    signature=f"{name}(...)",
                    description=self._get_leading_comments(node, source_bytes)
                ))

                base_name = name.split('::')[-1] if '::' in name else name
                if name not in keep_functions and base_name not in keep_functions:
                    body_node = node.child_by_field_name('body')
                    if body_node and body_node.type == 'compound_statement':
                        replacements.append((body_node.start_byte, body_node.end_byte, "{ /* ... */ }"))
                
                # 関数内部のローカルクラスなどはスケルトン化で消滅するためトラバースしない
                return

            for child in node.children:
                traverse(child, current_class)

        traverse(tree.root_node)

        # バイト位置の後ろから順に置換することでインデックスのズレを防ぐ
        for start, end, text in sorted(replacements, key=lambda x: x[0], reverse=True):
            source_bytes = source_bytes[:start] + text.encode('utf-8') + source_bytes[end:]

        return source_bytes.decode('utf-8', errors='replace'), roles, DependencyEntry(file_path=rel_file_path, imported_modules=sorted(list(deps)))