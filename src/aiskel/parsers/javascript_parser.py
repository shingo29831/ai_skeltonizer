# src/aiskel/parsers/javascript_parser.py
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser
from typing import List, Set, Tuple
from .base_parser import BaseParser, RoleEntry, DependencyEntry

class JavascriptParser(BaseParser):
    def __init__(self, is_typescript: bool = False, is_tsx: bool = False):
        if is_tsx:
            self.language = Language(tsts.language_tsx())
        elif is_typescript:
            self.language = Language(tsts.language_typescript())
        else:
            self.language = Language(tsjs.language())
        self.parser = Parser(self.language)

    def _get_node_text(self, node, source_bytes: bytes) -> str:
        return source_bytes[node.start_byte:node.end_byte].decode('utf-8')

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

        def traverse(node, current_class=None):
            if node.type == 'import_statement':
                source_node = node.child_by_field_name('source')
                if source_node:
                    deps.add(self._get_node_text(source_node, source_bytes).strip("'\""))
            
            if node.type == 'class_declaration':
                name_node = node.child_by_field_name('name')
                class_name = self._get_node_text(name_node, source_bytes) if name_node else "AnonymousClass"
                roles.append(RoleEntry(
                    file_path=rel_file_path, element_type="Class", name=class_name,
                    signature=f"class {class_name}", description=self._get_leading_comments(node, source_bytes)
                ))
                for child in node.children:
                    traverse(child, current_class=class_name)
                return

            if node.type in {'function_declaration', 'method_definition', 'arrow_function'}:
                name = "anonymous"
                if node.type in {'function_declaration', 'method_definition'}:
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = self._get_node_text(name_node, source_bytes)
                elif node.type == 'arrow_function':
                    parent = node.parent
                    if parent and parent.type == 'variable_declarator':
                        name_node = parent.child_by_field_name('name')
                        if name_node:
                            name = self._get_node_text(name_node, source_bytes)

                full_name = f"{current_class}.{name}" if current_class else name
                roles.append(RoleEntry(
                    file_path=rel_file_path, element_type="Method" if current_class else "Function",
                    name=full_name, signature=f"function {name}(...)",
                    description=self._get_leading_comments(node, source_bytes)
                ))

                if full_name not in keep_functions and name not in keep_functions:
                    body_node = node.child_by_field_name('body')
                    if body_node and body_node.type == 'statement_block':
                        replacements.append((body_node.start_byte + 1, body_node.end_byte - 1, "\n  /* ... */\n"))

            for child in node.children:
                traverse(child, current_class)

        traverse(tree.root_node)

        for start, end, text in sorted(replacements, key=lambda x: x[0], reverse=True):
            source_bytes = source_bytes[:start] + text.encode('utf-8') + source_bytes[end:]

        return source_bytes.decode('utf-8'), roles, DependencyEntry(file_path=rel_file_path, imported_modules=sorted(list(deps)))