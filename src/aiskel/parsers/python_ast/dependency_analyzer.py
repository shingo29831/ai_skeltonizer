"""Module: @role: Python ASTからインポート文を解析し、内部モジュール依存と外部ライブラリ依存を分類・抽出する。"""

import ast
from pathlib import Path
import sys
from typing import List, Optional, Set

from aiskel.parsers.base_parser import DependencyEntry

# なぜ必要か: Python標準ライブラリ（sys, os, typing等）を除外し、プロジェクト間依存と主要外部ライブラリのみに絞る
_STDLIB_MODULES = getattr(
    sys,
    "stdlib_module_names",
    frozenset({
        "abc", "argparse", "ast", "asyncio", "base64", "collections", "contextlib",
        "copy", "csv", "dataclasses", "datetime", "difflib", "enum", "functools",
        "glob", "hashlib", "importlib", "inspect", "io", "itertools", "json",
        "logging", "math", "multiprocessing", "os", "pathlib", "pickle", "platform",
        "queue", "random", "re", "shutil", "socket", "sqlite3", "string", "subprocess",
        "sys", "tempfile", "threading", "time", "traceback", "typing", "unittest",
        "urllib", "uuid", "warnings", "weakref", "xml",
    }),
)


def _to_module_notation(path_str: str) -> str:
    """ファイルパスをドット区切りのPythonモジュール表記へ変換する"""
    p = path_str.replace("\\", "/")
    if p.endswith(".py"):
        p = p[:-3]
    if p.endswith("/__init__"):
        p = p[:-9]
    return p.replace("/", ".")


# なぜ必要か: 相対インポートおよびプロジェクトルートからの絶対指定インポートを内部モジュールへ解決する (要件2-3)
def _resolve_internal_path(
    current_file_path: Path,
    module: Optional[str],
    level: int,
    known_project_files: Optional[Set[str]] = None,
) -> Optional[str]:
    current_dir = current_file_path.parent

    # 相対インポート (例: from .base_inspector import ...) -> 確実に内部依存
    if level > 0:
        target_dir = current_dir
        for _ in range(level - 1):
            target_dir = target_dir.parent
        if module:
            candidate = (target_dir / (module.replace(".", "/") + ".py")).as_posix()
        else:
            candidate = (target_dir / "__init__.py").as_posix()
        return _to_module_notation(candidate)

    # プロジェクト内モジュールの推測解決 (例: from aiskel.parsers import ...)
    if module:
        mod_as_path = module.replace(".", "/") + ".py"
        candidates = [
            mod_as_path,
            (current_dir / mod_as_path).as_posix(),
        ]
        if current_file_path.parts and current_file_path.parts[0] == "src":
            candidates.insert(1, f"src/{mod_as_path}")

        if known_project_files:
            for cand in candidates:
                if cand in known_project_files or cand.replace(".py", "/__init__.py") in known_project_files:
                    return _to_module_notation(cand)

        for cand in candidates:
            if Path(cand).exists():
                return _to_module_notation(cand)

        sibling = (current_dir / (module.split(".")[0] + ".py")).as_posix()
        if (known_project_files and sibling in known_project_files) or Path(sibling).exists():
            return _to_module_notation(sibling)

        # カレントパスが src/ 始まりで同一トップパッケージの場合
        first_pkg = module.split(".")[0]
        if current_file_path.parts:
            if current_file_path.parts[0] == "src" and len(current_file_path.parts) > 1 and current_file_path.parts[1] == first_pkg:
                return f"src.{module}"
            if current_file_path.parts[0] == first_pkg:
                return module

    return None


def extract_dependencies_from_ast(
    tree: ast.AST,
    rel_file_path: str,
    known_project_files: Optional[Set[str]] = None,
) -> DependencyEntry:
    internal_deps: Set[str] = set()
    external_deps: Set[str] = set()
    all_imported: Set[str] = set()
    current_file_path = Path(rel_file_path)

    # プロジェクト自身のパッケージ名を検出し外部依存との誤認を防止
    own_packages: Set[str] = set()
    for part in current_file_path.parts[:-1]:
        if part not in (".", "..", "src"):
            own_packages.add(part)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                all_imported.add(alias.name)
                root_pkg = alias.name.split(".")[0]
                if root_pkg in _STDLIB_MODULES:
                    continue

                resolved = _resolve_internal_path(current_file_path, alias.name, 0, known_project_files)
                if resolved:
                    internal_deps.add(resolved)
                elif root_pkg in own_packages or (current_file_path.parts and root_pkg == current_file_path.parts[0]):
                    internal_deps.add(alias.name)
                else:
                    external_deps.add(root_pkg)

        elif isinstance(node, ast.ImportFrom):
            level = getattr(node, "level", 0)
            module = getattr(node, "module", None)
            import_name = module if module else ""
            if import_name:
                all_imported.add(import_name)

            if level > 0:
                resolved = _resolve_internal_path(current_file_path, module, level, known_project_files)
                if resolved:
                    internal_deps.add(resolved)
            elif module:
                root_pkg = module.split(".")[0]
                if root_pkg in _STDLIB_MODULES:
                    continue

                resolved = _resolve_internal_path(current_file_path, module, 0, known_project_files)
                if resolved:
                    internal_deps.add(resolved)
                elif root_pkg in own_packages or (current_file_path.parts and root_pkg == current_file_path.parts[0]):
                    internal_deps.add(module)
                else:
                    external_deps.add(root_pkg)
            else:
                for alias in node.names:
                    all_imported.add(alias.name)
                    root_pkg = alias.name.split(".")[0]
                    if root_pkg not in _STDLIB_MODULES:
                        if root_pkg in own_packages:
                            internal_deps.add(alias.name)
                        else:
                            external_deps.add(root_pkg)

    return DependencyEntry(
        file_path=rel_file_path,
        imported_modules=sorted(list(all_imported)),
        internal_imports=sorted(list(internal_deps)),
        external_imports=sorted(list(external_deps)),
    )
