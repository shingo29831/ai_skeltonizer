"""Module: @role: 新規タブ引き継ぎの共有定数・用途種別・ペイロードデータモデルを定義する。"""
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

MAX_HANDOFF_PAYLOAD_BYTES = 4 * 1024 * 1024
HANDOFF_TTL_SEC = 120.0
MAX_PENDING_HANDOFFS = 20
MAX_TRACKED_AUTO_TABS = 512

class HandoffKind(str, Enum):
    """新規タブ引き継ぎの用途種別を定義する。"""

    PATCH_REVIEW = "patch_review"

    DISCOVERY = "discovery"

    FILE_SPLIT = "file_split"
    DIR_ORGANIZE = "dir_organize"
    CONFIG_INFER = "config_infer"

@dataclass(frozen=True)
class DiscoveryTask:
    """1度目探索で分割された単一タスクの要件と対象パス群を保持する。"""

    task_text: str
    dirs: Tuple[str, ...]

@dataclass(frozen=True)
class TabHandoffPayload:
    """新規タブへ引き継ぐAI応答・要約バンドル・紐づけ情報を保持する。"""

    handoff_id: str
    source_tab_id: str
    new_chat_url: str
    response_text: str
    summary_text: str
    combined_text: str
    created_at: float
    assigned_tab_id: Optional[str] = None
    remaining_auto_tabs: int = 0
    handoff_kind: str = HandoffKind.PATCH_REVIEW.value
    target_file: str = ""
    source_command_notice: str = ""
    source_command_is_error: bool = False
    allow_rename: Optional[bool] = None
    config_infer_target: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "handoff_id": self.handoff_id,
            "source_tab_id": self.source_tab_id,
            "new_chat_url": self.new_chat_url,
            "response_text": self.response_text,
            "summary_text": self.summary_text,
            "combined_text": self.combined_text,
            "assigned_tab_id": self.assigned_tab_id,
            "remaining_auto_tabs": self.remaining_auto_tabs,
            "handoff_kind": self.handoff_kind,
            "target_file": self.target_file,
            "source_command_notice": self.source_command_notice,
            "source_command_is_error": self.source_command_is_error,
            "allow_rename": self.allow_rename,
            "config_infer_target": self.config_infer_target,
        }
