"""Module: @role: パッチ適用処理の実行結果（成功数・失敗数・スキップ数・変更ファイル群・失敗詳細）を保持するデータモデルを提供する。"""
from pathlib import Path
from typing import Dict, List, Optional, Set

class PatchResult(tuple):
    # なぜ必要か: 後方互換を保ちつつ失敗詳細・新規ディレクトリ・復元スナップショットを提供するため
    def __new__(
        cls,
        success: int,
        fail: int,
        skipped: int,
        modified_files: Set[Path],
        failed_details: Optional[List[str]] = None,
        created_dirs: Optional[List[Path]] = None,
        policy_added_dirs: Optional[List[Path]] = None,
        rollback_snapshot: Optional[Dict[Path, Optional[bytes]]] = None,
    ):
        return super().__new__(cls, (success, fail, skipped, modified_files))

    def __init__(
        self,
        success: int,
        fail: int,
        skipped: int,
        modified_files: Set[Path],
        failed_details: Optional[List[str]] = None,
        created_dirs: Optional[List[Path]] = None,
        policy_added_dirs: Optional[List[Path]] = None,
        rollback_snapshot: Optional[Dict[Path, Optional[bytes]]] = None,
    ):
        self.success = success
        self.fail = fail
        self.skipped = skipped
        self.modified_files = modified_files
        self.failed_details = failed_details or []
        self.created_dirs = created_dirs or []
        self.policy_added_dirs = policy_added_dirs or []
        self.rollback_snapshot = rollback_snapshot if rollback_snapshot is not None else {}
