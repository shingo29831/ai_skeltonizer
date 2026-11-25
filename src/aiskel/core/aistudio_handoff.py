"""Module: @role: パッチ出力検知時の応答・要約バンドル統合および新規タブへの引き継ぎ状態管理を担当する。"""
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from ..config import SkeletonConfig, load_bundle_filename_from_meta
from .aistudio_prompts import PATCH_HANDOFF_STOP_SIGNAL
from .patch_parser import DELIMITER_END, DELIMITER_MID, DELIMITER_START, has_patch_delimiters

MAX_HANDOFF_PAYLOAD_BYTES = 4 * 1024 * 1024
HANDOFF_TTL_SEC = 120.0
MAX_PENDING_HANDOFFS = 20
MAX_TRACKED_AUTO_TABS = 512
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
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_STOP_SIGNAL_LINE_RE = re.compile(
    r"^[ \t]*`?" + re.escape(PATCH_HANDOFF_STOP_SIGNAL) + r"`?[ \t。.]*$"
)


class HandoffKind(str, Enum):
    """新規タブ引き継ぎの用途種別を定義する。"""

    PATCH_REVIEW = "patch_review"
    FILE_SPLIT = "file_split"

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
        }


def _find_valid_patch_line_indices(lines: List[str]) -> Set[int]:
    excluded: Set[int] = set()
    total = len(lines)
    i = 0
    while i < total:
        if DELIMITER_START.match(lines[i]):
            has_mid = False
            end_idx = -1
            for j in range(i + 1, total):
                if DELIMITER_START.match(lines[j]):
                    break
                if DELIMITER_MID.match(lines[j]):
                    has_mid = True
                    continue
                if DELIMITER_END.match(lines[j]):
                    if has_mid:
                        end_idx = j
                    break
            if end_idx != -1:
                excluded.update(range(i, end_idx + 1))
                i = end_idx + 1
                continue
        i += 1
    return excluded


def _find_excluded_stop_signal_line_indices(lines: List[str]) -> Set[int]:
    # パッチ内自己言及やコードブロック内例示による誤停止を防ぐため対象範囲を除外
    excluded = _find_valid_patch_line_indices(lines)
    active_fence: Optional[Tuple[str, int]] = None
    for idx, line in enumerate(lines):
        if active_fence is not None:
            excluded.add(idx)
            fence_char, fence_len = active_fence
            stripped = line.strip()
            if len(stripped) >= fence_len and set(stripped) == {fence_char}:
                active_fence = None
            continue
        match = _FENCE_OPEN_RE.match(line)
        if match:
            marker, info = match.group(1), match.group(2)
            if marker[0] == "~" or "`" not in info:
                active_fence = (marker[0], len(marker))
                excluded.add(idx)
    return excluded


def has_patch_delimiters_in_text(text: str) -> bool:
    """テキスト内にaiskelパッチ形式（<<<< ==== >>>> の順序ペアとヘッダ/commit構造）が含まれるか判定する。"""
    if not text or not text.strip() or not has_patch_delimiters(text):
        return False
    if not _PATCH_STRUCTURE_HINT_RE.search(text):
        return False
    return bool(_find_valid_patch_line_indices(text.splitlines()))


def has_handoff_stop_signal_in_text(text: str) -> bool:
    """応答テキストのコードブロック・パッチ外に新規タブ作成停止合図が含まれるか判定する。"""
    if not text or not text.strip() or PATCH_HANDOFF_STOP_SIGNAL not in text:
        return False
    lines = text.splitlines()
    excluded_indices = _find_excluded_stop_signal_line_indices(lines)
    for idx, line in enumerate(lines):
        if idx in excluded_indices:
            continue
        if _STOP_SIGNAL_LINE_RE.match(line):
            return True
    return False


def _wrap_proposed_implementation(resp: str) -> str:
    # 応答内の同名閉じタグによる境界破壊を防ぐため衝突時は動的タグ名へ切り替える
    tag = "proposed_implementation"
    if re.search(rf"</?{tag}\b", resp, re.IGNORECASE):
        idx = 1
        while re.search(rf"</?{tag}_{idx}\b", resp, re.IGNORECASE):
            idx += 1
        tag = f"{tag}_{idx}"
    return f"<{tag}>\n{resp}\n</{tag}>"


