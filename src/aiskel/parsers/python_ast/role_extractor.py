"""Module: @role: Python ASTからモジュール・クラス・関数・定数およびUIハンドラの役割メタデータを抽出する。"""

import ast
import re
from typing import List, Optional

from aiskel.parsers.base_parser import RoleEntry
from aiskel.parsers.sanitizer import extract_clean_role_description

# なぜ必要か: Qtの基底イベントハンドラを一般メソッドから分離・集約しAIの見落とし防止とトークン節約を両立 (要件1-1 方針A)
QT_UI_EVENT_HANDLERS = {
    # マウス系
    "mousePressEvent",
    "mouseReleaseEvent",
    "mouseDoubleClickEvent",
    "mouseMoveEvent",
    "wheelEvent",
    # キーボード系
    "keyPressEvent",
    "keyReleaseEvent",
    # 描画・ジオメトリ・ライフサイクル
    "paintEvent",
    "resizeEvent",
    "moveEvent",
    "showEvent",
    "hideEvent",
    "closeEvent",
    "changeEvent",
    # フォーカス・カーソル系
    "focusInEvent",
    "focusOutEvent",
    "enterEvent",
    "leaveEvent",
    "inputMethodEvent",
    # ドラッグ＆ドロップ系
    "dragEnterEvent",
    "dragMoveEvent",
    "dragLeaveEvent",
    "dropEvent",
    # その他UIイベント
    "contextMenuEvent",
    "timerEvent",
    "actionEvent",
    "tabletEvent",
}


# なぜ必要か: 関数内で明示的に送出される例外を抽出し、AIによる不適切なエラー握り潰しや未処理例外を抑止
def _extract_raises_from_node(node: ast.FunctionDef | ast.AsyncFunctionDef) -> List[str]:
    raises = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Raise) and sub.exc:
            if isinstance(sub.exc, ast.Name):
                raises.add(sub.exc.id)
            elif isinstance(sub.exc, ast.Call):
                if isinstance(sub.exc.func, ast.Name):
                    raises.add(sub.exc.func.id)
                elif isinstance(sub.exc.func, ast.Attribute):
                    raises.add(sub.exc.func.attr)
    return sorted(list(raises))


# なぜ必要か: 先頭の空行やshebangをスキップし、ファイル冒頭のモジュール責務コメントを確実に抽出
def _get_module_header_comments(source_lines: List[str]) -> List[str]:
    comments = []
    for line in source_lines:
        s = line.strip()
        if not s or s.startswith("#!") or s.startswith("# -*-"):
            continue
        if s.startswith("#"):
            cleaned = s.lstrip("#").strip()
            if cleaned:
                comments.append(cleaned)
        else:
            break
    return comments


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


def _infer_type_str(val_node: Optional[ast.AST]) -> str:
    if val_node is None:
        return "Any"
    if isinstance(val_node, ast.List):
        if val_node.elts and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in val_node.elts):
            return "list[str]"
        return "list"
    if isinstance(val_node, ast.Set):
        if val_node.elts and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in val_node.elts):
            return "set[str]"
        return "set"
    if isinstance(val_node, ast.Dict):
        return "dict"
    if isinstance(val_node, ast.Tuple):
        return "tuple"
    if isinstance(val_node, ast.Constant):
        if isinstance(val_node.value, str):
            return "str"
        if isinstance(val_node.value, bool):
            return "bool"
        if isinstance(val_node.value, int):
            return "int"
        if isinstance(val_node.value, float):
            return "float"
        return type(val_node.value).__name__
    if isinstance(val_node, ast.Call):
        func_name = ""
        if isinstance(val_node.func, ast.Name):
            func_name = val_node.func.id
        elif isinstance(val_node.func, ast.Attribute):
            func_name = val_node.func.attr
        if func_name == "compile":
            return "re.Pattern"
        if func_name in ("Path", "set", "list", "dict"):
            return func_name
    return "Any"


