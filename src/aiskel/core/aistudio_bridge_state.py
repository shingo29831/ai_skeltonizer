"""Module: @role: ブラウザタブとローカルブリッジの紐づけ状態管理・コマンド実行中状態追跡およびタブ検証ロジックを提供する。"""
import contextlib
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .aistudio_bridge_types import (
    MAX_COMMAND_KIND_CHARS,
    MAX_RUNNING_COMMAND_CHARS,
    BridgeCommandKind,
    RunningCommandInfo,
)

MAX_TAB_ID_CHARS = 128
MAX_TAB_TITLE_CHARS = 200
MAX_TAB_URL_CHARS = 500
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def sanitize_console_text(raw: Optional[str], max_len: int = MAX_TAB_TITLE_CHARS) -> str:
    """ANSIエスケープシーケンスと制御文字を除去し、ログ出力安全な文字列へ正規化する。"""
    cleaned = _ANSI_ESCAPE_RE.sub("", raw or "")
    cleaned = _CONTROL_CHAR_RE.sub(" ", cleaned).strip()
    return cleaned[:max_len]


@dataclass(frozen=True)
class TabBindingInfo:
    """現在ローカルブリッジに紐づけられているブラウザタブの情報を保持する。"""

    tab_id: str
    tab_title: str
    tab_url: str
    bound_at: str


