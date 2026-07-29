# src/py_skeletonizer/dependency_analyzer.py
"""
Role: ASTからインポート文を解析し、プロジェクト内モジュール間の依存関係マップを構築する。
"""
import ast
from dataclasses import dataclass
from typing import Dict, List, Set


@dataclass
class DependencyEntry:
    file_path: str
    imported_modules: List[str]


def extract_dependencies_from_ast(tree: ast.AST, rel_file_path: str) -> DependencyEntry:
    modules: Set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            else:
                for alias in node.names:
                    modules.add(alias.name)

    return DependencyEntry(
        file_path=rel_file_path,
        imported_modules=sorted(list(modules))
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