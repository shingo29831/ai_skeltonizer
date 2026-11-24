"""Module: @role: パッチ出力検知時の応答・要約バンドル統合および新規タブへの引き継ぎ状態管理を担当する。"""
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from ..config import SkeletonConfig, load_bundle_filename_from_meta
from .patch_parser import DELIMITER_END, DELIMITER_MID, DELIMITER_START, has_patch_delimiters

MAX_HANDOFF_PAYLOAD_BYTES = 4 * 1024 * 1024
HANDOFF_TTL_SEC = 120.0
MAX_PENDING_HANDOFFS = 20
_DEFAULT_BUNDLE_FILENAMES = (
    "architecture_bundle.txt",
    "architecture_bundle.md",
    "architecture_bundle.xml",
)
_COMMIT_TAG_OPEN = "<" + "commit>"
_PATCH_STRUCTURE_HINT_RE = re.compile(
    r"^[ \t]*(?:ファイルパス|File(?:\s+path)?|Path|削除)[ \t]*[:：]|^[ \t]*"
    + re.escape(_COMMIT_TAG_OPEN)
    + r"[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


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

    def to_dict(self) -> Dict[str, object]:
        return {
            "handoff_id": self.handoff_id,
            "source_tab_id": self.source_tab_id,
            "new_chat_url": self.new_chat_url,
            "response_text": self.response_text,
            "summary_text": self.summary_text,
            "combined_text": self.combined_text,
            "assigned_tab_id": self.assigned_tab_id,
        }


def has_patch_delimiters_in_text(text: str) -> bool:
    """テキスト内にaiskelパッチ形式（<<<< ==== >>>> の順序ペアとヘッダ/commit構造）が含まれるか判定する。"""
    if not text or not text.strip() or not has_patch_delimiters(text):
        return False
    if not _PATCH_STRUCTURE_HINT_RE.search(text):
        return False
    lines = text.splitlines()
    total = len(lines)
    i = 0
    while i < total:
        if DELIMITER_START.match(lines[i]):
            has_mid = False
            for j in range(i + 1, total):
                if DELIMITER_START.match(lines[j]):
                    break
                if DELIMITER_MID.match(lines[j]):
                    has_mid = True
                    continue
                if DELIMITER_END.match(lines[j]):
                    if has_mid:
                        return True
                    break
        i += 1
    return False


def format_handoff_prompt_text(response_text: str, summary_text: str) -> str:
    """コピーしたAI応答と要約バンドルを新規タブ貼り付け用テキストへ統合する。"""
    resp = (response_text or "").strip()
    summary = (summary_text or "").strip()
    if resp and summary:
        return f"{resp}\n\n{summary}"
    return resp or summary


def _find_existing_bundle_file(project_root: Path, output_dir: Path) -> Optional[Path]:
    configured = load_bundle_filename_from_meta(project_root, output_dir)
    if configured:
        candidate = output_dir / configured
        if candidate.is_file():
            return candidate
        if configured != _DEFAULT_BUNDLE_FILENAMES[0]:
            return None
    for name in _DEFAULT_BUNDLE_FILENAMES:
        candidate = output_dir / name
        if candidate.is_file():
            return candidate
    return None


def load_project_summary_text(project_root: Path) -> str:
    """対象プロジェクトを差分同期し、最新の要約バンドル文字列を取得する。"""
    # 循環インポート回避のためcli_syncを遅延参照し、.gitignore更新・config初期化・最新バンドル生成を統一実行
    from ..cli_sync import resolve_output_dir, run_sync

    resolved_root = project_root.resolve()
    output_dir = resolve_output_dir(resolved_root, None)
    configured_name = load_bundle_filename_from_meta(resolved_root, output_dir)
    config = SkeletonConfig(
        create_bundle=True,
        bundle_format="txt",
        bundle_filename=configured_name,
    )
    _, _, _, bundle_path = run_sync(
        resolved_root,
        output_dir,
        config,
        force=False,
        quiet=True,
        check_bloat=False,
    )
    if bundle_path is None or not bundle_path.is_file():
        bundle_path = _find_existing_bundle_file(resolved_root, output_dir)
    if bundle_path is None or not bundle_path.is_file():
        _, _, _, bundle_path = run_sync(
            resolved_root,
            output_dir,
            config,
            force=True,
            quiet=True,
            check_bloat=False,
        )

    if bundle_path is None or not bundle_path.is_file():
        raise FileNotFoundError("要約バンドルファイルが見つかりません")

    content = bundle_path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("要約バンドルファイルが空です")
    return content


class PatchHandoffManager:
    """パッチ検知時の新規タブ引き継ぎペイロードをTTL付きでスレッドセーフに管理する。"""

    def __init__(self, ttl_sec: float = HANDOFF_TTL_SEC) -> None:
        self._ttl_sec = ttl_sec
        self._lock = threading.Lock()
        self._entries: Dict[str, TabHandoffPayload] = {}

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            hid for hid, item in self._entries.items()
            if (now - item.created_at) > self._ttl_sec
        ]
        for hid in expired:
            self._entries.pop(hid, None)
        while len(self._entries) > MAX_PENDING_HANDOFFS:
            oldest_id = next(iter(self._entries))
            self._entries.pop(oldest_id, None)

    def create(
        self,
        source_tab_id: str,
        new_chat_url: str,
        response_text: str,
        summary_text: str,
    ) -> TabHandoffPayload:
        """新規引き継ぎレコードを生成して登録する。"""
        now = time.monotonic()
        handoff_id = secrets.token_urlsafe(16)
        combined = format_handoff_prompt_text(response_text, summary_text)
        item = TabHandoffPayload(
            handoff_id=handoff_id,
            source_tab_id=source_tab_id,
            new_chat_url=new_chat_url,
            response_text=response_text,
            summary_text=summary_text,
            combined_text=combined,
            created_at=now,
        )
        with self._lock:
            self._prune_expired_locked(now)
            self._entries[handoff_id] = item
        return item

    def assign_tab(self, handoff_id: str, tab_id: str) -> Optional[TabHandoffPayload]:
        """指定引き継ぎIDに新規生成タブのtab_idを関連付ける。"""
        clean_hid = (handoff_id or "").strip()
        clean_tid = (tab_id or "").strip()
        if not clean_hid or not clean_tid:
            return None
        now = time.monotonic()
        with self._lock:
            self._prune_expired_locked(now)
            existing = self._entries.get(clean_hid)
            if existing is None:
                return None
            updated = TabHandoffPayload(
                handoff_id=existing.handoff_id,
                source_tab_id=existing.source_tab_id,
                new_chat_url=existing.new_chat_url,
                response_text=existing.response_text,
                summary_text=existing.summary_text,
                combined_text=existing.combined_text,
                created_at=existing.created_at,
                assigned_tab_id=clean_tid,
            )
            self._entries[clean_hid] = updated
            return updated

    def claim(self, handoff_id: str = "", tab_id: str = "") -> Optional[TabHandoffPayload]:
        """handoff_idまたは割当済みtab_idに一致する未完了引き継ぎを返す(リダイレクト耐性のためackまで保持)。"""
        clean_hid = (handoff_id or "").strip()
        clean_tid = (tab_id or "").strip()
        now = time.monotonic()
        with self._lock:
            self._prune_expired_locked(now)
            if clean_hid:
                item = self._entries.get(clean_hid)
                if item is None:
                    return None
                if item.assigned_tab_id and clean_tid and item.assigned_tab_id != clean_tid:
                    return None
                if not item.assigned_tab_id and clean_tid:
                    item = TabHandoffPayload(
                        handoff_id=item.handoff_id,
                        source_tab_id=item.source_tab_id,
                        new_chat_url=item.new_chat_url,
                        response_text=item.response_text,
                        summary_text=item.summary_text,
                        combined_text=item.combined_text,
                        created_at=item.created_at,
                        assigned_tab_id=clean_tid,
                    )
                    self._entries[clean_hid] = item
                return item
            if clean_tid:
                for item in reversed(list(self._entries.values())):
                    if item.assigned_tab_id == clean_tid:
                        return item
        return None

    def ack(self, handoff_id: str = "", tab_id: str = "") -> bool:
        """貼り付け完了済みの引き継ぎレコードを確定削除する。"""
        clean_hid = (handoff_id or "").strip()
        clean_tid = (tab_id or "").strip()
        with self._lock:
            if clean_hid and clean_hid in self._entries:
                item = self._entries[clean_hid]
                if not item.assigned_tab_id or not clean_tid or item.assigned_tab_id == clean_tid:
                    self._entries.pop(clean_hid, None)
                    return True
                return False
            if clean_tid:
                removed = False
                for hid in list(self._entries.keys()):
                    if self._entries[hid].assigned_tab_id == clean_tid:
                        self._entries.pop(hid, None)
                        removed = True
                return removed
        return False