def format_handoff_prompt_text(
    response_text: str,
    summary_text: str,
    review_prompt: Optional[str] = None,
) -> str:
    """コピーしたAI応答・要約バンドル・任意のレビュー指示を新規タブ貼り付け用テキストへ統合する。"""
    resp = (response_text or "").strip()
    summary = (summary_text or "").strip()
    if not resp and not summary:
        return ""
    prompt = (review_prompt or "").strip()
    if not prompt:
        if resp and summary:
            return f"{resp}\n\n{summary}"
        return resp or summary
    sections = [prompt]
    if resp:
        sections.append(_wrap_proposed_implementation(resp))
    if summary:
        sections.append(summary)
    return "\n\n".join(sections)


def format_summary_prompt_text(prompt_text: str, summary_text: str) -> str:
    """プロンプト指示とプロジェクト要約バンドルをチャット欄挿入用テキストへ統合する。"""
    prompt = (prompt_text or "").strip()
    summary = (summary_text or "").strip()
    if prompt and summary:
        return f"{prompt}\n\n{summary}"
    return prompt or summary


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
        self._auto_tab_remaining: "OrderedDict[str, int]" = OrderedDict()
        # 上限を下げた後に再度上げた際の使用済み回数超過を維持するため符号付き残高を保持
        self._auto_tab_balance: Dict[str, int] = {}
        self._entry_balances: Dict[str, int] = {}
        self._tab_patches: "OrderedDict[str, str]" = OrderedDict()
        self._tab_lineage: Dict[str, str] = {}
        self._last_detected_patch: Optional[str] = None

    def _record_tab_patch_locked(self, tab_id: str, patch_text: str, overwrite: bool = True) -> None:
        clean_tid = (tab_id or "").strip()
        if not clean_tid or not patch_text or not patch_text.strip():
            return
        if not overwrite and clean_tid in self._tab_patches:
            return
        self._tab_patches[clean_tid] = patch_text
        self._tab_patches.move_to_end(clean_tid)
        while len(self._tab_patches) > MAX_TRACKED_AUTO_TABS:
            evicted_tid, _ = self._tab_patches.popitem(last=False)
            self._tab_lineage.pop(evicted_tid, None)

    def _record_lineage_locked(self, child_tab_id: str, source_tab_id: str) -> None:
        clean_child = (child_tab_id or "").strip()
        clean_source = (source_tab_id or "").strip()
        if clean_child and clean_source and clean_child != clean_source:
            self._tab_lineage[clean_child] = clean_source

    def record_detected_patch(self, patch_text: str, tab_id: str = "") -> None:
        """最後に検知した置換コード応答をタブ紐づけと合わせて保持する。"""
        if not patch_text or not patch_text.strip():
            return
        with self._lock:
            self._last_detected_patch = patch_text
            if tab_id:
                self._record_tab_patch_locked(tab_id, patch_text, overwrite=True)

    def get_last_detected_patch(self, tab_id: str = "") -> Optional[str]:
        """指定タブに引き継がれた置換コード、またはブリッジ全体で最後に検知した置換コードを返す。"""
        clean_tid = (tab_id or "").strip()
        with self._lock:
            if clean_tid and clean_tid in self._tab_patches:
                return self._tab_patches[clean_tid]
            return self._last_detected_patch

    def consume_detected_patch(self, tab_id: str = "", patch_text: Optional[str] = None) -> None:
        """適用完了した置換コードを保持状態から解放し、後続ターンでの二重適用を防止する。"""
        clean_tid = (tab_id or "").strip()
        with self._lock:
            seed_tids: Set[str] = set()
            if clean_tid:
                seed_tids.add(clean_tid)
            if patch_text is not None:
                for tid, stored in self._tab_patches.items():
                    if stored == patch_text:
                        seed_tids.add(tid)

            # 引き継ぎ先タブで統合パッチ適用時、親タブに残る旧パッチも同時に無効化する
            target_tids: Set[str] = set()
            stack = list(seed_tids)
            while stack:
                cur = stack.pop()
                if not cur or cur in target_tids:
                    continue
                target_tids.add(cur)
                parent = self._tab_lineage.get(cur)
                if parent and parent not in target_tids:
                    stack.append(parent)

            removed_patches: Set[str] = set()
            for tid in target_tids:
                removed = self._tab_patches.pop(tid, None)
                if removed is not None:
                    removed_patches.add(removed)
                self._tab_lineage.pop(tid, None)

            if patch_text is not None:
                removed_patches.add(patch_text)

            if self._last_detected_patch is not None:
                if self._last_detected_patch in removed_patches or (
                    patch_text is None and not clean_tid and not removed_patches
                ):
                    self._last_detected_patch = None

    def clear_tab_patch(self, tab_id: str = "") -> None:
        """指定タブおよび系譜上・フォールバック保持に残る旧パッチをすべてクリアする。"""
        self.consume_detected_patch(tab_id=tab_id, patch_text=None)
        with self._lock:
            self._last_detected_patch = None

    def _record_auto_tab_locked(self, tab_id: str, remaining: int, balance: Optional[int] = None) -> None:
        clean_tid = (tab_id or "").strip()
        if not clean_tid:
            return
        clamped = max(0, int(remaining))
        self._auto_tab_remaining[clean_tid] = clamped
        self._auto_tab_balance[clean_tid] = int(balance) if balance is not None else clamped
        self._auto_tab_remaining.move_to_end(clean_tid)
        while len(self._auto_tab_remaining) > MAX_TRACKED_AUTO_TABS:
            evicted_tid, _ = self._auto_tab_remaining.popitem(last=False)
            self._auto_tab_balance.pop(evicted_tid, None)

    def get_remaining_auto_tabs(self, tab_id: str, default_limit: int) -> int:
        """対象タブの残り自動生成回数を返す（自動生成タブ以外は設定値を返す）。"""
        clean_tid = (tab_id or "").strip()
        with self._lock:
            if clean_tid and clean_tid in self._auto_tab_remaining:
                return self._auto_tab_remaining[clean_tid]
        return max(0, int(default_limit))

    def is_auto_generated_tab(self, tab_id: str) -> bool:
        """対象タブがパッチ引き継ぎにより自動生成されたタブか判定する。"""
        clean_tid = (tab_id or "").strip()
        if not clean_tid:
            return False
        with self._lock:
            return clean_tid in self._auto_tab_remaining

    def consume_auto_tab_if_tracked(self, tab_id: str, next_remaining: int) -> None:
        """自動生成タブとして追跡中の場合のみ残り自動生成回数を更新する。"""
        clean_tid = (tab_id or "").strip()
        if not clean_tid:
            return
        with self._lock:
            if clean_tid in self._auto_tab_remaining:
                self._record_auto_tab_locked(clean_tid, next_remaining)

    def adjust_auto_tab_limits(self, old_limit: int, new_limit: int) -> None:
        """上限設定変更の差分を追跡中タブおよび待機中引き継ぎの残り回数へ整合反映する。"""
        delta = int(new_limit) - int(old_limit)
        if delta == 0:
            return
        with self._lock:
            for tid in list(self._auto_tab_remaining.keys()):
                prev_balance = self._auto_tab_balance.get(tid, self._auto_tab_remaining[tid])
                next_balance = prev_balance + delta
                self._auto_tab_balance[tid] = next_balance
                self._auto_tab_remaining[tid] = max(0, next_balance)
            for hid, item in list(self._entries.items()):
                prev_entry_balance = self._entry_balances.get(hid, item.remaining_auto_tabs)
                next_entry_balance = prev_entry_balance + delta
                self._entry_balances[hid] = next_entry_balance
                next_rem = max(0, next_entry_balance)
                self._entries[hid] = TabHandoffPayload(
                    handoff_id=item.handoff_id,
                    source_tab_id=item.source_tab_id,
                    new_chat_url=item.new_chat_url,
                    response_text=item.response_text,
                    summary_text=item.summary_text,
                    combined_text=item.combined_text,
                    created_at=item.created_at,
                    assigned_tab_id=item.assigned_tab_id,
                    remaining_auto_tabs=next_rem,
                    handoff_kind=item.handoff_kind,
                    target_file=item.target_file,
                )

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            hid for hid, item in self._entries.items()
            if (now - item.created_at) > self._ttl_sec
        ]
        for hid in expired:
            self._entries.pop(hid, None)
            self._entry_balances.pop(hid, None)
        while len(self._entries) > MAX_PENDING_HANDOFFS:
            oldest_id = next(iter(self._entries))
            self._entries.pop(oldest_id, None)
            self._entry_balances.pop(oldest_id, None)

    def create(
        self,
        source_tab_id: str,
        new_chat_url: str,
        response_text: str,
        summary_text: str,
        review_prompt: Optional[str] = None,
        remaining_auto_tabs: int = 0,
        handoff_kind: Union[HandoffKind, str] = HandoffKind.PATCH_REVIEW,
        target_file: str = "",
        raw_combined_text: Optional[str] = None,
    ) -> TabHandoffPayload:
        """新規引き継ぎレコードを生成して登録する。"""
        now = time.monotonic()
        handoff_id = secrets.token_urlsafe(16)
        combined = (
            raw_combined_text
            if raw_combined_text is not None
            else format_handoff_prompt_text(response_text, summary_text, review_prompt=review_prompt)
        )
        kind_val = (
            handoff_kind.value
            if isinstance(handoff_kind, HandoffKind)
            else (str(handoff_kind or "").strip() or HandoffKind.PATCH_REVIEW.value)
        )
        item = TabHandoffPayload(
            handoff_id=handoff_id,
            source_tab_id=source_tab_id,
            new_chat_url=new_chat_url,
            response_text=response_text,
            summary_text=summary_text,
            combined_text=combined,
            created_at=now,
            remaining_auto_tabs=max(0, int(remaining_auto_tabs)),
            handoff_kind=kind_val,
            target_file=target_file,
        )
        with self._lock:
            self._prune_expired_locked(now)
            self._entries[handoff_id] = item
            self._entry_balances[handoff_id] = item.remaining_auto_tabs
        return item

    def _record_claimed_tab_metadata_locked(self, tab_id: str, item: TabHandoffPayload) -> None:
        self._record_auto_tab_locked(
            tab_id,
            item.remaining_auto_tabs,
            balance=self._entry_balances.get(item.handoff_id, item.remaining_auto_tabs),
        )
        if item.response_text.strip():
            self._record_tab_patch_locked(tab_id, item.response_text, overwrite=True)
        # 新規分割タスク開始時は旧ファイルのパッチ系譜を引き継がない
        if item.handoff_kind != HandoffKind.FILE_SPLIT.value:
            self._record_lineage_locked(tab_id, item.source_tab_id)

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
                remaining_auto_tabs=existing.remaining_auto_tabs,
                handoff_kind=existing.handoff_kind,
                target_file=existing.target_file,
            )
            self._entries[clean_hid] = updated
            # unbind・リロード後も自動生成タブの残り回数を保持するため記録
            self._record_claimed_tab_metadata_locked(clean_tid, updated)
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
                        remaining_auto_tabs=item.remaining_auto_tabs,
                        handoff_kind=item.handoff_kind,
                        target_file=item.target_file,
                    )
                    self._entries[clean_hid] = item
                if clean_tid:
                    self._record_claimed_tab_metadata_locked(clean_tid, item)
                return item
            if clean_tid:
                for item in reversed(list(self._entries.values())):
                    if item.assigned_tab_id == clean_tid:
                        self._record_claimed_tab_metadata_locked(clean_tid, item)
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
                    self._entry_balances.pop(clean_hid, None)
                    return True
                return False
            if clean_tid:
                removed = False
                for hid in list(self._entries.keys()):
                    if self._entries[hid].assigned_tab_id == clean_tid:
                        self._entries.pop(hid, None)
                        self._entry_balances.pop(hid, None)
                        removed = True
                return removed
        return False
