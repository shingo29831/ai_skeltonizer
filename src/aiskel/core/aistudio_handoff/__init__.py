"""Module: @role: Google AI Studioの新規タブ引き継ぎ状態管理・テキスト整形・スクリプト生成の公開ファサードを提供する。"""

from .manager import PatchHandoffManager
from .script import build_patch_handoff_js
from .text import (
    extract_discovery_tasks,
    format_handoff_prompt_text,
    format_summary_prompt_text,
    has_applicable_patch_in_text,
    has_handoff_stop_signal_in_text,
    has_move_patch_in_text,
    has_patch_delimiters_in_text,
    load_project_summary_text,
)
from .types import (
    HANDOFF_TTL_SEC,
    MAX_HANDOFF_PAYLOAD_BYTES,
    MAX_PENDING_HANDOFFS,
    MAX_TRACKED_AUTO_TABS,
    DiscoveryTask,
    HandoffKind,
    TabHandoffPayload,
)

__all__ = [
    "DiscoveryTask",
    "HANDOFF_TTL_SEC",
    "HandoffKind",
    "MAX_HANDOFF_PAYLOAD_BYTES",
    "MAX_PENDING_HANDOFFS",
    "MAX_TRACKED_AUTO_TABS",
    "PatchHandoffManager",
    "TabHandoffPayload",
    "build_patch_handoff_js",
    "extract_discovery_tasks",
    "format_handoff_prompt_text",
    "format_summary_prompt_text",
    "has_applicable_patch_in_text",
    "has_handoff_stop_signal_in_text",
    "has_move_patch_in_text",
    "has_patch_delimiters_in_text",
    "load_project_summary_text",
]
