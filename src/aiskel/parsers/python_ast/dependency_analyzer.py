import ast
from pathlib import Path
import sys
from typing import List, Optional, Set

from aiskel.parsers.base_parser import DependencyEntry, generate_dependency_map_text

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


# なぜ必要か: 相対インポートおよびプロジェクトルートからの絶対指定インポートを実体ファイルパスへ解決する
def _resolve_internal_path(
    current_file_path: Path,
    module: Optional[str],
    level: int,
    known_project_files: Optional[Set[str]] = None,
) -> Optional[str]:
    current_dir = current_file_path.parent

    # 相対インポート (例: from .base_inspector import ...)
    if level > 0:
        target_dir = current_dir
        for _ in range(level - 1):
            target_dir = target_dir.parent
        if module:
            candidate = (target_dir / (module.replace(".", "/") + ".py")).as_posix()
        else:
            candidate = target_dir.as_posix() + ".py"
        return candidate

    # プロジェクト内モジュールの推測解決 (例: from core.executor.runner import ...)
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
                if cand in known_project_files:
                    return cand

        # ファイル存在チェックまたは構造照合
        for cand in candidates:
            if Path(cand).exists():
                return cand

        # 同一ディレクトリ直下のインポート推測 (例: base_inspector)
        sibling = (current_dir / (module.split(".")[0] + ".py")).as_posix()
        if known_project_files and sibling in known_project_files:
            return sibling
        if Path(sibling).exists():
            return sibling

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
                else:
                    external_deps.add(root_pkg)
            else:
                for alias in node.names:
                    all_imported.add(alias.name)
                    root_pkg = alias.name.split(".")[0]
                    if root_pkg not in _STDLIB_MODULES:
                        external_deps.add(root_pkg)

    return DependencyEntry(
        file_path=rel_file_path,
        imported_modules=sorted(list(all_imported)),
        internal_imports=sorted(list(internal_deps)),
        external_imports=sorted(list(external_deps)),
    )
