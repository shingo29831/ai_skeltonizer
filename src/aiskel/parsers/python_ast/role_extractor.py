"""Module: @role: Python ASTからモジュール・クラス・関数・定数およびUIハンドラの役割メタデータを抽出する。"""

import ast
import re
from typing import List, Optional

from aiskel.parsers.base_parser import RoleEntry
from aiskel.parsers.sanitizer import extract_clean_role_description

# なぜ必要か: コンテキストマネージャーやCallable等のインターフェース契約に関わる特殊メソッドを保持しAIの見落としを防止
CRITICAL_DUNDER_METHODS = {
    "__init__",
    "__call__",
    "__enter__",
    "__exit__",
    "__getitem__",
    "__iter__",
    "__len__",
    "__contains__",
    "__aenter__",
    "__aexit__",
}

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


# なぜ必要か: AST内の明示的raiseに加え、Docstring記載の仕様例外も拾い集めてAIのエラー設計漏れを防止
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

    # なぜ必要か: DocstringのRaises記述からも抽出して下位層送出例外の把握漏れを防止
    doc = ast.get_docstring(node)
    if doc:
        doc_raises = re.findall(r"(?:Raises|raises)\s*:\s*\n?\s*([A-Za-z0-9_]+Error|[A-Za-z0-9_]+Exception)", doc)
        for exc in doc_raises:
            raises.add(exc)

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


# なぜ必要か: メソッド修飾子に加え、Webルート・CLI・バリデータ等の重要エントリーポイント契約をAIに見失わせない
CRITICAL_METHOD_DECORATORS = {"property", "classmethod", "staticmethod", "abstractmethod"}
# なぜ必要か: APIルーティングやCLIコマンド定義の消失によるエンドポイント特定不能バグを防止
ROUTING_AND_ENTRY_DECORATORS = {
    "get", "post", "put", "delete", "patch", "route", "api_view",
    "command", "group", "task", "shared_task", "field_validator", "validator", "model_validator"
}


def _format_decorator(d: ast.AST) -> Optional[str]:
    # なぜ必要か: 引数付きデコレータ(@app.get('/path')等)を要約しURL・コマンドの消失を防止
    try:
        if isinstance(d, ast.Call):
            func_name = ast.unparse(d.func)
            base_ident = func_name.split(".")[-1]
            if base_ident in ROUTING_AND_ENTRY_DECORATORS or base_ident in CRITICAL_METHOD_DECORATORS:
                compact_args = [ast.unparse(a) for a in d.args[:2]]
                args_repr = f"({', '.join(compact_args)})" if compact_args else "()"
                if len(args_repr) > 40:
                    args_repr = "(...)"
                return f"@{func_name}{args_repr}"
        elif isinstance(d, (ast.Name, ast.Attribute)):
            d_str = ast.unparse(d)
            base_ident = d_str.split(".")[-1]
            if base_ident in CRITICAL_METHOD_DECORATORS or base_ident in ROUTING_AND_ENTRY_DECORATORS:
                return f"@{d_str}"
    except Exception:
        pass
    return None


