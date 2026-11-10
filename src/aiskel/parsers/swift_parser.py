"""Module: @role: Tree-sitterを用いたSwiftソースコードの構文解析、スケルトン生成、役割メタデータ抽出を担当する。"""

from typing import List, Optional, Set, Tuple
import re

try:
    import tree_sitter_swift as tsswift
except ImportError:
    tsswift = None

from tree_sitter import Language, Parser
from .base_parser import BaseParser, RoleEntry, DependencyEntry
from .sanitizer import extract_clean_role_description

SWIFT_SYSTEM_FRAMEWORKS = {
    "Foundation", "SwiftUI", "UIKit", "AppKit", "Combine", "CoreData",
    "CoreGraphics", "CoreLocation", "CoreBluetooth", "Dispatch", "os",
    "XCTest", "AVFoundation", "Metal", "WebKit", "Security", "CryptoKit",
    "Network", "Photos", "UserNotifications", "SwiftData", "Accessibility",
}


class SwiftParser(BaseParser):
    def __init__(self) -> None:
        if tsswift is None:
            raise ImportError(
                "tree-sitter-swift is required to parse Swift files. Please install it via 'pip install tree-sitter-swift'."
            )
        self.language = Language(tsswift.language())
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
        while prev and prev.type in {"comment", "multiline_comment", "doc_comment", "line_comment"}:
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
            if first_child.type in {"comment", "multiline_comment", "doc_comment", "line_comment"}:
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
                if child.type in {"type_identifier", "simple_identifier", "identifier", "custom_type", "user_type"}:
                    return self._get_node_text(child, source_bytes)
            return "Anonymous"

        def find_function_body(func_node):
            body = func_node.child_by_field_name("body")
            if body:
                return body
            for child in func_node.children:
                if child.type in {"function_body", "code_block", "block"}:
                    return child
            # なぜ必要か: シグネチャと本体が分離している文法パターンでのフォールバック取得
            sibling = func_node.next_named_sibling
            if sibling and sibling.type in {"function_body", "code_block", "block"}:
                return sibling
            return None

        type_declaration_kinds = {
            "class_declaration",
            "struct_declaration",
            "enum_declaration",
            "protocol_declaration",
            "extension_declaration",
            "actor_declaration",
        }

        func_declaration_kinds = {
            "function_declaration",
            "init_declaration",
            "deinit_declaration",
            "subscript_declaration",
        }

        def resolve_type_kind(node) -> Tuple[str, str]:
            # なぜ必要か: tree-sitter文法差異(class_declaration集約型 vs 個別型)を網羅判定
            raw_text = self._get_node_text(node, source_bytes)
            node_type = node.type
            if node_type == "protocol_declaration" or "protocol " in raw_text[:30]:
                return "protocol", "Protocol"
            if node_type == "struct_declaration" or "struct " in raw_text[:30]:
                return "struct", "Struct"
            if node_type == "enum_declaration" or "enum " in raw_text[:30]:
                return "enum", "Enum"
            if node_type == "actor_declaration" or "actor " in raw_text[:30]:
                return "actor", "Actor"
            if node_type == "extension_declaration" or "extension " in raw_text[:30]:
                return "extension", "Extension"
            return "class", "Class"

        def traverse(node, current_class: Optional[str] = None) -> None:
            if node.type == "import_declaration":
                raw_text = self._get_node_text(node, source_bytes).strip()
                match = re.search(r"import\s+(?:(?:typealias|struct|class|enum|protocol|let|var|func)\s+)?([a-zA-Z0-9_\.]+)", raw_text)
                if match:
                    mod_name = match.group(1).split(".")[0]
                    all_deps.add(mod_name)
                    if mod_name in SWIFT_SYSTEM_FRAMEWORKS:
                        external_deps.add(mod_name)
                    else:
                        internal_deps.add(mod_name)
                return

            if node.type in type_declaration_kinds:
                keyword, element_type = resolve_type_kind(node)
                type_name = get_identifier_name(node)
                full_type_name = f"{current_class}.{type_name}" if current_class else type_name

                body_node = node.child_by_field_name("body")
                if not body_node:
                    for child in node.children:
                        if child.type in {"class_body", "enum_body", "struct_body", "code_block", "block"}:
                            body_node = child
                            break

                body_start = body_node.start_byte if body_node else node.end_byte
                signature = source_bytes[node.start_byte:body_start].decode("utf-8", errors="replace").strip()
                if not signature:
                    signature = f"{keyword} {type_name}"

                fields: List[str] = []
                enum_members: List[str] = []

                if body_node:
                    for member in body_node.children:
                        if member.type in {"enum_element", "enum_case"}:
                            m_name = get_identifier_name(member)
                            if m_name and m_name != "Anonymous":
                                enum_members.append(m_name)
                        elif member.type in {"property_declaration", "variable_declaration"}:
                            f_name = get_identifier_name(member)
                            if f_name and f_name != "Anonymous" and not f_name.startswith("_"):
                                fields.append(f_name)

                roles.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type=element_type,
                        name=full_type_name,
                        signature=signature,
                        description=self._get_leading_comments(node, source_bytes),
                        fields=fields if fields else None,
                        enum_members=enum_members if enum_members else None,
                    )
                )

                if body_node:
                    for child in body_node.children:
                        traverse(child, current_class=full_type_name)
                return

            if node.type in func_declaration_kinds:
                name = get_identifier_name(node)
                if node.type == "init_declaration":
                    name = "init"
                elif node.type == "deinit_declaration":
                    name = "deinit"

                full_name = f"{current_class}.{name}" if current_class else name
                is_method = current_class is not None

                body_node = find_function_body(node)
                body_start = body_node.start_byte if body_node else node.end_byte
                sig_text = source_bytes[node.start_byte:body_start].decode("utf-8", errors="replace").strip()
                if not sig_text:
                    sig_text = self._get_node_text(node, source_bytes).strip()

                element_type = "Method" if is_method else "Function"
                if node.type == "init_declaration":
                    element_type = "Constructor"
                elif node.type == "deinit_declaration":
                    element_type = "Destructor"
                elif node.type == "subscript_declaration":
                    element_type = "Subscript"

                ui_handlers = None
                if name in {"body", "viewDidLoad", "viewWillAppear", "viewDidAppear", "viewWillDisappear", "viewDidDisappear", "onAppear", "onDisappear", "onChange"} or name.startswith(("onTap", "handle", "didTap")):
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
                    if body_node:
                        replacements.append((body_node.start_byte, body_node.end_byte, "{\n    /* ... */\n}"))
                return

            if node.type in {"function_body", "code_block"}:
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
