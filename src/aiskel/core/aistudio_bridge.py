"""Module: @role: ブラウザ監視クライアントからの通知を受信し、検証済みaiskelコマンドをメインスレッド上で安全に自動実行するローカルHTTPブリッジを提供する。"""
import contextlib
import io
import ipaddress
import queue
import socket
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, TextIO, Tuple
from urllib.parse import urlsplit

from ..config import DEFAULT_PATCH_HANDOFF_MAX_TABS, load_patch_handoff_max_tabs_from_meta
from .aistudio_auto_apply import execute_auto_apply_patch
from .aistudio_auto_split import (
    build_split_combined_text,
    collect_bloat_targets_sorted_by_size,
    format_bloat_targets_payload,
    validate_split_target_request,
)
from .aistudio_bridge_handler import build_bridge_request_handler_class
from .aistudio_bridge_state import TabBindingManager
from .aistudio_command import (
    CommandValidationError,
    build_safe_copy_argv,
    extract_aiskel_copy_commands,
)
from .aistudio_handoff import (
    MAX_HANDOFF_PAYLOAD_BYTES,
    HandoffKind,
    PatchHandoffManager,
    format_summary_prompt_text,
    has_handoff_stop_signal_in_text,
    has_patch_delimiters_in_text,
    load_project_summary_text,
)
from .aistudio_prompts import (
    PATCH_HANDOFF_STOP_SIGNAL,
    PromptPattern,
    list_prompt_patterns,
    load_prompt_for_pattern,
)
from .browser_paste import trigger_browser_prompt_run
from .clipboard import set_clipboard_text
from .aistudio_extension import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    export_browser_extension,
    format_host_for_url,
    get_default_extension_dir,
    get_firefox_extension_dir,
    is_loopback_host,
    load_or_create_bridge_token,
    normalize_bind_host,
    resolve_client_host,
)

MAX_PAYLOAD_BYTES = 65536
MAX_DEDUP_HISTORY = 100
EXECUTION_WAIT_TIMEOUT_SEC = 120.0
SOCKET_TIMEOUT_SEC = 10.0
MAX_ERROR_DETAIL_CHARS = 240
MAX_PATCH_HANDOFF_MAX_TABS = 999
ALLOWED_EXTENSION_SCHEMES = frozenset({"chrome-extension", "moz-extension", "edge-extension"})


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _is_ipv6_literal(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


@dataclass(frozen=True)
class BridgeExecutionResult:
    """ブリッジ経由コマンド実行の結果を保持する。"""

    ok: bool
    exit_code: int
    command: str
    argv: Tuple[str, ...]
    message: str
    skipped: bool = False
    pasted: bool = False
    auto_run: bool = False
    auto_run_allowed: bool = False
    paste_reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "command": self.command,
            "message": self.message,
            "skipped": self.skipped,
            "pasted": self.pasted,
            "auto_run": self.auto_run,
            "auto_run_allowed": self.auto_run_allowed,
            "paste_reason": self.paste_reason,
        }


@dataclass
class _ExecutionJob:
    payload: Dict[str, object]
    action: str = "execute"
    done_event: threading.Event = field(default_factory=threading.Event)
    status_code: int = int(HTTPStatus.INTERNAL_SERVER_ERROR)
    result: Any = None
    error: Optional[BaseException] = None


class _TeeTextIO:
    """元のstderrへリアルタイム出力しつつ、ブラウザ通知用にエラー内容を同時捕捉するストリーム。"""

    def __init__(self, wrapped: TextIO) -> None:
        self._wrapped = wrapped
        self._buffer = io.StringIO()

    def write(self, text: str) -> int:
        self._buffer.write(text)
        return self._wrapped.write(text)

    def flush(self) -> None:
        self._wrapped.flush()

    def isatty(self) -> bool:
        return getattr(self._wrapped, "isatty", lambda: False)()

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)

    def getvalue(self) -> str:
        return self._buffer.getvalue()


def _normalize_system_exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return int(exc.code)
    return 1


def _extract_error_detail(stderr_text: str) -> str:
    lines: List[str] = []
    for raw in stderr_text.splitlines():
        cleaned = raw.strip()
        if not cleaned or cleaned.startswith("📋"):
            continue
        lines.append(cleaned)
    detail = " / ".join(lines)
    if len(detail) > MAX_ERROR_DETAIL_CHARS:
        return detail[: MAX_ERROR_DETAIL_CHARS - 3] + "..."
    return detail


def is_allowed_origin(origin: Optional[str], allow_missing: bool = False) -> bool:
    """OriginヘッダがGoogle AI Studioまたはブラウザ拡張機能に厳密一致するか判定する。"""
    normalized = (origin or "").strip()
    # Userscript等のサンドボックス環境から送られるOrigin: nullはトークン認証済み時のみ許可
    if not normalized or normalized.lower() == "null":
        return allow_missing

    parsed = urlsplit(normalized)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False

    scheme = parsed.scheme.lower()
    if scheme == "https":
        if parsed.username is not None or parsed.password is not None:
            return False
        return (parsed.hostname or "").lower() == "aistudio.google.com" and parsed.port in (None, 443)

    if scheme in ALLOWED_EXTENSION_SCHEMES:
        return bool(parsed.netloc)

    return False


