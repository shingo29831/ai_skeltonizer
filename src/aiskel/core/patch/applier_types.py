"""Module: @role: パッチ適用トランザクション中の仮想ファイルバッファおよび集計状態のデータモデルを定義する。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

@dataclass
class PatchApplyState:
    """インメモリ仮想バッファによるパッチ適用中の変更状態と成否集計を保持する。"""

    fail_count: int = 0
    failed_details: List[str] = field(default_factory=list)
    success_count: int = 0
    skipped_count: int = 0
    modified_files: Set[Path] = field(default_factory=set)
    files_to_delete: Set[Path] = field(default_factory=set)
    file_contents: Dict[Path, Optional[str]] = field(default_factory=dict)
    original_contents: Dict[Path, Optional[str]] = field(default_factory=dict)
    binary_contents: Dict[Path, bytes] = field(default_factory=dict)
    move_sources: Dict[Path, Path] = field(default_factory=dict)
    extra_allowed_dirs: Set[Path] = field(default_factory=set)
    case_rename_targets: Set[Path] = field(default_factory=set)
