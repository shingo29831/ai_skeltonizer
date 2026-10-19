"""Module: @role: Tree-sitterを用いたC#ソースコードの構文解析、スケルトン生成、役割メタデータ抽出を担当する。"""

from typing import List, Optional, Set, Tuple
import re

try:
    import tree_sitter_c_sharp as tscs
except ImportError:
    tscs = None

from tree_sitter import Language, Parser
from .base_parser import BaseParser, RoleEntry, DependencyEntry
from .sanitizer import extract_clean_role_description


class CSharpParser(BaseParser):
    def __init__(self) -> None:
        if tscs is None:
            raise ImportError(
                "tree-sitter-c-sharp is required to parse C# files. Please install it via 'pip install tree-sitter-c-sharp'."
            )
        self.language = Language(tscs.language())
        self.parser = Parser(self.language)

    def _get_node_text(self, node, source_bytes: bytes) -> str:
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _clean_comment_text(self, raw_comment: str) -> str:
        cleaned_lines: List[str] = []
        for line in raw_comment.splitlines():
            line = line.strip()
            if line.startswith("///"):
                line = line[3:].strip()
            elif line.startswith("//"):
                line = line[2:].strip()
            elif line.startswith("/*"):
                line = line[2:].strip()
            if line.endswith("*/"):
                line = line[:-2].strip()
            if line.startswith("*"):
                line = line[1:].strip()
            # なぜ必要か: XMLドキュメントコメントタグを除去し要約トークンを最適化
            line = re.sub(r"<[^>]+>", "", line).strip()
            if line:
                cleaned_lines.append(line)
        return extract_clean_role_description(leading_comments=cleaned_lines)

    def _get_leading_comments(self, node, source_bytes: bytes) -> str:
        prev = node.prev_sibling
        comments: List[str] = []
        while prev and prev.type in {"comment", "single_line_comment", "delimited_comment"}:
            comments.insert(0, self._get_node_text(prev, source_bytes).strip())
            prev = prev.prev_sibling
        if not comments:
            return ""
        return self._clean_comment_text("\n".join(comments))

    def parse_and_process(
        self,
        source_code: str,
        rel_file_path: str,
        keep_functions: Set[str],
        only_nodes: Set[str],
    ) -> Tuple[str, List[RoleEntry], DependencyEntry]:
        source_bytes = source_code.encode("utf-8")
        tree = self.parser.parse(source_bytes)

        roles: List[RoleEntry] = []
        all_deps: Set[str] = set()
        internal_deps: Set[str] = set()
        external_deps: Set[str] = set()
        replacements: List[Tuple[int, int, str]] = []

        if tree.root_node.children:
            first_child = tree.root_node.children[0]
            if first_child.type in {"comment", "single_line_comment", "delimited_comment"}:
                mod_desc = self._clean_comment_text(self._get_node_text(first_child, source_bytes))
                if mod_desc:
                    roles.append(
                        RoleEntry(
                            file_path=rel_file_path,
                            element_type="Module",
                            name=rel_file_path,
                            signature="",
                            description=mod_desc,
                        )
                    )

        def get_identifier_name(target_node) -> str:
            name_node = target_node.child_by_field_name("name")
            if name_node:
                return self._get_node_text(name_node, source_bytes)
            for child in target_node.children:
                if child.type == "identifier":
                    return self._get_node_text(child, source_bytes)
            return "Anonymous"

        type_declaration_kinds = {
            "class_declaration": "class",
            "struct_declaration": "struct",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "record_declaration": "record",
            "record_struct_declaration": "record struct",
        }

        func_declaration_kinds = {
            "method_declaration",
            "constructor_declaration",
            "destructor_declaration",
            "operator_declaration",
            "conversion_operator_declaration",
        }

        def traverse(node, current_class: Optional[str] = None) -> None:
            if node.type == "using_directive":
                raw_text = self._get_node_text(node, source_bytes).strip().rstrip(";")
                clean_dep = re.sub(r"^(?:global\s+)?using\s+(?:unsafe\s+)?(?:static\s+)?", "", raw_text).strip()
                if "=" in clean_dep:
                    clean_dep = clean_dep.split("=", 1)[1].strip()
                if clean_dep:
                    all_deps.add(clean_dep)
                    if clean_dep.startswith(("System", "Microsoft")) or "." not in clean_dep:
                        external_deps.add(clean_dep)
                    else:
                        internal_deps.add(clean_dep)
                return

            if node.type in type_declaration_kinds:
                keyword = type_declaration_kinds[node.type]
                class_name = get_identifier_name(node)
                full_class_name = f"{current_class}.{class_name}" if current_class else class_name

                type_params_node = node.child_by_field_name("type_parameters")
                type_params = self._get_node_text(type_params_node, source_bytes) if type_params_node else ""

                bases_node = node.child_by_field_name("bases")
                bases_text = f" : {self._get_node_text(bases_node, source_bytes)}" if bases_node else ""

                signature = f"{keyword} {class_name}{type_params}{bases_text}".strip()

                fields: List[str] = []
                enum_members: List[str] = []
                body_node = node.child_by_field_name("body")

                if body_node:
                    for member in body_node.children:
                        if member.type == "property_declaration":
                            p_type = member.child_by_field_name("type")
                            p_name = member.child_by_field_name("name")
                            if p_type and p_name:
                                p_name_str = self._get_node_text(p_name, source_bytes)
                                if not p_name_str.startswith("_"):
                                    fields.append(f"{self._get_node_text(p_type, source_bytes)} {p_name_str}")
                        elif member.type == "enum_member_declaration":
                            m_name = member.child_by_field_name("name")
                            if m_name:
                                enum_members.append(self._get_node_text(m_name, source_bytes))

                roles.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type="Class",
                        name=full_class_name,
                        signature=signature,
                        description=self._get_leading_comments(node, source_bytes),
                        fields=fields if fields else None,
                        enum_members=enum_members if enum_members else None,
                    )
                )

                if body_node:
                    for child in body_node.children:
                        traverse(child, current_class=full_class_name)
                return

            if node.type in func_declaration_kinds:
                name = get_identifier_name(node)
                if node.type == "destructor_declaration":
                    name = f"~{name}"
                full_name = f"{current_class}.{name}" if current_class else name
                is_method = current_class is not None

                type_node = node.child_by_field_name("type")
                ret_type = self._get_node_text(type_node, source_bytes) if type_node else ""
                params_node = node.child_by_field_name("parameters")
                params_text = self._get_node_text(params_node, source_bytes) if params_node else "()"

                sig_prefix = f"{ret_type} " if ret_type else ""
                signature = f"{sig_prefix}{name}{params_text}"

                roles.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type="Method" if is_method else "Function",
                        name=full_name,
                        signature=signature,
                        description=self._get_leading_comments(node, source_bytes),
                    )
                )

                if name not in keep_functions and full_name not in keep_functions:
                    body_node = node.child_by_field_name("body")
                    if body_node and body_node.type == "block":
                        replacements.append((body_node.start_byte, body_node.end_byte, "{ /* ... */ }"))
                    else:
                        for child in node.children:
                            if child.type == "arrow_expression_clause":
                                replacements.append((child.start_byte, child.end_byte, "=> default!;"))
                                break
                return

            if node.type == "property_declaration":
                for child in node.children:
                    if child.type == "accessor_list":
                        for acc in child.children:
                            if acc.type == "accessor_declaration":
                                acc_body = acc.child_by_field_name("body")
                                if acc_body and acc_body.type == "block":
                                    replacements.append((acc_body.start_byte, acc_body.end_byte, "{ /* ... */ }"))
                                for acc_child in acc.children:
                                    if acc_child.type == "arrow_expression_clause":
                                        replacements.append((acc_child.start_byte, acc_child.end_byte, "=> default!;"))
                    elif child.type == "arrow_expression_clause":
                        replacements.append((child.start_byte, child.end_byte, "=> default!;"))
                return

            for child in node.children:
                traverse(child, current_class)

        traverse(tree.root_node)

        for start, end, text in sorted(replacements, key=lambda x: x[0], reverse=True):
            source_bytes = source_bytes[:start] + text.encode("utf-8") + source_bytes[end:]

        dep_entry = DependencyEntry(
            file_path=rel_file_path,
            imported_modules=sorted(list(all_deps)),
            internal_imports=sorted(list(internal_deps)),
            external_imports=sorted(list(external_deps)),
        )

        return source_bytes.decode("utf-8", errors="replace"), roles, dep_entry
