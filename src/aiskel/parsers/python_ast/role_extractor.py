import ast
from dataclasses import dataclass
from typing import List, Optional

from aiskel.parsers.sanitizer import extract_clean_role_description


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


# なぜ必要か: 装飾線やライセンス条項を排除した単一の自然文サマリーを抽出するためにsanitizerモジュールへ統合
def _build_description(docstring: Optional[str], leading_comments: List[str], entity_name: str = "") -> str:
    return extract_clean_role_description(
        docstring=docstring,
        leading_comments=leading_comments,
        entity_name=entity_name,
    )


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
                description=_build_description(module_doc, module_comments, rel_file_path),
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
                    signature=f"class {node.name}:",
                    description=_build_description(ast.get_docstring(node), leading_comments, node.name),
                )
            )
            for sub_node in node.body:
                if isinstance(sub_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # なぜ必要か: 特殊メソッド（__init__等）や内部メソッド（_始まり）を除外して公開APIに絞りトークンを節約
                    if sub_node.name.startswith("_"):
                        continue
                    sub_comments = _get_leading_comments(source_lines, getattr(sub_node, "lineno", 0))
                    entries.append(
                        RoleEntry(
                            file_path=rel_file_path,
                            element_type="Method",
                            name=f"{node.name}.{sub_node.name}",
                            signature=_get_function_signature(sub_node),
                            description=_build_description(ast.get_docstring(sub_node), sub_comments, sub_node.name),
                        )
                    )

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # なぜ必要か: 内部ヘルパー関数（_始まり）を除外してモジュール公開インターフェースに限定
            if node.name.startswith("_"):
                continue
            leading_comments = _get_leading_comments(source_lines, getattr(node, "lineno", 0))
            entries.append(
                RoleEntry(
                    file_path=rel_file_path,
                    element_type="Function",
                    name=node.name,
                    signature=_get_function_signature(node),
                    description=_build_description(ast.get_docstring(node), leading_comments, node.name),
                )
            )

    return entries


# なぜ必要か: 二重表記（Class Foo: class Foo）を廃止し、クラス下にメソッドをインデント配置して構造を明瞭化
def generate_role_map_text(all_entries: List[RoleEntry]) -> str:
    lines = ["# Role Map"]
    grouped: dict[str, List[RoleEntry]] = {}
    for entry in all_entries:
        grouped.setdefault(entry.file_path, []).append(entry)

    for path in sorted(grouped.keys()):
        file_entries = grouped[path]
        lines.append(f"[{path}]")

        module_entries = [e for e in file_entries if e.element_type == "Module"]
        if module_entries and module_entries[0].description and module_entries[0].description != "(役割記述なし)":
            lines.append(f"Module: {module_entries[0].description}")

        classes: dict[str, List[RoleEntry]] = {}
        standalone_funcs: List[RoleEntry] = []

        for entry in file_entries:
            if entry.element_type == "Module":
                continue
            name_parts = entry.name.split(".")
            base_func_name = name_parts[-1]
            if base_func_name.startswith("_"):
                continue

            if entry.element_type == "Class":
                classes.setdefault(entry.name, [])
            elif entry.element_type == "Method":
                class_name = name_parts[0] if len(name_parts) > 1 else ""
                classes.setdefault(class_name, []).append(entry)
            elif entry.element_type == "Function":
                standalone_funcs.append(entry)

        seen_classes = set()
        for entry in file_entries:
            if entry.element_type == "Class":
                class_name = entry.name
                seen_classes.add(class_name)
                class_desc = f" - {entry.description}" if entry.description and entry.description != "(役割記述なし)" else ""
                lines.append(f"class {class_name}:{class_desc}")
                for method in classes.get(class_name, []):
                    desc = f"\n    {method.description}" if method.description and method.description != "(役割記述なし)" else ""
                    lines.append(f"  {method.signature}{desc}")

        for class_name, methods in classes.items():
            if class_name and class_name not in seen_classes:
                lines.append(f"class {class_name}:")
                for method in methods:
                    desc = f"\n    {method.description}" if method.description and method.description != "(役割記述なし)" else ""
                    lines.append(f"  {method.signature}{desc}")

        for func in standalone_funcs:
            desc = f"\n  {func.description}" if func.description and func.description != "(役割記述なし)" else ""
            lines.append(f"{func.signature}{desc}")

    return "\n".join(lines)