# なぜ必要か: 中途半端な「...」省略を廃止し、完全値または型+役割で1行定義してハルシネーションを防止 (要件1-2)
def _format_constant_entry(
    target_name: str,
    node: ast.Assign | ast.AnnAssign,
    ann_node: Optional[ast.AST] = None,
    source_lines: Optional[List[str]] = None,
) -> str:
    val_node = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
    lineno = getattr(node, "lineno", 0)
    role_comment = ""
    # なぜ必要か: 正規表現や文字列リテラル内部の「#」をコメントと誤認して壊れた断片を出力するバグを防止
    if source_lines and 1 <= lineno <= len(source_lines):
        line = source_lines[lineno - 1]
        end_col = getattr(node, "end_col_offset", None)
        if end_col is not None and end_col <= len(line):
            tail = line[end_col:]
            if "#" in tail:
                role_comment = tail.split("#", 1)[1].strip()

    if ann_node:
        try:
            type_str = ast.unparse(ann_node) if hasattr(ast, "unparse") else "Any"
        except Exception:
            type_str = _infer_type_str(val_node)
    else:
        type_str = _infer_type_str(val_node)

    comment_suffix = f"  # {role_comment}" if role_comment else ""

    if val_node and isinstance(val_node, ast.Constant):
        val = val_node.value
        if isinstance(val, (bool, int, float)) or val is None:
            return f"{target_name} = {val}{comment_suffix}"
        if isinstance(val, str) and len(repr(val)) <= 35:
            return f"{target_name} = {repr(val)}{comment_suffix}"

    return f"{target_name}: {type_str}{comment_suffix}"


def extract_roles_from_ast(tree: ast.AST, rel_file_path: str, source_code: str = "") -> List[RoleEntry]:
    entries: List[RoleEntry] = []
    source_lines = source_code.splitlines() if source_code else []

    module_doc = ast.get_docstring(tree)
    # なぜ必要か: start_lineno=2の固定指定によるコメント見落としバグを根本解消
    module_comments = _get_module_header_comments(source_lines) if source_lines else []
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

    # なぜ必要か: UPPER_SNAKE_CASE定数のうち、内部用正規表現パターンを除外し真の設定定数のみ抽出 (要件1-2)
    constants: List[str] = []
    for node in getattr(tree, "body", []):
        target_name = None
        ann_node = None
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    target_name = t.id
                    break
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                target_name = node.target.id
                ann_node = node.annotation

        if target_name and target_name.isupper() and not target_name.startswith("_"):
            # 内部実装用正規表現パターンを排除してトークン浪費を防止
            if target_name.endswith(("_PATTERN", "_PATTERNS", "_REGEX")):
                continue
            constants.append(_format_constant_entry(target_name, node, ann_node, source_lines))

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
                # なぜ必要か: 型注釈付きフィールドを抽出しプロパティ参照エラーを完全抑止 (要件2-1)
                extracted_fields: List[str] = []
                for sub in node.body:
                    if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                        if not sub.target.id.startswith("_"):
                            try:
                                ann_str = ast.unparse(sub.annotation) if hasattr(ast, "unparse") else "Any"
                            except Exception:
                                ann_str = "Any"
                            extracted_fields.append(f"{sub.target.id}: {ann_str}")

                # なぜ必要か: UIイベントハンドラを分離検出し、通常メソッド一覧から除外 (要件1-1 方針A)
                ui_handlers: List[str] = []
                method_entries: List[RoleEntry] = []

                for sub_node in node.body:
                    if isinstance(sub_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if sub_node.name.startswith("_"):
                            continue
                        if sub_node.name in QT_UI_EVENT_HANDLERS:
                            ui_handlers.append(sub_node.name)
                            continue

                        sub_comments = _get_leading_comments(source_lines, getattr(sub_node, "lineno", 0))
                        method_entries.append(
                            RoleEntry(
                                file_path=rel_file_path,
                                element_type="Method",
                                name=f"{node.name}.{sub_node.name}",
                                signature=_get_function_signature(sub_node),
                                description=_build_description(ast.get_docstring(sub_node), sub_comments, sub_node.name),
                            )
                        )

                entries.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type="Class",
                        name=node.name,
                        signature=f"class {node.name}:",
                        description=_build_description(ast.get_docstring(node), leading_comments, node.name),
                        fields=extracted_fields if extracted_fields else None,
                        ui_handlers=ui_handlers if ui_handlers else None,
                    )
                )
                entries.extend(method_entries)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # なぜ必要か: 内部ヘルパー関数（_始まり）を除外して公開インターフェースに限定 (要件1-4)
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