class CommandExecutionTracker:
    """現在実行中のコマンド状態（種別・コマンド文字列・開始時刻）をスレッドセーフに追跡する。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._active: Dict[str, RunningCommandInfo] = {}

    def begin(self, kind: str, command: str, tab_id: str = "") -> str:
        """コマンド実行開始を記録し、終了解除用の一意な実行IDを返す。"""
        kind_raw = kind.value if isinstance(kind, BridgeCommandKind) else str(kind or "")
        safe_kind = sanitize_console_text(kind_raw, MAX_COMMAND_KIND_CHARS) or BridgeCommandKind.RUN_COMMAND.value
        safe_cmd = sanitize_console_text(command, MAX_RUNNING_COMMAND_CHARS)
        safe_tab_id = sanitize_console_text(tab_id, MAX_TAB_ID_CHARS)
        started_at = datetime.now().isoformat(timespec="seconds")
        started_at_ms = int(time.time() * 1000)
        started_monotonic = time.monotonic()
        with self._lock:
            self._seq += 1
            execution_id = f"cmd-{self._seq}"
            self._active[execution_id] = RunningCommandInfo(
                execution_id=execution_id,
                kind=safe_kind,
                command=safe_cmd,
                tab_id=safe_tab_id,
                started_at=started_at,
                started_at_ms=started_at_ms,
                started_monotonic=started_monotonic,
            )
            return execution_id

    def end(self, execution_id: str) -> bool:
        """指定された実行IDのコマンド実行中状態を解除する。"""
        with self._lock:
            return self._active.pop(execution_id, None) is not None

    def clear(self) -> None:
        """追跡中の全実行中状態をクリアする。"""
        with self._lock:
            self._active.clear()

    def get_status(self) -> Dict[str, object]:
        """現在のコマンド実行中サマリーを返す。"""
        with self._lock:
            items = list(self._active.values())
        now_mono = time.monotonic()
        serialized: List[Dict[str, object]] = [item.to_dict(now_monotonic=now_mono) for item in items]
        primary = serialized[0] if serialized else None
        return {
            "running": bool(serialized),
            "count": len(serialized),
            "kind": primary["kind"] if primary is not None else None,
            "command": primary["command"] if primary is not None else "",
            "tab_id": primary["tab_id"] if primary is not None else "",
            "started_at": primary["started_at"] if primary is not None else None,
            "started_at_ms": primary["started_at_ms"] if primary is not None else None,
            "elapsed_ms": primary["elapsed_ms"] if primary is not None else 0,
            "commands": serialized,
        }

class TabBindingManager:
    """ブラウザタブの紐づけ・解除およびコマンド実行時のタブ一致検証をスレッドセーフに管理する。"""

    def __init__(self, project_root: Path, require_binding: bool = False) -> None:
        self.project_root = project_root.resolve()
        self.require_binding = require_binding
        self._lock = threading.Lock()
        self._binding: Optional[TabBindingInfo] = None
        self._execution_tracker = CommandExecutionTracker()

    @classmethod
    def _normalize_tab_id(cls, raw_tab_id: Optional[str]) -> str:
        cleaned = (raw_tab_id or "").strip()
        if not cleaned:
            raise ValueError("tab_id を指定してください")
        if len(cleaned) > MAX_TAB_ID_CHARS:
            raise ValueError("tab_id が長すぎます")
        if any(ord(ch) < 32 or ord(ch) == 127 or (0x80 <= ord(ch) <= 0x9F) for ch in cleaned):
            raise ValueError("tab_id に制御文字を含めることはできません")
        return cleaned

    @classmethod
    def validate_tab_id(cls, raw_tab_id: Optional[str]) -> str:
        """外部から渡されたtab_idを正規化・検証し、不正時はValueErrorを送出する。"""
        return cls._normalize_tab_id(raw_tab_id)

    def _build_info_locked(self) -> Dict[str, object]:
        binding = self._binding
        return {
            "ok": True,
            "bound": binding is not None,
            "tab_id": binding.tab_id if binding is not None else None,
            "tab_title": binding.tab_title if binding is not None else "",
            "tab_url": binding.tab_url if binding is not None else "",
            "bound_at": binding.bound_at if binding is not None else None,
            "project_root": str(self.project_root),
            "project_name": sanitize_console_text(self.project_root.name, MAX_TAB_TITLE_CHARS),
        }

    def _with_execution_status(self, info: Dict[str, object]) -> Dict[str, object]:
        exec_status = self._execution_tracker.get_status()
        info["command_running"] = bool(exec_status["running"])
        info["running_command"] = exec_status
        return info

    def begin_command(self, kind: str, command: str, tab_id: str = "") -> str:
        """実行中コマンドの開始を登録し、実行IDを返す。"""
        return self._execution_tracker.begin(kind=kind, command=command, tab_id=tab_id)

    def end_command(self, execution_id: str) -> bool:
        """指定された実行IDのコマンド実行中状態を解除する。"""
        return self._execution_tracker.end(execution_id)

    def get_execution_status(self) -> Dict[str, object]:
        """現在のコマンド実行中状態を返す。"""
        return self._execution_tracker.get_status()

    def get_info(self) -> Dict[str, object]:
        """現在のタブ紐づけ状態を返す。"""
        with self._lock:
            info = self._build_info_locked()
        return self._with_execution_status(info)

    def bind(self, tab_id: str, tab_title: str = "", tab_url: str = "") -> Dict[str, object]:
        """指定されたブラウザタブをこのブリッジへ紐づける。"""
        normalized_id = self._normalize_tab_id(tab_id)
        info = TabBindingInfo(
            tab_id=normalized_id,
            tab_title=sanitize_console_text(tab_title, MAX_TAB_TITLE_CHARS),
            tab_url=sanitize_console_text(tab_url, MAX_TAB_URL_CHARS),
            bound_at=datetime.now().isoformat(timespec="seconds"),
        )
        with self._lock:
            self._binding = info
            built = self._build_info_locked()
        return self._with_execution_status(built)

    def bind_if_unbound(self, tab_id: str, tab_title: str = "", tab_url: str = "") -> Tuple[bool, Dict[str, object]]:
        """未紐づけの場合のみ指定タブを紐づけ、(今回新規紐づけしたか, 最新状態) を返す。"""
        normalized_id = self._normalize_tab_id(tab_id)
        info = TabBindingInfo(
            tab_id=normalized_id,
            tab_title=sanitize_console_text(tab_title, MAX_TAB_TITLE_CHARS),
            tab_url=sanitize_console_text(tab_url, MAX_TAB_URL_CHARS),
            bound_at=datetime.now().isoformat(timespec="seconds"),
        )
        with self._lock:
            if self._binding is not None:
                built = self._build_info_locked()
                changed = False
            else:
                self._binding = info
                built = self._build_info_locked()
                changed = True
        return changed, self._with_execution_status(built)

    def unbind(self, tab_id: Optional[str] = None) -> Tuple[bool, Dict[str, object]]:
        """紐づけを解除し、(実際に解除されたか, 最新状態) を返す。"""
        target_id = self._normalize_tab_id(tab_id) if tab_id is not None else ""
        with self._lock:
            if self._binding is None:
                built = self._build_info_locked()
                changed = False
            elif target_id and self._binding.tab_id != target_id:
                # 対象tab_id指定時は別タブの解除・終了イベントによる誤解除を防止
                built = self._build_info_locked()
                changed = False
            else:
                self._binding = None
                built = self._build_info_locked()
                changed = True
        return changed, self._with_execution_status(built)

    def validate_execute_payload(self, payload: Dict[str, object]) -> Optional[str]:
        """実行リクエストが紐づけ済みタブと整合するか検証し、不整合時は理由メッセージを返す。"""
        has_tab_id = "tab_id" in payload
        if not has_tab_id and not self.require_binding:
            return None

        raw_tab_id = payload.get("tab_id") if has_tab_id else None
        req_tab_id = raw_tab_id.strip() if isinstance(raw_tab_id, str) else ""
        with self._lock:
            current = self._binding

        if current is None:
            return "タブがVSCodeと紐づけられていません。先に「VSCodeと紐づける」ボタンで紐づけてください"
        if not req_tab_id or req_tab_id != current.tab_id:
            return "別のタブがVSCodeと紐づけられているため実行をスキップしました"
        return None

@contextlib.contextmanager
def bridge_command_scope(
    bridge: Any,
    *,
    kind: str,
    command: str,
    tab_id: str = "",
) -> Iterator[Optional[str]]:
    """コマンド実行中状態をスコープ開始時に登録し、終了時に必ず解除するコンテキストマネージャ。"""
    begin_fn = getattr(bridge, "begin_command", None)
    end_fn = getattr(bridge, "end_command", None)
    if not callable(begin_fn) or not callable(end_fn):
        binding_manager = getattr(bridge, "_binding_manager", None)
        begin_fn = getattr(binding_manager, "begin_command", None)
        end_fn = getattr(binding_manager, "end_command", None)
    execution_id: Optional[str] = None
    if callable(begin_fn):
        execution_id = begin_fn(kind=kind, command=command, tab_id=tab_id)
    try:
        yield execution_id
    finally:
        if execution_id is not None and callable(end_fn):
            end_fn(execution_id)