class AiskelBridgeServer:
    """Google AI Studio監視クライアントからの通知を受理し、メインスレッドでaiskel cを自動実行するHTTPサーバー。"""

    def __init__(
        self,
        project_root: Path,
        command_runner: Callable[[List[str]], int],
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        token: Optional[str] = None,
        no_focus: bool = False,
        no_paste: bool = False,
        require_tab_binding: bool = False,
        auto_run: Optional[bool] = None,
        structured_command_runner: Optional[Callable[..., Any]] = None,
        clipboard_writer: Optional[Callable[[str], None]] = None,
        prompt_run_trigger: Optional[Callable[..., Any]] = None,
        output_dir: Optional[Path] = None,
        patch_handoff_max_tabs: Optional[int] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.output_dir = output_dir.resolve() if output_dir is not None else None
        self._explicit_patch_handoff_max_tabs: Optional[int] = (
            max(0, min(MAX_PATCH_HANDOFF_MAX_TABS, int(patch_handoff_max_tabs)))
            if patch_handoff_max_tabs is not None and not isinstance(patch_handoff_max_tabs, bool)
            else None
        )
        self.command_runner = command_runner
        self.structured_command_runner = structured_command_runner
        self.clipboard_writer: Callable[[str], None] = clipboard_writer or set_clipboard_text
        self.prompt_run_trigger: Callable[..., Any] = prompt_run_trigger or trigger_browser_prompt_run
        self.host = host
        self.port = port
        self._token: Optional[str] = None
        self.token = token
        self.no_focus = no_focus
        self.no_paste = no_paste
        if no_focus or no_paste or auto_run is False:
            self._auto_run_mode = "disabled"
        elif auto_run is True:
            self._auto_run_mode = "enabled"
        else:
            self._auto_run_mode = "manual"
        self._binding_manager = TabBindingManager(self.project_root, require_binding=require_tab_binding)
        self._handoff_manager = PatchHandoffManager()
        self._exec_lock = threading.Lock()
        self._queue_state_lock = threading.Lock()
        self._recent_keys: Deque[str] = deque(maxlen=MAX_DEDUP_HISTORY)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._job_queue: "queue.Queue[_ExecutionJob]" = queue.Queue()
        self._stop_event = threading.Event()
        self._use_main_thread_queue = False
        self._shutting_down = False
        self._startup_ready_in_progress = False

    @staticmethod
    def _normalize_token(token: Optional[str]) -> Optional[str]:
        if token is None:
            return None
        cleaned = token.strip()
        return cleaned if cleaned else None

    @property
    def token(self) -> Optional[str]:
        return self._token

    @token.setter
    def token(self, value: Optional[str]) -> None:
        self._token = self._normalize_token(value)

    @property
    def server_address(self) -> Tuple[str, int]:
        if self._httpd is not None:
            addr = self._httpd.server_address
            return str(addr[0]), int(addr[1])
        return normalize_bind_host(self.host), self.port

    def get_patch_handoff_max_tabs(self) -> int:
        """非自動生成タブにおけるパッチ検知時の新規タブ自動作成上限回数を返す。"""
        if self._explicit_patch_handoff_max_tabs is not None:
            return self._explicit_patch_handoff_max_tabs
        configured = load_patch_handoff_max_tabs_from_meta(self.project_root, self.output_dir)
        if configured is not None:
            return max(0, min(MAX_PATCH_HANDOFF_MAX_TABS, configured))
        return DEFAULT_PATCH_HANDOFF_MAX_TABS

    def set_patch_handoff_max_tabs(self, max_tabs: int) -> Dict[str, object]:
        """パッチ検知時の新規タブ自動作成上限回数を動的に更新し、最新の紐づけ情報を返す。"""
        normalized = max(0, min(MAX_PATCH_HANDOFF_MAX_TABS, int(max_tabs)))
        with self._exec_lock:
            old_limit = self.get_patch_handoff_max_tabs()
            self._explicit_patch_handoff_max_tabs = normalized
            self._handoff_manager.adjust_auto_tab_limits(old_limit, normalized)
            return self.get_binding_info()

    def handle_handoff_max_tabs_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """ブラウザUI等からの新規タブ自動作成上限回数の変更リクエストを検証・反映する。"""
        raw_val = payload.get("patch_handoff_max_tabs")
        if raw_val is None and "max_tabs" in payload:
            raw_val = payload.get("max_tabs")
        if isinstance(raw_val, bool) or not isinstance(raw_val, int):
            return int(HTTPStatus.BAD_REQUEST), {
                "ok": False,
                "message": "patch_handoff_max_tabs を整数で指定してください",
            }
        if raw_val < 0 or raw_val > MAX_PATCH_HANDOFF_MAX_TABS:
            return int(HTTPStatus.BAD_REQUEST), {
                "ok": False,
                "message": f"patch_handoff_max_tabs は 0〜{MAX_PATCH_HANDOFF_MAX_TABS} の範囲で指定してください",
            }
        _, tab_err = self._validate_optional_tab_id(payload, required=False)
        if tab_err is not None:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err}

        info = self.set_patch_handoff_max_tabs(raw_val)
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{timestamp}] ⚙ パッチ自動新タブ上限を変更しました: {raw_val} 回", flush=True)
        return int(HTTPStatus.OK), info

    def _enrich_binding_info(self, info: Dict[str, object]) -> Dict[str, object]:
        enriched = dict(info)
        bound_tab_id = str(enriched.get("tab_id") or "") if enriched.get("bound") else ""
        is_auto_tab = self._handoff_manager.is_auto_generated_tab(bound_tab_id) if bound_tab_id else False
        # 自動生成タブはmanualモードでも自動送信ONで開始する
        enriched["auto_run_default"] = (self._auto_run_mode == "enabled") or (
            is_auto_tab and self._auto_run_mode != "disabled"
        )
        enriched["auto_run_allowed"] = self._auto_run_mode != "disabled"
        max_tabs = self.get_patch_handoff_max_tabs()
        enriched["patch_handoff_max_tabs"] = max_tabs
        if bound_tab_id:
            enriched["remaining_auto_tabs"] = self._handoff_manager.get_remaining_auto_tabs(
                bound_tab_id,
                default_limit=max_tabs,
            )
            enriched["is_auto_generated_tab"] = is_auto_tab
        else:
            enriched["remaining_auto_tabs"] = max_tabs
            enriched["is_auto_generated_tab"] = False
        return enriched

    def get_prompt_payload(self, pattern: str = PromptPattern.DEFAULT.value) -> Tuple[int, Dict[str, object]]:
        """指定パターンに対応する自動貼り付け用プロンプト文字列を返す。"""
        try:
            prompt_text = load_prompt_for_pattern(self.project_root, pattern)
        except ValueError as e:
            return int(HTTPStatus.BAD_REQUEST), {
                "ok": False,
                "message": str(e),
                "available_patterns": list(list_prompt_patterns()),
            }
        normalized = (pattern or "").strip().lower() or PromptPattern.DEFAULT.value
        return int(HTTPStatus.OK), {
            "ok": True,
            "pattern": normalized,
            "prompt": prompt_text,
            "available_patterns": list(list_prompt_patterns()),
        }

    def handle_summary_prompt_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """最新のプロジェクト要約バンドルと指定パターンのプロンプトを取得し統合テキストを返す。"""
        raw_pattern = payload.get("pattern")
        pattern = str(raw_pattern).strip() if isinstance(raw_pattern, str) and raw_pattern.strip() else PromptPattern.DEFAULT.value
        try:
            prompt_text = load_prompt_for_pattern(self.project_root, pattern)
        except ValueError as e:
            return int(HTTPStatus.BAD_REQUEST), {
                "ok": False,
                "message": str(e),
                "available_patterns": list(list_prompt_patterns()),
            }

        with self._exec_lock:
            try:
                summary_text = load_project_summary_text(self.project_root)
            except (OSError, ValueError, RuntimeError) as e:
                return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                    "ok": False,
                    "message": f"要約バンドルの取得に失敗しました: {e}",
                }

        combined_text = format_summary_prompt_text(prompt_text, summary_text)
        return int(HTTPStatus.OK), {
            "ok": True,
            "pattern": pattern.lower(),
            "prompt": prompt_text,
            "summary": summary_text,
            "combined_text": combined_text,
            "available_patterns": list(list_prompt_patterns()),
        }

    def handle_bloat_targets_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """aiskel b 基準でプロジェクト内の肥大化ファイルを検出し、ファイルサイズ降順の一覧を返す。"""
        tab_error = self._binding_manager.validate_execute_payload(payload)
        if tab_error is not None:
            return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

        with self._exec_lock:
            try:
                bloated, max_lines, max_chars = collect_bloat_targets_sorted_by_size(
                    self.project_root,
                    self.output_dir,
                )
            except (OSError, ValueError, RuntimeError) as e:
                return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                    "ok": False,
                    "message": f"肥大化ファイルの検出に失敗しました: {e}",
                }

        timestamp = datetime.now().strftime("%H:%M:%S")
        print(
            f"\n[{timestamp}] 📊 自動分割対象抽出 (aiskel b): {len(bloated)} 件 "
            f"(基準: 行数 >= {max_lines}, 文字数 >= {max_chars:,})",
            flush=True,
        )
        return int(HTTPStatus.OK), format_bloat_targets_payload(
            self.project_root,
            bloated,
            max_lines=max_lines,
            max_chars=max_chars,
        )

    def handle_split_prompt_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """現在の空タブで自動分割を開始するための対象1ファイル分割指示+最新要約テキストを返す。"""
        req_tab_id, tab_err = self._validate_optional_tab_id(payload, required=self._binding_manager.require_binding)
        if tab_err is not None or req_tab_id is None:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

        tab_error = self._binding_manager.validate_execute_payload(payload)
        if tab_error is not None:
            return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

        target_info, target_err = validate_split_target_request(
            self.project_root,
            payload,
            output_dir=self.output_dir,
        )
        if target_err is not None or target_info is None:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": target_err or "対象ファイル指定が不正です"}

        with self._exec_lock:
            try:
                summary_text = load_project_summary_text(self.project_root)
                combined_text = build_split_combined_text(self.project_root, target_info, summary_text)
            except (OSError, ValueError, RuntimeError) as e:
                return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                    "ok": False,
                    "message": f"分割プロンプトの生成に失敗しました: {e}",
                }
            if req_tab_id:
                self._handoff_manager.clear_tab_patch(req_tab_id)
                self._handoff_manager.consume_auto_tab_if_tracked(
                    req_tab_id,
                    self.get_patch_handoff_max_tabs(),
                )
            else:
                self._handoff_manager.clear_tab_patch("")

        timestamp = datetime.now().strftime("%H:%M:%S")
        print(
            f"\n[{timestamp}] ✂ 自動分割開始 ({target_info['file_index']}/{target_info['total_files']}): "
            f"{target_info['rel_path']}",
            flush=True,
        )
        return int(HTTPStatus.OK), {
            "ok": True,
            "target_file": target_info["rel_path"],
            "combined_text": combined_text,
        }

    def handle_split_handoff_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """次の分割対象ファイルを新しいチャットタブへ最新要約とともに引き継ぐレコードを作成する。"""
        req_tab_id, tab_err = self._validate_optional_tab_id(payload, required=self._binding_manager.require_binding)
        if tab_err is not None or req_tab_id is None:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

        tab_error = self._binding_manager.validate_execute_payload(payload)
        if tab_error is not None:
            return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

        target_info, target_err = validate_split_target_request(
            self.project_root,
            payload,
            output_dir=self.output_dir,
        )
        if target_err is not None or target_info is None:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": target_err or "対象ファイル指定が不正です"}

        new_chat_url = str(payload.get("new_chat_url") or "https://aistudio.google.com/prompts/new_chat").strip()
        parsed_url = urlsplit(new_chat_url)
        if parsed_url.scheme.lower() != "https" or (parsed_url.hostname or "").lower() != "aistudio.google.com":
            new_chat_url = "https://aistudio.google.com/prompts/new_chat"

        with self._exec_lock:
            try:
                summary_text = load_project_summary_text(self.project_root)
                combined_text = build_split_combined_text(self.project_root, target_info, summary_text)
            except (OSError, ValueError, RuntimeError) as e:
                return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                    "ok": False,
                    "message": f"分割プロンプトの生成に失敗しました: {e}",
                }
            self._handoff_manager.clear_tab_patch(req_tab_id)
            max_tabs = self.get_patch_handoff_max_tabs()
            handoff = self._handoff_manager.create(
                source_tab_id="",
                new_chat_url=new_chat_url,
                response_text="",
                summary_text=summary_text,
                remaining_auto_tabs=max_tabs,
                handoff_kind=HandoffKind.FILE_SPLIT,
                target_file=str(target_info["rel_path"]),
                raw_combined_text=combined_text,
            )

        timestamp = datetime.now().strftime("%H:%M:%S")
        print(
            f"\n[{timestamp}] ✂ 自動分割引き継ぎ ({target_info['file_index']}/{target_info['total_files']}): "
            f"{target_info['rel_path']} -> 新規タブへ移行します (自動新タブ上限: {max_tabs})",
            flush=True,
        )
        return int(HTTPStatus.OK), {
            "ok": True,
            "skipped": False,
            "remaining_auto_tabs": max_tabs,
            "handoff_id": handoff.handoff_id,
            "new_chat_url": handoff.new_chat_url,
            "target_file": target_info["rel_path"],
            "message": "次ファイルの自動分割を新規タブへ引き継ぎます",
        }

    def get_binding_info(self) -> Dict[str, object]:
        """現在のブラウザタブ紐づけ状態を返す。"""
        return self._enrich_binding_info(self._binding_manager.get_info())

    def bind_tab(
        self,
        tab_id: str,
        tab_title: str = "",
        tab_url: str = "",
        handoff_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """指定されたブラウザタブをこのブリッジへ紐づける。"""
        raw_info = self._binding_manager.bind(tab_id=tab_id, tab_title=tab_title, tab_url=tab_url)
        # assign_tabで自動生成タブの残り回数を確定させてからbinding情報を拡張する
        if handoff_id:
            self._handoff_manager.assign_tab(handoff_id, str(raw_info.get("tab_id") or tab_id))
        return self._enrich_binding_info(raw_info)

    def bind_tab_if_unbound(
        self,
        tab_id: str,
        tab_title: str = "",
        tab_url: str = "",
        handoff_id: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, object]]:
        """未紐づけの場合のみブラウザタブを紐づけ、(今回新規紐づけしたか, 紐づけ情報) を返す。"""
        newly_bound, raw_info = self._binding_manager.bind_if_unbound(
            tab_id=tab_id,
            tab_title=tab_title,
            tab_url=tab_url,
        )
        if newly_bound and handoff_id:
            self._handoff_manager.assign_tab(handoff_id, str(raw_info.get("tab_id") or tab_id))
        return newly_bound, self._enrich_binding_info(raw_info)

    def unbind_tab(self, tab_id: Optional[str] = None) -> Tuple[bool, Dict[str, object]]:
        """現在のブラウザタブ紐づけを解除する。"""
        changed, info = self._binding_manager.unbind(tab_id=tab_id)
        return changed, self._enrich_binding_info(info)

    def _resolve_target_title_keywords(self, payload: Dict[str, object]) -> Tuple[str, ...]:
        raw_title = payload.get("tab_title")
        title_str = " ".join(str(raw_title).split()) if isinstance(raw_title, str) else ""
        if not title_str:
            binding_title = self._binding_manager.get_info().get("tab_title")
            title_str = " ".join(str(binding_title).split()) if isinstance(binding_title, str) else ""
        normalized = title_str[:80].strip().lower()
        if len(normalized) >= 4:
            return (normalized,)
        return ("google ai studio", "aistudio")

    def _normalize_incoming_commands(self, payload: Dict[str, object]) -> List[str]:
        raw_commands = payload.get("commands")
        extracted: List[str] = []
        if isinstance(raw_commands, list):
            for item in raw_commands:
                if isinstance(item, str):
                    extracted.extend(extract_aiskel_copy_commands(item))
        elif isinstance(payload.get("command"), str):
            extracted.extend(extract_aiskel_copy_commands(str(payload["command"])))
        elif isinstance(payload.get("response_text"), str):
            extracted.extend(extract_aiskel_copy_commands(str(payload["response_text"])))
        return extracted

    def handle_execute_payload(self, payload: Dict[str, object]) -> Tuple[int, BridgeExecutionResult]:
        """受信JSONペイロードを検証し、安全なaiskel cコマンドを排他実行する。"""
        tab_error = self._binding_manager.validate_execute_payload(payload)
        if tab_error is not None:
            return int(HTTPStatus.CONFLICT), BridgeExecutionResult(
                ok=False,
                exit_code=1,
                command="",
                argv=(),
                message=tab_error,
                skipped=True,
            )

        commands = self._normalize_incoming_commands(payload)
        if not commands:
            return int(HTTPStatus.BAD_REQUEST), BridgeExecutionResult(
                ok=False,
                exit_code=1,
                command="",
                argv=(),
                message="実行可能な aiskel c コマンドが含まれていません",
            )

        # 別ウィンドウ作業中のフォーカス奪取・誤ペースト・クリップボード破壊を防ぐため非フォーカス時は自動無効化
        page_focused = payload.get("page_focused")
        effective_no_focus = self.no_focus or (page_focused is False)
        effective_no_paste = self.no_paste or (page_focused is False)
        # --no-focus起動時でもフォーカス中ならエラー詳細のクリップボード保存を維持するため非フォーカス時のみ抑止
        effective_no_error_clipboard = page_focused is False

        try:
            argv, display_cmd = build_safe_copy_argv(
                commands,
                project_root=self.project_root,
                no_focus=effective_no_focus,
                no_paste=effective_no_paste,
                no_error_clipboard=effective_no_error_clipboard,
            )
        except CommandValidationError as e:
            return int(HTTPStatus.BAD_REQUEST), BridgeExecutionResult(
                ok=False,
                exit_code=1,
                command=" ; ".join(commands),
                argv=(),
                message=str(e),
            )

        turn_id = str(payload.get("turn_id") or "").strip()
        raw_tab_id = payload.get("tab_id")
        req_tab_id = raw_tab_id.strip() if isinstance(raw_tab_id, str) else ""
        # タブ切替直後の同一ターン位置・同一コマンドの誤スキップを防ぐためtab_idで重複キーを分離
        dedup_key = f"{req_tab_id}::{turn_id}::{display_cmd}" if turn_id else ""

        target_title_keywords = self._resolve_target_title_keywords(payload)
        # タブ側が既にフォアグラウンドのときは別ブラウザ・別ウィンドウへのフォーカス奪取を防ぐ
        focus_browser = (not effective_no_focus) and (page_focused is not True)

        with self._exec_lock:
            if dedup_key and dedup_key in self._recent_keys:
                return int(HTTPStatus.OK), BridgeExecutionResult(
                    ok=True,
                    exit_code=0,
                    command=display_cmd,
                    argv=tuple(argv),
                    message="同一ターンのコマンドは実行済みのためスキップしました",
                    skipped=True,
                )

            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{timestamp}] 🤖 AI Studio 推論完了を検知 -> 自動実行: {display_cmd}", flush=True)

            tee_stderr = _TeeTextIO(sys.stderr)
            system_exit_note = ""
            exec_pasted = False
            exec_paste_reason = ""
            with contextlib.redirect_stderr(tee_stderr):
                try:
                    if self.structured_command_runner is not None:
                        raw_res = self.structured_command_runner(
                            argv,
                            focus_browser=focus_browser,
                            allowed_title_keywords=target_title_keywords,
                        )
                    else:
                        raw_res = self.command_runner(argv)

                    if isinstance(raw_res, int):
                        exit_code = int(raw_res)
                    else:
                        exit_code = int(getattr(raw_res, "exit_code", 1))
                        exec_pasted = bool(getattr(raw_res, "pasted", False))
                        exec_paste_reason = str(getattr(raw_res, "paste_reason", "") or "")
                except SystemExit as e:
                    # argparse等のSystemExitでブリッジワーカーループが崩壊するのを防止
                    exit_code = _normalize_system_exit_code(e)
                    if isinstance(e.code, str) and e.code.strip():
                        system_exit_note = e.code.strip()

            if exit_code == 0 and dedup_key:
                self._recent_keys.append(dedup_key)

            # なぜ必要か: aiskel c失敗時に自動コピーされたエラー内容も成功時同様に貼り付け・自動送信可能にするため
            paste_suppressed = effective_no_focus or effective_no_paste
            pasted = (not paste_suppressed) and exec_pasted
            paste_reason = "" if (pasted or paste_suppressed) else exec_paste_reason
            auto_run_allowed = pasted and (self._auto_run_mode != "disabled")
            is_auto_tab = bool(req_tab_id and self._handoff_manager.is_auto_generated_tab(req_tab_id))
            auto_run = pasted and (
                (self._auto_run_mode == "enabled")
                or (self._auto_run_mode != "disabled" and is_auto_tab)
            )

            if exit_code == 0:
                status_msg = "コマンドを実行しました"
            else:
                detail = _extract_error_detail(tee_stderr.getvalue()) or system_exit_note
                status_msg = (
                    f"コマンドが終了コード {exit_code} で失敗しました: {detail}"
                    if detail
                    else f"コマンドが終了コード {exit_code} で失敗しました"
                )

            return int(HTTPStatus.OK), BridgeExecutionResult(
                ok=(exit_code == 0),
                exit_code=exit_code,
                command=display_cmd,
                argv=tuple(argv),
                message=status_msg,
                pasted=pasted,
                auto_run=auto_run,
                auto_run_allowed=auto_run_allowed,
                paste_reason=paste_reason,
            )

    def handle_trigger_run_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """JS側のクリック送信が反応しない場合のOSネイティブCtrl+Enter送信フォールバックを実行する。"""
        tab_error = self._binding_manager.validate_execute_payload(payload)
        if tab_error is not None:
            return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

        if self._auto_run_mode == "disabled" or self.no_focus or self.no_paste:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "自動送信は無効化されています"}
        # page_focused欠落時の誤爆を防ぐため明示的にTrueのときのみ許可する
        if payload.get("page_focused") is not True:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "タブが非アクティブのため自動送信を中止しました"}

        target_title_keywords = self._resolve_target_title_keywords(payload)
        with self._exec_lock:
            res = self.prompt_run_trigger(
                interactive_only=True,
                allowed_title_keywords=target_title_keywords,
            )
            ok = bool(getattr(res, "success", False))
            reason = str(getattr(res, "reason", "") or "")
            return int(HTTPStatus.OK), {
                "ok": ok,
                "message": "Ctrl+Enterを送信しました" if ok else (reason or "OSショートカット送信に失敗しました"),
            }

    @staticmethod
    def _make_job_result(action: str, ok: bool, message: str, exit_code: int = 1) -> Any:
        # dict応答APIはJSON直列化するため、actionに応じて戻り値型を厳密に揃える
        if action in ("handoff", "trigger_run", "summary_prompt", "bloat_targets", "split_prompt", "split_handoff"):
            return {"ok": ok, "message": message}
        return BridgeExecutionResult(
            ok=ok,
            exit_code=exit_code,
            command="",
            argv=(),
            message=message,
        )

    def _validate_optional_tab_id(self, payload: Dict[str, object], required: bool = False) -> Tuple[Optional[str], Optional[str]]:
        if "tab_id" not in payload:
            if required:
                return None, "tab_id を指定してください"
            return "", None
        raw_tab_id = payload.get("tab_id")
        if not isinstance(raw_tab_id, str):
            return None, "tab_id を文字列で指定してください"
        try:
            return self._binding_manager.validate_tab_id(raw_tab_id), None
        except ValueError as e:
            return None, str(e)

    def _execute_auto_apply(self, patch_text: str) -> Tuple[int, str]:
        return execute_auto_apply_patch(
            project_root=self.project_root,
            patch_text=patch_text,
            command_runner=self.command_runner,
            max_error_chars=MAX_ERROR_DETAIL_CHARS,
        )

    def _run_terminal_auto_apply(
        self,
        *,
        req_tab_id: str,
        target_patch: str,
        has_patch: bool,
        stop_signal_detected: bool,
        limit_reached: bool,
        remaining_auto_tabs: int,
        success_message: str,
        log_banner: str,
    ) -> Tuple[int, Dict[str, object]]:
        copied_to_clipboard = False
        try:
            self.clipboard_writer(target_patch)
            copied_to_clipboard = True
        except Exception as e:
            print(f"⚠ 応答のクリップボードコピーに失敗しました: {e}", file=sys.stderr, flush=True)

        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{timestamp}] {log_banner}", flush=True)
        apply_exit_code, apply_message = self._execute_auto_apply(target_patch)
        applied = apply_exit_code == 0
        if applied:
            self._handoff_manager.consume_detected_patch(req_tab_id, target_patch)
        return int(HTTPStatus.OK), {
            "ok": True,
            "skipped": True,
            "stop_signal_detected": stop_signal_detected,
            "limit_reached": limit_reached,
            "has_patch": has_patch,
            "has_target_patch": True,
            "remaining_auto_tabs": remaining_auto_tabs,
            "copied_to_clipboard": copied_to_clipboard,
            "apply_attempted": True,
            "applied": applied,
            "apply_exit_code": apply_exit_code,
            "apply_message": apply_message,
            "message": success_message if applied else apply_message,
        }

    def handle_handoff_create(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """<<<< ==== >>>> 出力応答を受領し、応答コピー・要約読込・引き継ぎレコード登録またはaiskel a自動適用を実行する。"""
        req_tab_id, tab_err = self._validate_optional_tab_id(payload, required=self._binding_manager.require_binding)
        if tab_err is not None or req_tab_id is None:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

        tab_error = self._binding_manager.validate_execute_payload(payload)
        if tab_error is not None:
            return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

        raw_response = payload.get("response_text")
        response_text = raw_response if isinstance(raw_response, str) else ""
        stop_requested = (payload.get("stop_handoff") is True) or has_handoff_stop_signal_in_text(response_text)
        has_patch = bool(response_text.strip() and has_patch_delimiters_in_text(response_text))
        if not has_patch and not stop_requested:
            return int(HTTPStatus.BAD_REQUEST), {
                "ok": False,
                "message": "パッチ区切り記号(<<<< ==== >>>>)を含む有効な応答テキストがありません",
            }

        turn_id = str(payload.get("turn_id") or "").strip()
        dedup_key = f"HANDOFF::{req_tab_id}::{turn_id}" if turn_id else ""

        new_chat_url = str(payload.get("new_chat_url") or "https://aistudio.google.com/prompts/new_chat").strip()
        if not new_chat_url.startswith("https://aistudio.google.com/"):
            new_chat_url = "https://aistudio.google.com/prompts/new_chat"

        with self._exec_lock:
            if dedup_key and dedup_key in self._recent_keys:
                return int(HTTPStatus.OK), {
                    "ok": True,
                    "skipped": True,
                    "duplicate_skipped": True,
                    "stop_signal_detected": stop_requested,
                    "has_patch": has_patch,
                    "message": "同一ターンのパッチ引き継ぎは処理済みのためスキップしました",
                }

            max_tabs = self.get_patch_handoff_max_tabs()
            remaining_before = self._handoff_manager.get_remaining_auto_tabs(req_tab_id, default_limit=max_tabs)
            if stop_requested:
                if has_patch:
                    self._handoff_manager.record_detected_patch(response_text, tab_id=req_tab_id)
                target_patch = response_text if has_patch else self._handoff_manager.get_last_detected_patch(req_tab_id)
                if not target_patch:
                    if dedup_key:
                        self._recent_keys.append(dedup_key)
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    print(
                        f"\n[{timestamp}] ✅ 欠陥なし完了合図({PATCH_HANDOFF_STOP_SIGNAL})を検知 -> 適用対象の置換コードがないため新規タブは作成しません",
                        flush=True,
                    )
                    return int(HTTPStatus.OK), {
                        "ok": True,
                        "skipped": True,
                        "stop_signal_detected": True,
                        "has_patch": False,
                        "has_target_patch": False,
                        "limit_reached": False,
                        "remaining_auto_tabs": remaining_before,
                        "copied_to_clipboard": False,
                        "apply_attempted": False,
                        "applied": False,
                        "apply_exit_code": None,
                        "apply_message": "",
                        "message": "完了合図を検知したため新規タブは作成しませんでした（適用対象の置換コードなし）",
                    }
                # 最終適用フェーズ到達後は適用失敗リトライ時に再度レビュー新タブを開かないよう残数を確定消費
                self._handoff_manager.consume_auto_tab_if_tracked(req_tab_id, 0)
                status_code, body = self._run_terminal_auto_apply(
                    req_tab_id=req_tab_id,
                    target_patch=target_patch,
                    has_patch=has_patch,
                    stop_signal_detected=True,
                    limit_reached=False,
                    remaining_auto_tabs=0,
                    success_message="完了合図を検知したため最後に検知した置換コードを aiskel a で適用しました",
                    log_banner=f"✅ 欠陥なし完了合図({PATCH_HANDOFF_STOP_SIGNAL})を検知 -> 最後に検知した置換コードを aiskel a で実行します",
                )
                if dedup_key:
                    self._recent_keys.append(dedup_key)
                return status_code, body

            if remaining_before <= 0:
                self._handoff_manager.record_detected_patch(response_text, tab_id=req_tab_id)
                status_code, body = self._run_terminal_auto_apply(
                    req_tab_id=req_tab_id,
                    target_patch=response_text,
                    has_patch=True,
                    stop_signal_detected=False,
                    limit_reached=True,
                    remaining_auto_tabs=0,
                    success_message="新規タブ自動作成の回数制限に達したため最後に検知した置換コードを aiskel a で適用しました",
                    log_banner="📋 パッチ出力(<<<< ==== >>>>)を検知（新規タブ自動作成の回数制限到達） -> 最後に検知した置換コードを aiskel a で実行します",
                )
                if dedup_key:
                    self._recent_keys.append(dedup_key)
                return status_code, body

            try:
                summary_text = load_project_summary_text(self.project_root)
            except (OSError, ValueError, RuntimeError) as e:
                return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                    "ok": False,
                    "message": f"要約バンドルの取得に失敗しました: {e}",
                }

            copied_to_clipboard = False
            try:
                self.clipboard_writer(response_text)
                copied_to_clipboard = True
            except Exception as e:
                print(f"⚠ 応答のクリップボードコピーに失敗しました: {e}", file=sys.stderr, flush=True)

            self._handoff_manager.record_detected_patch(response_text, tab_id=req_tab_id)
            next_remaining = remaining_before - 1
            review_prompt = load_prompt_for_pattern(self.project_root, PromptPattern.PATCH_REVIEW)
            handoff = self._handoff_manager.create(
                source_tab_id=req_tab_id,
                new_chat_url=new_chat_url,
                response_text=response_text,
                summary_text=summary_text,
                review_prompt=review_prompt,
                remaining_auto_tabs=next_remaining,
            )
            self._handoff_manager.consume_auto_tab_if_tracked(req_tab_id, next_remaining)
            if dedup_key:
                self._recent_keys.append(dedup_key)

            timestamp = datetime.now().strftime("%H:%M:%S")
            print(
                f"\n[{timestamp}] 📋 パッチ出力(<<<< ==== >>>>)を検知 -> 応答をコピーし、新規タブへ要約とともに引き継ぎます (新規タブ残り回数: {next_remaining})",
                flush=True,
            )
            return int(HTTPStatus.OK), {
                "ok": True,
                "skipped": False,
                "limit_reached": False,
                "remaining_auto_tabs": next_remaining,
                "copied_to_clipboard": copied_to_clipboard,
                "handoff_id": handoff.handoff_id,
                "new_chat_url": handoff.new_chat_url,
                "message": "パッチ応答をコピーし、新規タブ引き継ぎを準備しました",
            }

    def handle_handoff_claim(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """新規タブからの引き継ぎ取得要求を処理し、必要時は新規タブ紐づけも同時に確定する。"""
        req_tab_id, tab_err = self._validate_optional_tab_id(payload, required=self._binding_manager.require_binding)
        if tab_err is not None or req_tab_id is None:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

        raw_handoff_id = payload.get("handoff_id")
        if raw_handoff_id is not None and not isinstance(raw_handoff_id, str):
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "handoff_id を文字列で指定してください"}
        handoff_id = raw_handoff_id.strip() if isinstance(raw_handoff_id, str) else ""

        item = self._handoff_manager.claim(handoff_id=handoff_id, tab_id=req_tab_id)
        if item is None:
            return int(HTTPStatus.OK), {"ok": True, "found": False}

        binding_info: Optional[Dict[str, object]] = None
        if req_tab_id:
            current_info = self.get_binding_info()
            if not current_info.get("bound") or current_info.get("tab_id") != req_tab_id:
                try:
                    binding_info = self.bind_tab(
                        tab_id=req_tab_id,
                        tab_title=str(payload.get("tab_title") or "Google AI Studio"),
                        tab_url=str(payload.get("tab_url") or item.new_chat_url),
                        handoff_id=item.handoff_id,
                    )
                    print(f"\n🔗 新規タブをVSCodeプロジェクトと自動紐づけしました: {req_tab_id}", flush=True)
                except ValueError as e:
                    return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": str(e)}
            else:
                binding_info = current_info

        return int(HTTPStatus.OK), {
            "ok": True,
            "found": True,
            "handoff": item.to_dict(),
            "binding": binding_info,
        }

    def handle_handoff_ack(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """新規タブへの貼り付け完了通知を受理し、引き継ぎレコードを解放する。"""
        req_tab_id, tab_err = self._validate_optional_tab_id(payload, required=False)
        if tab_err is not None or req_tab_id is None:
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

        raw_handoff_id = payload.get("handoff_id")
        if raw_handoff_id is not None and not isinstance(raw_handoff_id, str):
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "handoff_id を文字列で指定してください"}
        handoff_id = raw_handoff_id.strip() if isinstance(raw_handoff_id, str) else ""
        acked = self._handoff_manager.ack(handoff_id=handoff_id, tab_id=req_tab_id)
        return int(HTTPStatus.OK), {"ok": True, "acked": acked}

    def _dispatch_job(self, payload: Dict[str, object], action: str) -> Tuple[int, Any]:
        # シャットダウン時のキュー取り残し競合を防ぐため状態確認と投入をロック内で原子化
        with self._queue_state_lock:
            if self._shutting_down:
                return int(HTTPStatus.SERVICE_UNAVAILABLE), self._make_job_result(
                    action, False, "ブリッジサーバー終了のため実行を中止しました"
                )
            if not self._use_main_thread_queue:
                run_inline = True
                job: Optional[_ExecutionJob] = None
            else:
                run_inline = False
                job = _ExecutionJob(payload=payload, action=action)
                self._job_queue.put(job)

        if run_inline or job is None:
            if action == "handoff":
                return self.handle_handoff_create(payload)
            if action == "trigger_run":
                return self.handle_trigger_run_payload(payload)
            if action == "summary_prompt":
                return self.handle_summary_prompt_payload(payload)
            if action == "bloat_targets":
                return self.handle_bloat_targets_payload(payload)
            if action == "split_prompt":
                return self.handle_split_prompt_payload(payload)
            if action == "split_handoff":
                return self.handle_split_handoff_payload(payload)
            return self.handle_execute_payload(payload)

        if not job.done_event.wait(timeout=EXECUTION_WAIT_TIMEOUT_SEC):
            return int(HTTPStatus.GATEWAY_TIMEOUT), self._make_job_result(
                action, False, "コマンド実行がタイムアウトしました"
            )
        if job.error is not None:
            raise job.error
        if job.result is None:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), self._make_job_result(
                action, False, "コマンド実行結果を取得できませんでした"
            )
        return job.status_code, job.result

    def _dispatch_execute_payload(self, payload: Dict[str, object]) -> Tuple[int, BridgeExecutionResult]:
        return self._dispatch_job(payload, action="execute")

    def _dispatch_handoff_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._dispatch_job(payload, action="handoff")

    def _dispatch_trigger_run_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._dispatch_job(payload, action="trigger_run")

    def _dispatch_summary_prompt_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._dispatch_job(payload, action="summary_prompt")

    def _dispatch_bloat_targets_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._dispatch_job(payload, action="bloat_targets")

    def _dispatch_split_prompt_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._dispatch_job(payload, action="split_prompt")

    def _dispatch_split_handoff_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        return self._dispatch_job(payload, action="split_handoff")

    def _drain_pending_jobs_locked(self) -> None:
        while True:
            try:
                job = self._job_queue.get_nowait()
            except queue.Empty:
                break
            job.status_code = int(HTTPStatus.SERVICE_UNAVAILABLE)
            job.result = self._make_job_result(
                job.action, False, "ブリッジサーバー終了のため実行を中止しました"
            )
            job.done_event.set()

    def _build_handler_class(self):
        return build_bridge_request_handler_class(
            bridge=self,
            max_payload_bytes=MAX_PAYLOAD_BYTES,
            socket_timeout_sec=SOCKET_TIMEOUT_SEC,
            is_allowed_origin_fn=is_allowed_origin,
            max_handoff_payload_bytes=MAX_HANDOFF_PAYLOAD_BYTES,
        )

    def bind(self) -> Tuple[str, int]:
        """ソケットをバインドしてサーバーインスタンスを初期化する(IPv4/IPv6対応)。"""
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not (0 <= self.port <= 65535):
            raise ValueError(f"ポート番号は 0〜65535 の範囲で指定してください: {self.port}")
        if self._httpd is None:
            handler_cls = self._build_handler_class()
            bind_host = normalize_bind_host(self.host)
            server_cls = _IPv6ThreadingHTTPServer if _is_ipv6_literal(bind_host) else ThreadingHTTPServer
            self._httpd = server_cls((bind_host, self.port), handler_cls)
            self._httpd.daemon_threads = True
        return self.server_address

    def serve_forever(self, on_ready: Optional[Callable[[threading.Event], None]] = None) -> None:
        """HTTP待受を専用スレッドで稼働させ、起動後処理とコマンド実行はCOM/UI安全なメインスレッドで処理する。"""
        self.bind()
        assert self._httpd is not None
        self._stop_event.clear()
        with self._queue_state_lock:
            self._shutting_down = False
            self._use_main_thread_queue = True
            self._startup_ready_in_progress = on_ready is not None

        httpd = self._httpd
        self._http_thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="aiskel-bridge-http",
        )
        self._http_thread.start()

        try:
            # なぜ必要か: UI/COM操作を含む起動後処理をメインスレッド上で安全に実行し、ログ混線と終了時競合を防ぐため
            if on_ready is not None:
                try:
                    on_ready(self._stop_event)
                except Exception as e:
                    print(f"⚠ ブリッジ起動後処理でエラーが発生しました: {e}", file=sys.stderr, flush=True)

            while not self._stop_event.is_set():
                try:
                    job = self._job_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    if job.action == "handoff":
                        status_code, result = self.handle_handoff_create(job.payload)
                    elif job.action == "trigger_run":
                        status_code, result = self.handle_trigger_run_payload(job.payload)
                    elif job.action == "summary_prompt":
                        status_code, result = self.handle_summary_prompt_payload(job.payload)
                    elif job.action == "bloat_targets":
                        status_code, result = self.handle_bloat_targets_payload(job.payload)
                    elif job.action == "split_prompt":
                        status_code, result = self.handle_split_prompt_payload(job.payload)
                    elif job.action == "split_handoff":
                        status_code, result = self.handle_split_handoff_payload(job.payload)
                    else:
                        status_code, result = self.handle_execute_payload(job.payload)
                    job.status_code = status_code
                    job.result = result
                except KeyboardInterrupt:
                    job.status_code = int(HTTPStatus.SERVICE_UNAVAILABLE)
                    job.result = self._make_job_result(
                        job.action, False, "ブリッジサーバー終了のため実行を中止しました"
                    )
                    raise
                except SystemExit as e:
                    exit_code = _normalize_system_exit_code(e)
                    job.status_code = int(HTTPStatus.OK)
                    job.result = self._make_job_result(
                        job.action,
                        ok=(exit_code == 0),
                        message=f"コマンドが終了コード {exit_code} で終了しました",
                        exit_code=exit_code,
                    )
                except BaseException as e:
                    job.error = e
                finally:
                    job.done_event.set()
        finally:
            self._stop_event.set()
            with self._queue_state_lock:
                self._shutting_down = True
                self._use_main_thread_queue = False
                self._drain_pending_jobs_locked()

    def shutdown(self) -> None:
        """起動中サーバーを安全に停止・クローズする。"""
        self._stop_event.set()
        with self._queue_state_lock:
            self._shutting_down = True
            self._use_main_thread_queue = False
            self._drain_pending_jobs_locked()
        httpd = self._httpd
        http_thread = self._http_thread
        if httpd is not None and http_thread is not None and http_thread.is_alive():
            # なぜ必要か: HTTP待受スレッド未起動時にshutdown()を呼ぶとBaseServer内で永久待機するため
            httpd.shutdown()
        if http_thread is not None and http_thread.is_alive():
            http_thread.join(timeout=2.0)
        if httpd is not None:
            # なぜ必要か: 待受スレッド終了後にソケットを閉じて参照競合を防ぐため
            httpd.server_close()
        self._httpd = None
        self._http_thread = None


