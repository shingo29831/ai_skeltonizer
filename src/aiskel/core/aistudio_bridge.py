"""Module: @role: ブラウザ監視クライアントからの通知を受信し、検証済みaiskelコマンドをメインスレッド上で安全に自動実行するローカルHTTPブリッジを提供する。"""
import ipaddress
import queue
import socket
import sys
import threading
from collections import deque
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .aistudio_auto_apply import execute_auto_apply_patch
from .aistudio_bridge_execution import (
    handle_execute_command,
    handle_trigger_run,
    normalize_incoming_commands,
)
from .aistudio_bridge_handler import build_bridge_request_handler_class
from .aistudio_bridge_handoff import (
    get_patch_handoff_max_tabs as _get_patch_handoff_max_tabs,
    handle_handoff_ack as _handle_handoff_ack,
    handle_handoff_claim as _handle_handoff_claim,
    handle_handoff_create as _handle_handoff_create,
    handle_handoff_max_tabs as _handle_handoff_max_tabs,
    run_terminal_auto_apply as _run_terminal_auto_apply_impl,
    set_patch_handoff_max_tabs as _set_patch_handoff_max_tabs,
)
from .aistudio_bridge_prompts import (
    get_prompt_response,
    handle_bloat_targets,
    handle_split_handoff,
    handle_split_prompt,
    handle_summary_prompt,
)
from .aistudio_bridge_runner import run_bridge_lifecycle
from .aistudio_bridge_state import TabBindingManager
from .aistudio_bridge_types import (
    ALLOWED_EXTENSION_SCHEMES,
    EXECUTION_WAIT_TIMEOUT_SEC,
    MAX_DEDUP_HISTORY,
    MAX_ERROR_DETAIL_CHARS,
    MAX_PATCH_HANDOFF_MAX_TABS,
    MAX_PAYLOAD_BYTES,
    SOCKET_TIMEOUT_SEC,
    BridgeExecutionResult,
    ExecutionJob as _ExecutionJob,
    TeeTextIO as _TeeTextIO,
    extract_error_detail as _extract_error_detail,
    is_allowed_origin,
    make_bridge_job_result,
    normalize_system_exit_code as _normalize_system_exit_code,
    resolve_target_title_keywords,
    validate_optional_tab_id,
)
from .aistudio_extension import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    normalize_bind_host,
)
from .aistudio_handoff import (
    MAX_HANDOFF_PAYLOAD_BYTES,
    PatchHandoffManager,
)
from .aistudio_prompts import PromptPattern
from .browser_paste import trigger_browser_prompt_run
from .clipboard import set_clipboard_text

__all__ = [
    "ALLOWED_EXTENSION_SCHEMES",
    "AiskelBridgeServer",
    "BridgeExecutionResult",
    "EXECUTION_WAIT_TIMEOUT_SEC",
    "MAX_DEDUP_HISTORY",
    "MAX_ERROR_DETAIL_CHARS",
    "MAX_PATCH_HANDOFF_MAX_TABS",
    "MAX_PAYLOAD_BYTES",
    "SOCKET_TIMEOUT_SEC",
    "is_allowed_origin",
    "run_bridge_server",
]

class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

def _is_ipv6_literal(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
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
        return _get_patch_handoff_max_tabs(self)

    def set_patch_handoff_max_tabs(self, max_tabs: int) -> Dict[str, object]:
        """パッチ検知時の新規タブ自動作成上限回数を動的に更新し、最新の紐づけ情報を返す。"""
        return _set_patch_handoff_max_tabs(self, max_tabs)

    def handle_handoff_max_tabs_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """ブラウザUI等からの新規タブ自動作成上限回数の変更リクエストを検証・反映する。"""
        return _handle_handoff_max_tabs(self, payload)

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
        return get_prompt_response(self.project_root, pattern)

    def handle_summary_prompt_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """最新のプロジェクト要約バンドルと指定パターンのプロンプトを取得し統合テキストを返す。"""
        return handle_summary_prompt(self, payload)

    def handle_bloat_targets_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """aiskel b 基準でプロジェクト内の肥大化ファイルを検出し、ファイルサイズ降順の一覧を返す。"""
        return handle_bloat_targets(self, payload)

    def handle_split_prompt_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """現在の空タブで自動分割を開始するための対象1ファイル分割指示+最新要約テキストを返す。"""
        return handle_split_prompt(self, payload)

    def handle_split_handoff_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """次の分割対象ファイルを新しいチャットタブへ最新要約とともに引き継ぐレコードを作成する。"""
        return handle_split_handoff(self, payload)

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
        return resolve_target_title_keywords(self._binding_manager, payload)

    def _normalize_incoming_commands(self, payload: Dict[str, object]) -> List[str]:
        return normalize_incoming_commands(payload)

    def handle_execute_payload(self, payload: Dict[str, object]) -> Tuple[int, BridgeExecutionResult]:
        """受信JSONペイロードを検証し、安全なaiskel cコマンドを排他実行する。"""
        return handle_execute_command(self, payload)

    def handle_trigger_run_payload(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """JS側のクリック送信が反応しない場合のOSネイティブCtrl+Enter送信フォールバックを実行する。"""
        return handle_trigger_run(self, payload)

    @staticmethod
    def _make_job_result(action: str, ok: bool, message: str, exit_code: int = 1) -> Any:
        return make_bridge_job_result(action, ok, message, exit_code=exit_code)

    def _validate_optional_tab_id(self, payload: Dict[str, object], required: bool = False) -> Tuple[Optional[str], Optional[str]]:
        return validate_optional_tab_id(self._binding_manager, payload, required=required)

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
        return _run_terminal_auto_apply_impl(
            self,
            req_tab_id=req_tab_id,
            target_patch=target_patch,
            has_patch=has_patch,
            stop_signal_detected=stop_signal_detected,
            limit_reached=limit_reached,
            remaining_auto_tabs=remaining_auto_tabs,
            success_message=success_message,
            log_banner=log_banner,
        )

    def handle_handoff_create(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """<<<< ==== >>>> 出力応答を受領し、応答コピー・要約読込・引き継ぎレコード登録またはaiskel a自動適用を実行する。"""
        return _handle_handoff_create(self, payload)

    def handle_handoff_claim(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """新規タブからの引き継ぎ取得要求を処理し、必要時は新規タブ紐づけも同時に確定する。"""
        return _handle_handoff_claim(self, payload)

    def handle_handoff_ack(self, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        """新規タブへの貼り付け完了通知を受理し、引き継ぎレコードを解放する。"""
        return _handle_handoff_ack(self, payload)

    def _execute_action(self, action: str, payload: Dict[str, object]) -> Tuple[int, Any]:
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
            return self._execute_action(action, payload)

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
                    status_code, result = self._execute_action(job.action, job.payload)
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

    return run_bridge_lifecycle(
        server,
        host=host,
        port=port,
        display_project_root=project_root,
        explicit_token=explicit_token,
        extension_dir_arg=extension_dir_arg,
        on_ready=on_ready,
    )
