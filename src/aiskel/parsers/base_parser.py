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
        for entry in [e for e in file_entries if e.element_type != "Module"]:
            lines.append(f"{entry.element_type} {entry.name}: {entry.signature}")
            if entry.description and entry.description != "(役割記述なし)":
                lines.append(f"  {entry.description}")
    return "\n".join(lines)

def generate_dependency_map_text(entries: List[DependencyEntry]) -> str:
    lines = ["# Dependency Graph"]
    for entry in sorted(entries, key=lambda e: e.file_path):
        if not entry.imported_modules:
            continue
        lines.append(f"{entry.file_path} -> {', '.join(entry.imported_modules)}")
    return "\n".join(lines)