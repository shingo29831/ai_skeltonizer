"""Module: @role: Tree-sitterを用いたDartソースコードの構文解析、スケルトン生成、役割メタデータ抽出を担当する。"""

from typing import List, Optional, Set, Tuple
import re

try:
    import tree_sitter_dart as tsdart
except ImportError:
    tsdart = None

from tree_sitter import Language, Parser
from .base_parser import BaseParser, RoleEntry, DependencyEntry
from .sanitizer import extract_clean_role_description


class DartParser(BaseParser):
    def __init__(self) -> None:
        if tsdart is None:
            raise ImportError(
                "tree-sitter-dart is required to parse Dart files. Please install it via 'pip install tree-sitter-dart'."
            )
        self.language = Language(tsdart.language())
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
            if line:
                cleaned_lines.append(line)
        return extract_clean_role_description(leading_comments=cleaned_lines)

    def _get_leading_comments(self, node, source_bytes: bytes) -> str:
        prev = node.prev_sibling
        comments: List[str] = []
        while prev and prev.type in {"comment", "documentation_comment", "single_line_comment", "delimited_comment"}:
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
            if first_child.type in {"comment", "documentation_comment", "single_line_comment", "delimited_comment"}:
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

        def find_function_body(sig_node):
            # なぜ必要か: Dart文法では関数シグネチャと本体が兄弟ノードとして配置されるため
            sibling = sig_node.next_named_sibling
            if sibling and sibling.type == "function_body":
                return sibling
            parent = sig_node.parent
            if parent and parent.type in {"class_member_definition", "declaration"}:
                sibling = parent.next_named_sibling
                if sibling and sibling.type == "function_body":
                    return sibling
                for child in parent.children:
                    if child.type == "function_body":
                        return child
            body = sig_node.child_by_field_name("body")
            if body:
                return body
            for child in sig_node.children:
                if child.type in {"function_body", "block"}:
                    return child
            return None

        type_declaration_kinds = {
            "class_definition": "class",
            "class_declaration": "class",
            "mixin_declaration": "mixin",
            "extension_declaration": "extension",
            "enum_declaration": "enum",
        }

        func_declaration_kinds = {
            "function_signature",
            "method_signature",
            "getter_signature",
            "setter_signature",
            "constructor_signature",
            "factory_constructor_signature",
            "operator_signature",
        }

        def traverse(node, current_class: Optional[str] = None) -> None:
            if node.type in {
                "import_or_export",
                "library_import",
                "library_export",
                "part_directive",
                "part_of_directive",
            }:
                raw_text = self._get_node_text(node, source_bytes).strip()
                match = re.search(r"['\"]([^'\"]+)['\"]", raw_text)
                if match:
                    uri = match.group(1).strip()
                    all_deps.add(uri)
                    if uri.startswith("dart:") or (uri.startswith("package:") and not uri.startswith("package:src/")):
                        external_deps.add(uri)
                    else:
                        internal_deps.add(uri)
                return

            if node.type in type_declaration_kinds:
                keyword = type_declaration_kinds[node.type]
                class_name = get_identifier_name(node)
                full_class_name = f"{current_class}.{class_name}" if current_class else class_name

                body_node = node.child_by_field_name("body")
                if not body_node:
                    for child in node.children:
                        if child.type in {"class_body", "enum_body", "extension_body"}:
                            body_node = child
                            break

                body_start = body_node.start_byte if body_node else node.end_byte
                signature = source_bytes[node.start_byte:body_start].decode("utf-8", errors="replace").strip()
                if not signature:
                    signature = f"{keyword} {class_name}"

                fields: List[str] = []
                enum_members: List[str] = []

                if body_node:
                    for member in body_node.children:
                        if member.type == "enum_constant":
                            m_name = get_identifier_name(member)
                            if m_name and m_name != "Anonymous":
                                enum_members.append(m_name)
                        elif member.type in {"declaration", "initialized_variable_definition", "static_final_declaration"}:
                            f_name = get_identifier_name(member)
                            if f_name and f_name != "Anonymous" and not f_name.startswith("_"):
                                fields.append(f_name)

                roles.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type="Class" if keyword != "enum" else "Enum",
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
                full_name = f"{current_class}.{name}" if current_class else name
                is_method = current_class is not None

                sig_text = self._get_node_text(node, source_bytes).strip()

                element_type = "Method" if is_method else "Function"
                if node.type in {"constructor_signature", "factory_constructor_signature"}:
                    element_type = "Constructor"
                elif node.type in {"getter_signature", "setter_signature"}:
                    element_type = "Property"

                ui_handlers = None
                if name in {"build", "initState", "dispose", "didUpdateWidget", "didChangeDependencies"} or name.startswith(("on", "handle")):
                    ui_handlers = [name]

                roles.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type=element_type,
                        name=full_name,
                        signature=sig_text,
                        description=self._get_leading_comments(node, source_bytes),
                        ui_handlers=ui_handlers,
                    )
                )

                if name not in keep_functions and full_name not in keep_functions:
                    body_node = find_function_body(node)
                    if body_node:
                        body_text = self._get_node_text(body_node, source_bytes).strip()
                        if body_text.startswith("=>"):
                            replacements.append((body_node.start_byte, body_node.end_byte, "=> throw UnimplementedError();"))
                        else:
                            replacements.append((body_node.start_byte, body_node.end_byte, "{\n    /* ... */\n  }"))
                return

            if node.type == "function_body":
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
