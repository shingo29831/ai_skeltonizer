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
from typing import Callable, Deque, Dict, List, Optional, TextIO, Tuple
from urllib.parse import urlsplit

from .aistudio_bridge_handler import build_bridge_request_handler_class
from .aistudio_bridge_state import TabBindingManager
from .aistudio_command import (
    CommandValidationError,
    build_safe_copy_argv,
    extract_aiskel_copy_commands,
)
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

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "command": self.command,
            "message": self.message,
            "skipped": self.skipped,
        }


@dataclass
class _ExecutionJob:
    payload: Dict[str, object]
    done_event: threading.Event = field(default_factory=threading.Event)
    status_code: int = int(HTTPStatus.INTERNAL_SERVER_ERROR)
    result: Optional[BridgeExecutionResult] = None
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
    ) -> None:
        self.project_root = project_root.resolve()
        self.command_runner = command_runner
        self.host = host
        self.port = port
        self._token: Optional[str] = None
        self.token = token
        self.no_focus = no_focus
        self.no_paste = no_paste
        self._binding_manager = TabBindingManager(self.project_root, require_binding=require_tab_binding)
        self._exec_lock = threading.Lock()
        self._queue_state_lock = threading.Lock()
        self._recent_keys: Deque[str] = deque(maxlen=MAX_DEDUP_HISTORY)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._job_queue: "queue.Queue[_ExecutionJob]" = queue.Queue()
        self._stop_event = threading.Event()
        self._use_main_thread_queue = False
        self._shutting_down = False

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

    def get_binding_info(self) -> Dict[str, object]:
        """現在のブラウザタブ紐づけ状態を返す。"""
        return self._binding_manager.get_info()

    def bind_tab(self, tab_id: str, tab_title: str = "", tab_url: str = "") -> Dict[str, object]:
        """指定されたブラウザタブをこのブリッジへ紐づける。"""
        return self._binding_manager.bind(tab_id=tab_id, tab_title=tab_title, tab_url=tab_url)

    def unbind_tab(self, tab_id: Optional[str] = None) -> Tuple[bool, Dict[str, object]]:
        """現在のブラウザタブ紐づけを解除する。"""
        return self._binding_manager.unbind(tab_id=tab_id)

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
            with contextlib.redirect_stderr(tee_stderr):
                try:
                    exit_code = int(self.command_runner(argv))
                except SystemExit as e:
                    # argparse等のSystemExitでブリッジワーカーループが崩壊するのを防止
                    exit_code = _normalize_system_exit_code(e)
                    if isinstance(e.code, str) and e.code.strip():
                        system_exit_note = e.code.strip()

            if exit_code == 0 and dedup_key:
                self._recent_keys.append(dedup_key)

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
            )

    def _dispatch_execute_payload(self, payload: Dict[str, object]) -> Tuple[int, BridgeExecutionResult]:
        # シャットダウン時のキュー取り残し競合を防ぐため状態確認と投入をロック内で原子化
        with self._queue_state_lock:
            if self._shutting_down:
                return int(HTTPStatus.SERVICE_UNAVAILABLE), BridgeExecutionResult(
                    ok=False,
                    exit_code=1,
                    command="",
                    argv=(),
                    message="ブリッジサーバー終了のため実行を中止しました",
                )
            if not self._use_main_thread_queue:
                run_inline = True
                job: Optional[_ExecutionJob] = None
            else:
                run_inline = False
                job = _ExecutionJob(payload=payload)
                self._job_queue.put(job)

        if run_inline or job is None:
            return self.handle_execute_payload(payload)

        if not job.done_event.wait(timeout=EXECUTION_WAIT_TIMEOUT_SEC):
            return int(HTTPStatus.GATEWAY_TIMEOUT), BridgeExecutionResult(
                ok=False,
                exit_code=1,
                command="",
                argv=(),
                message="コマンド実行がタイムアウトしました",
            )
        if job.error is not None:
            raise job.error
        if job.result is None:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), BridgeExecutionResult(
                ok=False,
                exit_code=1,
                command="",
                argv=(),
                message="コマンド実行結果を取得できませんでした",
            )
        return job.status_code, job.result

    def _drain_pending_jobs_locked(self) -> None:
        while True:
            try:
                job = self._job_queue.get_nowait()
            except queue.Empty:
                break
            job.status_code = int(HTTPStatus.SERVICE_UNAVAILABLE)
            job.result = BridgeExecutionResult(
                ok=False,
                exit_code=1,
                command="",
                argv=(),
                message="ブリッジサーバー終了のため実行を中止しました",
            )
            job.done_event.set()

    def _build_handler_class(self):
        return build_bridge_request_handler_class(
            bridge=self,
            max_payload_bytes=MAX_PAYLOAD_BYTES,
            socket_timeout_sec=SOCKET_TIMEOUT_SEC,
            is_allowed_origin_fn=is_allowed_origin,
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
                    status_code, result = self.handle_execute_payload(job.payload)
                    job.status_code = status_code
                    job.result = result
                except KeyboardInterrupt:
                    job.status_code = int(HTTPStatus.SERVICE_UNAVAILABLE)
                    job.result = BridgeExecutionResult(
                        ok=False,
                        exit_code=1,
                        command="",
                        argv=(),
                        message="ブリッジサーバー終了のため実行を中止しました",
                    )
                    raise
                except SystemExit as e:
                    exit_code = _normalize_system_exit_code(e)
                    job.status_code = int(HTTPStatus.OK)
                    job.result = BridgeExecutionResult(
                        ok=(exit_code == 0),
                        exit_code=exit_code,
                        command="",
                        argv=(),
                        message=f"コマンドが終了コード {exit_code} で終了しました",
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
        print(f"   - Chrome/Edge拡張フォルダ: {exported_dir}")
        print(f"   - Firefox拡張マニフェスト: {firefox_dir / 'manifest.json'}")
        if extension_updated or firefox_updated:
            print("   ⚠ 拡張機能設定が更新されました。既に読み込み済みの場合はブラウザの拡張機能画面で「更新(リロード)」してください。")
        print("   ※ Chrome/Edge: chrome://extensions から上記拡張フォルダを読み込み")
        print("   ※ Firefox    : about:debugging#/runtime/this-firefox の「一時的なアドオンを読み込む」から上記 manifest.json を選択")
        print("   ※ 常駐利用   : Tampermonkey / Violentmonkey 用スクリプトは `aiskel listen --print-userscript` で出力できます")
        print("   ※ タブ紐づけ : 開いたタブ上の「VSCodeと紐づける」ボタン、または拡張機能アイコンから対象タブを選択してください")
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
