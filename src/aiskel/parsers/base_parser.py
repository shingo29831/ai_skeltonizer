"""Module: @role: スケルトンパーサーの抽象基底インターフェースおよび要約テキスト生成フォーマッタを提供する。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import re
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
    ui_handlers: Optional[List[str]] = None
    raises: Optional[List[str]] = None


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


# なぜ必要か: 二重ラベルの排除、型フィールド・UIハンドラ・定数のコンパクト集約により要約トークンを最適化
def generate_role_map_text(all_entries: List[RoleEntry]) -> str:
    lines = ["# Role Map"]
    grouped: dict[str, List[RoleEntry]] = {}
    for entry in all_entries:
        grouped.setdefault(entry.file_path, []).append(entry)

    for path in sorted(grouped.keys()):
        file_entries = grouped[path]
        lines.append(f"[{path}]")

        module_entries = [e for e in file_entries if e.element_type == "Module"]
        if module_entries and module_entries[0].description:
            raw_desc = module_entries[0].description.strip()
            # なぜ必要か: (Auto)自動生成ダミーを排除し、Module: @role: 書式へ統一 (要件1-3, 2-4)
            if not raw_desc.startswith("(Auto)") and raw_desc != "(役割記述なし)":
                # なぜ必要か: 多重にネストされた Module: @role: Comment: プレフィックスを全除去して単一の Module: @role: に統一
                clean_desc = re.sub(
                    r"^(?:(?:\*\*\[(?:Role|AI|Rule)\]\*\*|(?:Module|Class|Function)\s*:?\s*|@role\s*:?|Role\s*:?|AI\s*:?|Rule\s*:?|Comment\s*:?)\s*)+",
                    "",
                    raw_desc,
                    flags=re.IGNORECASE,
                ).strip()
                if clean_desc:
                    lines.append(f"Module: @role: {clean_desc}")

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
            # なぜ必要か: 特殊メソッドおよびプライベートメソッドを除外して公開インターフェースに絞り込む (要件1-4)
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
                class_desc = f" - {entry.description}" if entry.description and entry.description != "(役割記述なし)" and not entry.description.startswith("(Auto)") else ""
                lines.append(f"{entry.signature}{class_desc}")
                # なぜ必要か: フィールド属性・型を明記しプロパティ参照エラーを抑止 (要件2-1)
                if entry.fields:
                    lines.append(f"  fields: {', '.join(entry.fields)}")
                # なぜ必要か: UI定型ハンドラを1行に集約しAIの見落とし防止とトークン削減を両立 (要件1-1 方針A)
                if entry.ui_handlers:
                    lines.append(f"  ui_handlers: {', '.join(entry.ui_handlers)}")
                for method in classes.get(class_name, []):
                    # なぜ必要か: 例外仕様を末尾コメントとして最小トークンで明示しAIのエラー処理設計漏れを防止
                    raises_str = f"  # raises: {', '.join(method.raises)}" if method.raises else ""
                    desc = f"\n    {method.description}" if method.description and method.description != "(役割記述なし)" and not method.description.startswith("(Auto)") else ""
                    lines.append(f"  {method.signature}{raises_str}{desc}")

        for class_name, methods in classes.items():
            if class_name and class_name not in seen_classes:
                lines.append(f"class {class_name}:")
                for method in methods:
                    raises_str = f"  # raises: {', '.join(method.raises)}" if method.raises else ""
                    desc = f"\n    {method.description}" if method.description and method.description != "(役割記述なし)" and not method.description.startswith("(Auto)") else ""
                    lines.append(f"  {method.signature}{raises_str}{desc}")

        for func in standalone_funcs:
            # なぜ必要か: 例外仕様を末尾コメントとして最小トークンで明示しAIのエラー処理設計漏れを防止
            raises_str = f"  # raises: {', '.join(func.raises)}" if func.raises else ""
            desc = f"\n  {func.description}" if func.description and func.description != "(役割記述なし)" and not func.description.startswith("(Auto)") else ""
            lines.append(f"{func.signature}{raises_str}{desc}")

    return "\n".join(lines)


# なぜ必要か: 内部パス正規化と外部サードパーティ分離によりリファクタリング影響範囲分析を支援 (要件2-3)
def generate_dependency_map_text(entries: List[DependencyEntry]) -> str:
    lines = ["# Dependency Graph"]
    for entry in sorted(entries, key=lambda e: e.file_path):
        lines.append(f"[{entry.file_path}]")
        if entry.internal_imports:
            lines.append(f"  internal: {', '.join(entry.internal_imports)}")
        if entry.external_imports:
            lines.append(f"  external: {', '.join(entry.external_imports)}")
    return "\n".join(lines)
