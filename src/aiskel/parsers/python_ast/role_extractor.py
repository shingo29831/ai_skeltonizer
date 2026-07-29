# src/py_skeletonizer/role_extractor.py
"""
Role: PythonのASTノードやソースコードからクラス・関数の役割説明(Docstringおよび直上#コメント)を抽出し、集約レポートを生成する。
"""
import ast
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class RoleEntry:
    file_path: str
    element_type: str  # "Module", "Class", "Function", "Method"
    name: str
    signature: str
    description: str


def _get_leading_comments(source_lines: List[str], start_lineno: int) -> List[str]:
    comments = []
    curr_idx = start_lineno - 2

    while curr_idx >= 0:
        line = source_lines[curr_idx].strip()
        if line.startswith("#"):
            cleaned_comment = line.lstrip("#").strip()
            if cleaned_comment:
                comments.insert(0, cleaned_comment)
            curr_idx -= 1
        elif not line:
            curr_idx -= 1
        else:
            break

    return comments


def _build_description(docstring: Optional[str], leading_comments: List[str]) -> str:
    combined_lines = []

    for comment in leading_comments:
        if comment.startswith("Role:") or comment.startswith("AI:") or comment.startswith("Rule:"):
            combined_lines.append(f"**[{comment[:4].rstrip(':')}]** {comment[5:].strip()}")
        else:
            combined_lines.append(comment)

    if docstring:
        doc_lines = [line.strip() for line in docstring.strip().splitlines() if line.strip()]
        for line in doc_lines:
            if line.startswith("Role:") or line.startswith("AI:") or line.startswith("Rule:"):
                combined_lines.append(f"**[{line[:4].rstrip(':')}]** {line[5:].strip()}")
            else:
                combined_lines.append(line)

    if not combined_lines:
        return ""

    return " / ".join(dict.fromkeys(combined_lines))


def _get_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args_str = ast.unparse(node.args) if hasattr(ast, "unparse") else "..."
        returns_str = f" -> {ast.unparse(node.returns)}" if node.returns and hasattr(ast, "unparse") else ""
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        return f"{prefix}{node.name}({args_str}){returns_str}"
    except Exception:
        return f"def {node.name}(...)"


def extract_roles_from_ast(tree: ast.AST, rel_file_path: str, source_code: str = "") -> List[RoleEntry]:
    entries: List[RoleEntry] = []
    source_lines = source_code.splitlines() if source_code else []

    module_doc = ast.get_docstring(tree)
    module_comments = _get_leading_comments(source_lines, 2) if source_lines else []
    if module_doc or module_comments:
        entries.append(
            RoleEntry(
                file_path=rel_file_path,
                element_type="Module",
                name=rel_file_path,
                signature="",
                description=_build_description(module_doc, module_comments),
            )
        )

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            leading_comments = _get_leading_comments(source_lines, getattr(node, "lineno", 0))
            entries.append(
                RoleEntry(
                    file_path=rel_file_path,
                    element_type="Class",
                    name=node.name,
                    signature=f"class {node.name}",
                    description=_build_description(ast.get_docstring(node), leading_comments),
                )
            )
            for sub_node in node.body:
                if isinstance(sub_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sub_comments = _get_leading_comments(source_lines, getattr(sub_node, "lineno", 0))
                    entries.append(
                        RoleEntry(
                            file_path=rel_file_path,
                            element_type="Method",
                            name=f"{node.name}.{sub_node.name}",
                            signature=_get_function_signature(sub_node),
                            description=_build_description(ast.get_docstring(sub_node), sub_comments),
                        )
                    )

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            leading_comments = _get_leading_comments(source_lines, getattr(node, "lineno", 0))
            entries.append(
                RoleEntry(
                    file_path=rel_file_path,
                    element_type="Function",
                    name=node.name,
                    signature=_get_function_signature(node),
                    description=_build_description(ast.get_docstring(node), leading_comments),
                )
            )

    return entries


def generate_role_map_text(all_entries: List[RoleEntry]) -> str:
    lines = [
        "# AI Context: Project Role & Architecture Map",
        "",
        "この文書は、プロジェクト内に存在する各モジュール、クラス、および関数の責務とシグネチャを一覧化した退避マニュアルです。",
        "",
    ]

    grouped: dict[str, List[RoleEntry]] = {}
    for entry in all_entries:
        grouped.setdefault(entry.file_path, []).append(entry)

    sorted_paths = sorted(grouped.keys())

    for path in sorted_paths:
        file_entries = grouped[path]
        lines.append(f"## 📁 `{path}`")

        module_entries = [e for e in file_entries if e.element_type == "Module"]
        if module_entries:
            lines.append(f"> **Module Role**: {module_entries[0].description}")
        lines.append("")

        other_entries = [e for e in file_entries if e.element_type != "Module"]
        for entry in other_entries:
            icon = "🔷" if entry.element_type == "Class" else ("🔹" if entry.element_type == "Method" else "🔸")
            lines.append(f"- {icon} **{entry.element_type} `{entry.name}`**")
            lines.append(f"  - `Signature`: `{entry.signature}`")
            lines.append(f"  - `Role`: {entry.description}")

        lines.append("")

    return "\n".join(lines)