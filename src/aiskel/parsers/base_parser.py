from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple


@dataclass
class RoleEntry:
    file_path: str
    element_type: str  # "Module", "Class", "Function", "Method", "Constant"
    name: str
    signature: str
    description: str
    fields: Optional[List[str]] = None
    enum_members: Optional[List[str]] = None


@dataclass
class DependencyEntry:
    file_path: str
    imported_modules: List[str] = field(default_factory=list)
    internal_imports: List[str] = field(default_factory=list)
    external_imports: List[str] = field(default_factory=list)


class BaseParser(ABC):
    @abstractmethod
    def parse_and_process(
        self,
        source_code: str,
        rel_file_path: str,
        keep_functions: Set[str],
        only_nodes: Set[str],
    ) -> Tuple[str, List[RoleEntry], DependencyEntry]:
        """ソースコードを解析し、スケルトンコード、役割リスト、依存関係を返す"""
        pass


# なぜ必要か: 二重ラベル（Class Foo: class Foo）の排除、型フィールド・定数のコンパクト集約により要約トークンを最適化
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

        constant_entries = [e for e in file_entries if e.element_type == "Constant"]
        for const in constant_entries:
            if const.signature:
                lines.append(const.signature)

        classes: dict[str, List[RoleEntry]] = {}
        standalone_funcs: List[RoleEntry] = []

        for entry in file_entries:
            if entry.element_type in ("Module", "Constant"):
                continue
            name_parts = entry.name.split(".")
            base_func_name = name_parts[-1]
            # なぜ必要か: 特殊メソッドおよびプライベートメソッドを除外して公開インターフェースに絞り込む
            if base_func_name.startswith("_") and entry.element_type in ("Method", "Function"):
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
                lines.append(f"{entry.signature}{class_desc}")
                if entry.fields:
                    lines.append(f"  fields: {', '.join(entry.fields)}")
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


# なぜ必要か: 内部パス正規化と外部サードパーティ分離によりリファクタリング影響範囲分析を支援
def generate_dependency_map_text(entries: List[DependencyEntry]) -> str:
    lines = ["# Dependency Graph"]
    for entry in sorted(entries, key=lambda e: e.file_path):
        if not entry.internal_imports and not entry.external_imports:
            continue
        lines.append(entry.file_path)
        if entry.internal_imports:
            lines.append(f"  internal: {', '.join(entry.internal_imports)}")
        if entry.external_imports:
            lines.append(f"  external: {', '.join(entry.external_imports)}")
    return "\n".join(lines)
