import ast
import re
from typing import List, Optional

from aiskel.parsers.base_parser import RoleEntry, generate_role_map_text
from aiskel.parsers.sanitizer import extract_clean_role_description


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


def _get_base_names(node: ast.ClassDef) -> List[str]:
    bases = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            bases.append(b.id)
        elif isinstance(b, ast.Attribute):
            bases.append(b.attr)
        else:
            try:
                bases.append(ast.unparse(b))
            except Exception:
                pass
    return bases


def _format_constant_value(val_node: ast.AST) -> str:
    try:
        val_str = ast.unparse(val_node) if hasattr(ast, "unparse") else "..."
        if len(val_str) > 25:
            return val_str[:22] + "..."
        return val_str
    except Exception:
        return "..."


def extract_roles_from_ast(tree: ast.AST, rel_file_path: str, source_code: str = "") -> List[RoleEntry]:
    entries: List[RoleEntry] = []
    source_lines = source_code.splitlines() if source_code else []

    module_doc = ast.get_docstring(tree)
    module_comments = _get_leading_comments(source_lines, 2) if source_lines else []
    has_explicit_module_desc = bool(module_doc or module_comments)
    if has_explicit_module_desc:
        entries.append(
            RoleEntry(
                file_path=rel_file_path,
                element_type="Module",
                name=rel_file_path,
                signature="",
                description=_build_description(module_doc, module_comments, rel_file_path),
            )
        )

    # なぜ必要か: UPPER_SNAKE_CASEの主要定数を1行に圧縮し、マジックナンバーや設定値の誤認を防止
    constants: List[str] = []
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper() and not target.id.startswith("_"):
                    constants.append(f"{target.id} = {_format_constant_value(node.value)}")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id.isupper() and not node.target.id.startswith("_"):
                val_str = f" = {_format_constant_value(node.value)}" if node.value else ""
                constants.append(f"{node.target.id}{val_str}")

    if constants:
        entries.append(
            RoleEntry(
                file_path=rel_file_path,
                element_type="Constant",
                name="constants",
                signature=f"constants: {', '.join(constants[:6])}" + (", ..." if len(constants) > 6 else ""),
                description="",
            )
        )

    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef):
            leading_comments = _get_leading_comments(source_lines, getattr(node, "lineno", 0))
            base_names = _get_base_names(node)
            is_enum = any("Enum" in b or "Flag" in b for b in base_names)
            has_methods = any(isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) for sub in node.body)
            is_pydantic = any("BaseModel" in b for b in base_names)
            is_dataclass = any(
                (isinstance(d, ast.Name) and d.id == "dataclass")
                or (isinstance(d, ast.Attribute) and d.attr == "dataclass")
                or (isinstance(d, ast.Call) and getattr(d.func, "id", "") == "dataclass")
                for d in node.decorator_list
            )

            # なぜ必要か: Enumクラスの列挙メンバーを1行に集約し、AIによる文字列リテラルのハルシネーションを防止
            if is_enum:
                enum_members = []
                for sub in node.body:
                    if isinstance(sub, ast.Assign):
                        for target in sub.targets:
                            if isinstance(target, ast.Name) and not target.id.startswith("_"):
                                enum_members.append(target.id)
                    elif isinstance(sub, ast.AnnAssign):
                        if isinstance(sub.target, ast.Name) and not sub.target.id.startswith("_"):
                            enum_members.append(sub.target.id)
                base_name = next((b for b in base_names if "Enum" in b or "Flag" in b), "Enum")
                members_str = f": {', '.join(enum_members)}" if enum_members else ""
                entries.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type="Class",
                        name=node.name,
                        signature=f"class {node.name}({base_name}){members_str}",
                        description=_build_description(ast.get_docstring(node), leading_comments, node.name),
                        enum_members=enum_members,
                    )
                )
            else:
                # なぜ必要か: メソッドのない型定義やPydantic/Dataclassの属性型を抽出し、プロパティ参照エラーを完全抑止
                extracted_fields: List[str] = []
                if (not has_methods) or is_pydantic or is_dataclass:
                    for sub in node.body:
                        if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                            if not sub.target.id.startswith("_"):
                                try:
                                    ann_str = ast.unparse(sub.annotation) if hasattr(ast, "unparse") else "Any"
                                except Exception:
                                    ann_str = "Any"
                                extracted_fields.append(f"{sub.target.id}: {ann_str}")

                entries.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type="Class",
                        name=node.name,
                        signature=f"class {node.name}:",
                        description=_build_description(ast.get_docstring(node), leading_comments, node.name),
                        fields=extracted_fields if extracted_fields else None,
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

    # なぜ必要か: docstring無記載のファイルでも主要クラスまたは公開関数から責務を推測可能にするフォールバック
    if not has_explicit_module_desc:
        fallback_desc = ""
        file_stem = rel_file_path.split("/")[-1].replace(".py", "")
        clean_stem = re.sub(r"[_\-]", "", file_stem).lower()

        classes = [e for e in entries if e.element_type == "Class"]
        matched_class = next(
            (c for c in classes if re.sub(r"[_\-]", "", c.name).lower() == clean_stem and c.description and c.description != "(役割記述なし)"),
            None
        )
        if not matched_class and classes:
            matched_class = next((c for c in classes if c.description and c.description != "(役割記述なし)"), None)

        if matched_class and matched_class.description:
            fallback_desc = f"(Auto) {matched_class.description}"
        else:
            public_funcs = [e.name for e in entries if e.element_type == "Function"]
            if public_funcs:
                funcs_summary = ", ".join(public_funcs[:4]) + (", ..." if len(public_funcs) > 4 else "")
                fallback_desc = f"(Auto) 主要インターフェース: {funcs_summary}"
            elif classes:
                classes_summary = ", ".join(c.name for c in classes[:3])
                fallback_desc = f"(Auto) 提供クラス: {classes_summary}"

        if fallback_desc:
            entries.insert(
                0,
                RoleEntry(
                    file_path=rel_file_path,
                    element_type="Module",
                    name=rel_file_path,
                    signature="",
                    description=fallback_desc,
                )
            )

    return entries
