"""Module: @role: ブラウザタブとローカルブリッジの紐づけ状態管理およびタブ検証ロジックを提供する。"""
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

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


class TabBindingManager:
    """ブラウザタブの紐づけ・解除およびコマンド実行時のタブ一致検証をスレッドセーフに管理する。"""

    def __init__(self, project_root: Path, require_binding: bool = False) -> None:
        self.project_root = project_root.resolve()
        self.require_binding = require_binding
        self._lock = threading.Lock()
        self._binding: Optional[TabBindingInfo] = None

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

    def get_info(self) -> Dict[str, object]:
        """現在のタブ紐づけ状態を返す。"""
        with self._lock:
            return self._build_info_locked()

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
            return self._build_info_locked()

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
                return False, self._build_info_locked()
            self._binding = info
            return True, self._build_info_locked()

    def unbind(self, tab_id: Optional[str] = None) -> Tuple[bool, Dict[str, object]]:
        """紐づけを解除し、(実際に解除されたか, 最新状態) を返す。"""
        target_id = self._normalize_tab_id(tab_id) if tab_id is not None else ""
        with self._lock:
            if self._binding is None:
                return False, self._build_info_locked()
            # 対象tab_id指定時は別タブの解除・終了イベントによる誤解除を防止
            if target_id and self._binding.tab_id != target_id:
                return False, self._build_info_locked()
            self._binding = None
            return True, self._build_info_locked()

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
