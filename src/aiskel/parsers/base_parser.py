# src/aiskel/parsers/base_parser.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Set, Tuple

@dataclass
class RoleEntry:
    file_path: str
    element_type: str
    name: str
    signature: str
    description: str

@dataclass
class DependencyEntry:
    file_path: str
    imported_modules: List[str]

class BaseParser(ABC):
    @abstractmethod
    def parse_and_process(
        self, 
        source_code: str, 
        rel_file_path: str, 
        keep_functions: Set[str], 
        only_nodes: Set[str]
    ) -> Tuple[str, List[RoleEntry], DependencyEntry]:
        """ソースコードを解析し、スケルトンコード、役割リスト、依存関係を返す"""
        pass

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

    for path in sorted(grouped.keys()):
        file_entries = grouped[path]
        lines.append(f"## 📁 `{path}`")
        module_entries = [e for e in file_entries if e.element_type == "Module"]
        if module_entries and module_entries[0].description and module_entries[0].description != "(役割記述なし)":
            lines.append(f"> **Module Role**: {module_entries[0].description}")
        lines.append("")
        for entry in [e for e in file_entries if e.element_type != "Module"]:
            icon = "🔷" if entry.element_type == "Class" else ("🔹" if entry.element_type == "Method" else "🔸")
            lines.append(f"- {icon} **{entry.element_type} `{entry.name}`**")
            lines.append(f"  - `Signature`: `{entry.signature}`")
            if entry.description and entry.description != "(役割記述なし)":
                lines.append(f"  - `Role`: {entry.description}")
        lines.append("")
    return "\n".join(lines)

def generate_dependency_map_text(entries: List[DependencyEntry]) -> str:
    lines = [
        "# AI Context: Module Dependency Graph Map",
        "",
        "各ファイルが依存(import)している内部および外部モジュールの一覧です。リファクタリングの影響範囲の特定に使用してください。",
        "",
    ]
    for entry in sorted(entries, key=lambda e: e.file_path):
        if not entry.imported_modules:
            continue
        lines.append(f"## 🔗 `{entry.file_path}`")
        for mod in entry.imported_modules:
            lines.append(f"- `{mod}`")
        lines.append("")
    return "\n".join(lines)