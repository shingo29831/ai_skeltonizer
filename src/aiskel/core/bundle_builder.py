# src/py_skeletonizer/bundle_builder.py
"""
Role: ポリシー、プロジェクトツリー、全体辞書、スケルトンコード群を1つのバンドルファイルに統合し、トークン削減率を計算する。
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from .context_splitter import is_static_skeleton_file

@dataclass
class TokenStats:
    raw_chars: int = 0
    skeleton_chars: int = 0
    raw_tokens: int = 0
    skeleton_tokens: int = 0

    @property
    def raw_tokens_est(self) -> int:
        return self.raw_chars // 3

    @property
    def skeleton_tokens_est(self) -> int:
        return self.skeleton_chars // 3

    @property
    def saved_tokens(self) -> int:
        return max(0, self.raw_tokens - self.skeleton_tokens)

    @property
    def reduction_percentage(self) -> float:
        if self.raw_tokens == 0:
            return 0.0
        return (1.0 - (self.skeleton_tokens / self.raw_tokens)) * 100.0


def _discover_policy_text(project_root: Path, custom_policy_path: Optional[Path]) -> str:
    if custom_policy_path and custom_policy_path.exists():
        try:
            return custom_policy_path.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"指定されたポリシーファイルの読み込みに失敗しました: {custom_policy_path} ({e})")

    candidate_names = [".cursorrules", ".windsurfrules", "AI_POLICY.md", "RULE.md", "CLAUDE.md"]
    for name in candidate_names:
        candidate_path = project_root / name
        if candidate_path.exists():
            try:
                return candidate_path.read_text(encoding="utf-8")
            except OSError:
                continue

    return "・冒頭でファイルの役割を明示。UIとロジックは疎結合に。\n・修正時はファイル内のコードを省略せず出力。\n・エラー握り潰し厳禁、根本解決を。"


def build_bundle_file(
    project_root: Path,
    output_dir: Path,
    tree_text: str,
    role_map_text: str,
    dependency_map_text: str,
    file_contents: Dict[str, str],
    bundle_format: str = "txt",
    custom_policy_path: Optional[Path] = None,
) -> Path:
    policy_text = _discover_policy_text(project_root, custom_policy_path)
    bundle_path = output_dir / "phase2_context_bundle.txt"
    static_skeleton_path = output_dir / "phase2_static_skeleton.txt"
    architecture_path = output_dir / "phase1_architecture_bundle.txt"

    lines: List[str] = []
    static_lines: List[str] = []
    arch_lines: List[str] = []

    static_items = []
    dynamic_items = []
    for rel_path, content in sorted(file_contents.items()):
        if is_static_skeleton_file(Path(rel_path)):
            static_items.append((rel_path, content))
        else:
            dynamic_items.append((rel_path, content))

    if bundle_format == "xml" or bundle_format == "txt":
        arch_lines.append("<ai_architecture_bundle>")
        arch_lines.append("  <policy>\n" + policy_text.strip() + "\n  </policy>")
        arch_lines.append("  <project_tree>\n" + tree_text.strip() + "\n  </project_tree>")
        arch_lines.append("  <role_architecture_map>\n" + role_map_text.strip() + "\n  </role_architecture_map>")
        arch_lines.append("  <dependency_graph>\n" + dependency_map_text.strip() + "\n  </dependency_graph>")
        arch_lines.append("</ai_architecture_bundle>")

        lines.append("<ai_context_bundle>")
        lines.append("  <policy>\n" + policy_text.strip() + "\n  </policy>")
        lines.append("  <project_tree>\n" + tree_text.strip() + "\n  </project_tree>")
        lines.append("  <role_architecture_map>\n" + role_map_text.strip() + "\n  </role_architecture_map>")
        lines.append("  <dependency_graph>\n" + dependency_map_text.strip() + "\n  </dependency_graph>")
        lines.append("  <codebase>")
        
        if static_items:
            lines.append("    <static_skeleton>")
            static_lines.append("<static_skeleton>")
            for rel_path, content in static_items:
                file_block = f'      <file path="{rel_path}">\n{content.rstrip()}\n      </file>'
                lines.append(file_block)
                static_lines.append(file_block)
            lines.append("    </static_skeleton>")
            static_lines.append("</static_skeleton>")
            
        if dynamic_items:
            lines.append("    <dynamic_flesh>")
            for rel_path, content in dynamic_items:
                lines.append(f'      <file path="{rel_path}">\n{content.rstrip()}\n      </file>')
            lines.append("    </dynamic_flesh>")
            
        lines.append("  </codebase>")
        lines.append("</ai_context_bundle>")
    else:
        arch_lines.append("# Architecture Bundle")
        arch_lines.append("## Policy\n" + policy_text.strip())
        arch_lines.append("## Tree\n```\n" + tree_text.strip() + "\n```")
        arch_lines.append(role_map_text.strip())
        arch_lines.append(dependency_map_text.strip())

        lines.append("# Context Bundle")
        lines.append("## Policy\n" + policy_text.strip())
        lines.append("## Tree\n```\n" + tree_text.strip() + "\n```")
        lines.append(role_map_text.strip())
        lines.append(dependency_map_text.strip())
        lines.append("## Codebase")
        
        if static_items:
            lines.append("### [Static Skeleton Context]")
            static_lines.append("### [Static Skeleton Context]")
            for rel_path, content in static_items:
                lines.append(f"#### File: `{rel_path}`")
                lines.append("```python")
                lines.append(content.rstrip())
                lines.append("```")
                lines.append("")
                
                static_lines.append(f"#### File: `{rel_path}`")
                static_lines.append("```python")
                static_lines.append(content.rstrip())
                static_lines.append("```")
                static_lines.append("")
                
        if dynamic_items:
            lines.append("### [Dynamic Flesh Context]")
            for rel_path, content in dynamic_items:
                lines.append(f"#### File: `{rel_path}`")
                lines.append("```python")
                lines.append(content.rstrip())
                lines.append("```")
                lines.append("")

    try:
        bundle_path.write_text("\n".join(lines), encoding="utf-8")
        architecture_path.write_text("\n".join(arch_lines), encoding="utf-8")
        if static_lines:
            static_skeleton_path.write_text("\n".join(static_lines), encoding="utf-8")
        return bundle_path
    except OSError as e:
        raise RuntimeError(f"バンドルファイルの生成に失敗しました: {bundle_path} ({e})")