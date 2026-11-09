"""Module: @role: Tree-sitterを用いたJavaソースコードの構文解析、スケルトン生成、役割メタデータ抽出を担当する。"""

from typing import List, Optional, Set, Tuple
import re

try:
    import tree_sitter_java as tsjava
except ImportError:
    tsjava = None

from tree_sitter import Language, Parser
from .base_parser import BaseParser, RoleEntry, DependencyEntry
from .sanitizer import extract_clean_role_description


class JavaParser(BaseParser):
    def __init__(self) -> None:
        if tsjava is None:
            raise ImportError(
                "tree-sitter-java is required to parse Java files. Please install it via 'pip install tree-sitter-java'."
            )
        self.language = Language(tsjava.language())
        self.parser = Parser(self.language)

    def _get_node_text(self, node, source_bytes: bytes) -> str:
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _clean_comment_text(self, raw_comment: str) -> str:
        cleaned_lines: List[str] = []
        for line in raw_comment.splitlines():
            line = line.strip()
            if line.startswith("//"):
                line = line[2:].strip()
            elif line.startswith("/*"):
                line = line[2:].strip()
            if line.endswith("*/"):
                line = line[:-2].strip()
            if line.startswith("*"):
                line = line[1:].strip()
            # なぜ必要か: Javadocタグ(@param等)やHTMLタグを除去し要約トークンを最適化
            line = re.sub(r"<[^>]+>", "", line).strip()
            if line.startswith("@"):
                continue
            if line:
                cleaned_lines.append(line)
        return extract_clean_role_description(leading_comments=cleaned_lines)

    def _get_leading_comments(self, node, source_bytes: bytes) -> str:
        prev = node.prev_sibling
        comments: List[str] = []
        while prev and prev.type in {"comment", "line_comment", "block_comment"}:
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

        current_package: str = ""

        # ファイル先頭コメントからモジュール説明文を抽出
        if tree.root_node.children:
            first_child = tree.root_node.children[0]
            if first_child.type in {"comment", "line_comment", "block_comment"}:
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

        # パッケージ宣言の抽出
        for child in tree.root_node.children:
            if child.type == "package_declaration":
                raw_pkg = self._get_node_text(child, source_bytes).strip().rstrip(";")
                current_package = re.sub(r"^package\s+", "", raw_pkg).strip()
                break

        pkg_prefix = ""
        if current_package:
            parts = current_package.split(".")
            pkg_prefix = ".".join(parts[:2]) if len(parts) >= 2 else parts[0]

        def get_identifier_name(target_node) -> str:
            name_node = target_node.child_by_field_name("name")
            if name_node:
                return self._get_node_text(name_node, source_bytes)
            for ch in target_node.children:
                if ch.type == "identifier":
                    return self._get_node_text(ch, source_bytes)
            return "Anonymous"

        type_declaration_kinds = {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "record_declaration": "record",
            "annotation_type_declaration": "@interface",
        }

        func_declaration_kinds = {
            "method_declaration",
            "constructor_declaration",
            "compact_constructor_declaration",
        }

        def traverse(node, current_class: Optional[str] = None) -> None:
            if node.type == "import_declaration":
                raw_text = self._get_node_text(node, source_bytes).strip().rstrip(";")
                clean_dep = re.sub(r"^import\s+(?:static\s+)?", "", raw_text).strip()
                if clean_dep:
                    all_deps.add(clean_dep)
                    # なぜ必要か: パッケージプレフィックスに基づく正確な内部/外部依存分離
                    if pkg_prefix and clean_dep.startswith(pkg_prefix):
                        internal_deps.add(clean_dep)
                    elif clean_dep.startswith(("java.", "javax.", "jakarta.", "sun.", "com.sun.", "org.w3c.", "org.xml.")):
                        external_deps.add(clean_dep)
                    elif "." in clean_dep:
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

                superclass_node = node.child_by_field_name("superclass")
                superclass_text = f" {self._get_node_text(superclass_node, source_bytes)}" if superclass_node else ""

                interfaces_node = node.child_by_field_name("interfaces")
                if not interfaces_node:
                    for ch in node.children:
                        if ch.type in {"super_interfaces", "extends_interfaces"}:
                            interfaces_node = ch
                            break
                interfaces_text = f" {self._get_node_text(interfaces_node, source_bytes)}" if interfaces_node else ""

                signature = f"{keyword} {class_name}{type_params}{superclass_text}{interfaces_text}".strip()

                fields: List[str] = []
                enum_members: List[str] = []
                body_node = node.child_by_field_name("body")

                if node.type == "record_declaration":
                    params_node = node.child_by_field_name("parameters")
                    if params_node:
                        for param in params_node.children:
                            if param.type == "formal_parameter":
                                p_type = param.child_by_field_name("type")
                                p_name = param.child_by_field_name("name")
                                if p_type and p_name:
                                    fields.append(f"{self._get_node_text(p_type, source_bytes)} {self._get_node_text(p_name, source_bytes)}")

                if body_node:
                    for member in body_node.children:
                        if member.type == "field_declaration":
                            f_type = member.child_by_field_name("type")
                            if f_type:
                                f_type_str = self._get_node_text(f_type, source_bytes)
                                for sub in member.children:
                                    if sub.type == "variable_declarator":
                                        v_name = sub.child_by_field_name("name")
                                        if v_name:
                                            v_name_str = self._get_node_text(v_name, source_bytes)
                                            if not v_name_str.startswith("_"):
                                                fields.append(f"{f_type_str} {v_name_str}")
                        elif member.type == "enum_constant":
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
                full_name = f"{current_class}.{name}" if current_class else name
                is_method = current_class is not None

                type_node = node.child_by_field_name("type")
                ret_type = self._get_node_text(type_node, source_bytes) if type_node else ""
                params_node = node.child_by_field_name("parameters")
                params_text = self._get_node_text(params_node, source_bytes) if params_node else "()"

                sig_prefix = f"{ret_type} " if ret_type else ""
                signature = f"{sig_prefix}{name}{params_text}"

                # throws節から例外型一覧を抽出
                raises: List[str] = []
                for ch in node.children:
                    if ch.type == "throws":
                        clean_throws = re.sub(r"^throws\s+", "", self._get_node_text(ch, source_bytes)).strip()
                        for exc in clean_throws.split(","):
                            exc_clean = exc.strip()
                            if exc_clean:
                                raises.append(exc_clean)
                        break

                roles.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type="Method" if is_method else "Function",
                        name=full_name,
                        signature=signature,
                        description=self._get_leading_comments(node, source_bytes),
                        raises=raises if raises else None,
                    )
                )

                if name not in keep_functions and full_name not in keep_functions:
                    body_node = node.child_by_field_name("body")
                    if body_node and body_node.type in {"block", "constructor_body"}:
                        replacements.append((body_node.start_byte, body_node.end_byte, "{ /* ... */ }"))
                return

            if node.type == "static_initializer":
                for ch in node.children:
                    if ch.type == "block":
                        replacements.append((ch.start_byte, ch.end_byte, "{ /* ... */ }"))
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