def run_bridge_server(
    project_root: Path,
    on_run_command: Callable[[List[str]], int],
    host: str = DEFAULT_BRIDGE_HOST,
    port: int = DEFAULT_BRIDGE_PORT,
    explicit_token: Optional[str] = None,
    extension_dir_arg: Optional[Path] = None,
    no_focus: bool = False,
    no_paste: bool = False,
    on_ready: Optional[Callable[[threading.Event], None]] = None,
    require_tab_binding: bool = True,
    auto_run: Optional[bool] = None,
    structured_command_runner: Optional[Callable[..., Any]] = None,
    output_dir: Optional[Path] = None,
    patch_handoff_max_tabs: Optional[int] = None,
) -> int:
    """ローカルブリッジサーバーと拡張機能を準備し、ブラウザからの推論完了通知を待機する。"""
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたプロジェクトディレクトリが存在しません: {project_root}", file=sys.stderr)
        return 1

    if not isinstance(port, int) or isinstance(port, bool) or not (0 <= port <= 65535):
        print(f"エラー: ポート番号は 0〜65535 の範囲で指定してください: {port}", file=sys.stderr)
        return 1

    server = AiskelBridgeServer(
        project_root=project_root,
        command_runner=on_run_command,
        host=host,
        port=port,
        token=None,
        no_focus=no_focus,
        no_paste=no_paste,
        require_tab_binding=require_tab_binding,
        auto_run=auto_run,
        structured_command_runner=structured_command_runner,
        output_dir=output_dir,
        patch_handoff_max_tabs=patch_handoff_max_tabs,
    )

    # --port 0(動的割当)でも実ポートを拡張機能へ反映させるため先にbindを実行
    try:
        bound_host, bound_port = server.bind()
    except (OSError, ValueError, OverflowError) as e:
        print(f"エラー: ブリッジサーバーの起動に失敗しました ({host}:{port}): {e}", file=sys.stderr)
        return 1

    try:
        try:
            extension_dir = (
                extension_dir_arg.resolve()
                if extension_dir_arg is not None
                else get_default_extension_dir(port=bound_port)
            )
            active_token = load_or_create_bridge_token(extension_dir, explicit_token=explicit_token)
            server.token = active_token
            exported_dir, extension_updated = export_browser_extension(
                extension_dir,
                host=bound_host,
                port=bound_port,
                token=active_token,
                browser="chrome",
            )
            firefox_dir, firefox_updated = export_browser_extension(
                get_firefox_extension_dir(extension_dir),
                host=bound_host,
                port=bound_port,
                token=active_token,
                browser="firefox",
            )
        except OSError as e:
            print(f"エラー: 拡張機能または認証トークンファイルの出力に失敗しました: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"エラー: 拡張機能またはホスト設定が不正です: {e}", file=sys.stderr)
            return 1

        try:
            client_host = resolve_client_host(bound_host)
            bound_url_host = format_host_for_url(bound_host)
            client_url_host = format_host_for_url(client_host)
            loopback = is_loopback_host(bound_host)
        except ValueError as e:
            print(f"エラー: ホスト設定が不正です: {e}", file=sys.stderr)
            return 1

        print(f"🎧 Google AI Studio 自動実行ブリッジを起動しました: http://{bound_url_host}:{bound_port} (拡張機能連携用API)")
        if client_url_host != bound_url_host:
            print(f"   - ブラウザ接続先         : http://{client_url_host}:{bound_port}")
        if not loopback:
            print("   ⚠ ループバック以外のインターフェースで待受中です。認証トークンが有効化されています。")
        print(f"   - 対象プロジェクト       : {project_root}")
        print(f"   - パッチ自動新タブ上限   : {server.get_patch_handoff_max_tabs()} 回")
        print(f"   - Chrome/Edge拡張フォルダ: {exported_dir}")
        print(f"   - Firefox拡張マニフェスト: {firefox_dir / 'manifest.json'}")
        if extension_updated or firefox_updated:
            print("   ⚠ 拡張機能設定が更新されました。既に読み込み済みの場合はブラウザの拡張機能画面で「更新(リロード)」してください。")
        print("   ※ Chrome/Edge: chrome://extensions から上記拡張フォルダを読み込み")
        print("   ※ Firefox    : about:debugging#/runtime/this-firefox の「一時的なアドオンを読み込む」から上記 manifest.json を選択")
        print("   ※ 常駐利用   : Tampermonkey / Violentmonkey 用スクリプトは `aiskel listen --print-userscript` で出力できます")
        print("   ※ タブ紐づけ : AI Studioタブを開くと自動で紐づきます（解除・別タブへの切替はタブ上UIまたは拡張機能アイコンから操作）")
        print("   推論完了時の `aiskel c` コマンドを待機中... (終了するには Ctrl+C)")

        server.serve_forever(on_ready=on_ready)
    except (OSError, ValueError) as e:
        print(f"エラー: ブリッジサーバーの実行中にエラーが発生しました: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n⏹ ブリッジサーバーを終了しました。")
    finally:
        server.shutdown()
    return 0
