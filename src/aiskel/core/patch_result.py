"""Module: @role: パッチ適用処理の実行結果（成功数・失敗数・スキップ数・変更ファイル群・失敗詳細）を保持するデータモデルを提供する。"""
from pathlib import Path
from typing import List, Optional, Set


class PatchResult(tuple):
    # なぜ必要か: 既存の4要素タプル展開との後方互換を保ちつつ失敗詳細属性を安全に提供するため
    def __new__(cls, success: int, fail: int, skipped: int, modified_files: Set[Path], failed_details: Optional[List[str]] = None):
        return super().__new__(cls, (success, fail, skipped, modified_files))

    def __init__(self, success: int, fail: int, skipped: int, modified_files: Set[Path], failed_details: Optional[List[str]] = None):
        self.success = success
        self.fail = fail
        self.skipped = skipped
        self.modified_files = modified_files
        self.failed_details = failed_details or []