def _get_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args_str = ast.unparse(node.args) if hasattr(ast, "unparse") else "..."
        returns_str = f" -> {ast.unparse(node.returns)}" if node.returns and hasattr(ast, "unparse") else ""
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        
        decs = []
        for d in getattr(node, "decorator_list", []):
            dec_str = _format_decorator(d)
            if dec_str:
                decs.append(f"{dec_str} ")
        dec_prefix = "".join(decs)
        return f"{dec_prefix}{prefix}{node.name}({args_str}){returns_str}"
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

    # なぜ必要か: 設定・ガード条件となる短小コレクションをリテラル展開しAIのハルシネーションを抑止
    if val_node:
        if isinstance(val_node, ast.Constant):
            val = val_node.value
            if isinstance(val, (bool, int, float)) or val is None:
                return f"{target_name} = {val}{comment_suffix}"
            if isinstance(val, str) and len(repr(val)) <= 40:
                return f"{target_name} = {repr(val)}{comment_suffix}"
        elif isinstance(val_node, (ast.List, ast.Set, ast.Tuple)):
            # なぜ必要か: UI拡張子や除外設定などの重要コレクションが型名に丸め込まれるのを防ぐため上限を16要素・100文字に緩和
            try:
                unparsed = ast.unparse(val_node) if hasattr(ast, "unparse") else ""
                elts_count = len(getattr(val_node, "elts", []))
                if unparsed and elts_count <= 16 and len(unparsed) <= 100:
                    return f"{target_name} = {unparsed}{comment_suffix}"
            except Exception:
                pass
        elif isinstance(val_node, ast.Dict):
            # なぜ必要か: 言語マッピング等の辞書定数で中身が完全不可視になるのを防ぐため短小辞書またはキー一覧を展開
            try:
                unparsed = ast.unparse(val_node) if hasattr(ast, "unparse") else ""
                keys_count = len(val_node.keys)
                if unparsed and keys_count <= 8 and len(unparsed) <= 80:
                    return f"{target_name} = {unparsed}{comment_suffix}"
                # なぜ必要か: 言語拡張子マッピング等の多要素辞書でも対応キー一覧を展開しAIの推測を防止
                # なぜ必要か: 言語拡張子マッピング等の仕様定数でキーが切り捨てられてAIが対応状況を見落とすのを防止 (上限を35件に緩和)
                key_reprs = [ast.unparse(k) for k in val_node.keys if k is not None]
                if key_reprs:
                    if len(key_reprs) <= 35:
                        return f"{target_name}: dict(keys: {', '.join(key_reprs)}){comment_suffix}"
                    return f"{target_name}: dict(keys: {', '.join(key_reprs[:30])}, ...+{len(key_reprs)-30}){comment_suffix}"
            except Exception:
                pass

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
        # なぜ必要か: 重要定数の切り捨てを緩和しつつトークン上限を保護するため最大10件まで展開
        entries.append(
            RoleEntry(
                file_path=rel_file_path,
                element_type="Constant",
                name="constants",
                signature=f"constants: {', '.join(constants[:10])}" + (", ..." if len(constants) > 10 else ""),
                description="",
            )
        )

    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef):
            # なぜ必要か: 内部プライベートクラス（_始まり）を除外して公開インターフェースに限定し要約トークンを最適化
            if node.name.startswith("_"):
                continue
            leading_comments = _get_leading_comments(source_lines, getattr(node, "lineno", 0))
            base_names = _get_base_names(node)
            is_enum = any("Enum" in b or "Flag" in b for b in base_names)

            # なぜ必要か: Enum・通常クラス双方に定義されたメソッドやプロパティの完全性を維持
            ui_handlers: List[str] = []
            method_entries: List[RoleEntry] = []

            for sub_node in node.body:
                if isinstance(sub_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # なぜ必要か: __init__および重要インターフェース契約メソッドを保持
                    if sub_node.name.startswith("_") and sub_node.name not in CRITICAL_DUNDER_METHODS:
                        continue
                    if sub_node.name in QT_UI_EVENT_HANDLERS:
                        ui_handlers.append(sub_node.name)
                        continue

                    sub_comments = _get_leading_comments(source_lines, getattr(sub_node, "lineno", 0))
                    method_raises = _extract_raises_from_node(sub_node)
                    method_entries.append(
                        RoleEntry(
                            file_path=rel_file_path,
                            element_type="Method",
                            name=f"{node.name}.{sub_node.name}",
                            signature=_get_function_signature(sub_node),
                            description=_build_description(ast.get_docstring(sub_node), sub_comments, sub_node.name),
                            raises=method_raises if method_raises else None,
                        )
                    )

            if is_enum:
                # なぜ必要か: Enumのキーだけでなく短いリテラル値(文字列/数値)も保持しAIの状態値推測ミス・ハルシネーションを防止
                enum_members = []
                for sub in node.body:
                    target_name = None
                    val_repr = None
                    if isinstance(sub, ast.Assign):
                        for t in sub.targets:
                            if isinstance(t, ast.Name) and not t.id.startswith("_"):
                                target_name = t.id
                                if isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, (str, int, bool)):
                                    val_repr = repr(sub.value.value)
                                break
                    elif isinstance(sub, ast.AnnAssign):
                        if isinstance(sub.target, ast.Name) and not sub.target.id.startswith("_"):
                            target_name = sub.target.id
                            if sub.value and isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, (str, int, bool)):
                                val_repr = repr(sub.value.value)

                    if target_name:
                        enum_members.append(f"{target_name} = {val_repr}" if val_repr else target_name)

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
                entries.extend(method_entries)
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

                # なぜ必要か: @dataclassデコレータを保持しデータモデルの種別誤認を防止
                is_dataclass = any(
                    (isinstance(d, ast.Name) and d.id == "dataclass")
                    or (isinstance(d, ast.Attribute) and d.attr == "dataclass")
                    or (isinstance(d, ast.Call) and isinstance(d.func, (ast.Name, ast.Attribute)) and getattr(d.func, "id", getattr(d.func, "attr", "")) == "dataclass")
                    for d in getattr(node, "decorator_list", [])
                )
                class_prefix = "@dataclass\n" if is_dataclass else ""
                # なぜ必要か: 基底クラスをシグネチャに明示しAIによるインターフェース・多態性の見落としを抑止
                bases_suffix = f"({', '.join(base_names)})" if base_names else ""
                entries.append(
                    RoleEntry(
                        file_path=rel_file_path,
                        element_type="Class",
                        name=node.name,
                        signature=f"{class_prefix}class {node.name}{bases_suffix}:",
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
            func_raises = _extract_raises_from_node(node)
            entries.append(
                RoleEntry(
                    file_path=rel_file_path,
                    element_type="Function",
                    name=node.name,
                    signature=_get_function_signature(node),
                    description=_build_description(ast.get_docstring(node), leading_comments, node.name),
                    raises=func_raises if func_raises else None,
                )
            )

    return entries
