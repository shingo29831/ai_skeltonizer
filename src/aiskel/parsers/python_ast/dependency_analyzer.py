import ast
from dataclasses import dataclass
import sys
from typing import List, Set


@dataclass
class DependencyEntry:
    file_path: str
    imported_modules: List[str]


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


def extract_dependencies_from_ast(tree: ast.AST, rel_file_path: str) -> DependencyEntry:
    modules: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                if root_pkg not in _STDLIB_MODULES:
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_pkg = node.module.split(".")[0]
                if root_pkg not in _STDLIB_MODULES:
                    modules.add(node.module)
            else:
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    if root_pkg not in _STDLIB_MODULES:
                        modules.add(alias.name)

    return DependencyEntry(
        file_path=rel_file_path,
        imported_modules=sorted(list(modules)),
    )


def generate_dependency_map_text(entries: List[DependencyEntry]) -> str:
    lines = [
        "# AI Context: Module Dependency Graph Map",
        "",
        "各ファイルが依存(import)している内部および外部モジュールの一覧です。リファクタリングの影響範囲の特定に使用してください。",
        "",
    ]

    sorted_entries = sorted(entries, key=lambda e: e.file_path)
    for entry in sorted_entries:
        if not entry.imported_modules:
            continue
        lines.append(f"## 🔗 `{entry.file_path}`")
        for mod in entry.imported_modules:
            lines.append(f"- `{mod}`")
        lines.append("")

    return "\n".join(lines)
