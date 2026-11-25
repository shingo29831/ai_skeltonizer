"""Module: @role: ローカルHTTPブリッジのソケットバインド・メインスレッドジョブキュー排他ディスパッチおよび終了処理を担当する。"""
import ipaddress
import queue
import socket
import sys
import threading
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

from .aistudio_bridge_types import (
    EXECUTION_WAIT_TIMEOUT_SEC,
    ExecutionJob,
    normalize_system_exit_code,
)
from .aistudio_extension import normalize_bind_host

class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    """IPv6ソケット待受用のThreadingHTTPServer。"""

    address_family = socket.AF_INET6

def is_ipv6_literal(host: str) -> bool:
    """指定文字列がIPv6アドレスリテラルであるか判定する。"""
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False

def execute_bridge_action(bridge: Any, action: str, payload: Dict[str, object]) -> Tuple[int, Any]:
    """アクション種別に応じたブリッジハンドラを実行する。"""
    if action == "handoff":
        return bridge.handle_handoff_create(payload)
    if action == "trigger_run":
        return bridge.handle_trigger_run_payload(payload)
    if action == "summary_prompt":
        return bridge.handle_summary_prompt_payload(payload)
    if action == "bloat_targets":
        return bridge.handle_bloat_targets_payload(payload)
    if action == "split_prompt":
        return bridge.handle_split_prompt_payload(payload)
    if action == "split_handoff":
        return bridge.handle_split_handoff_payload(payload)
    return bridge.handle_execute_payload(payload)

def dispatch_bridge_job(bridge: Any, payload: Dict[str, object], action: str) -> Tuple[int, Any]:
    """稼働中ブリッジのメインスレッドキューへジョブを投入し、完了結果を待機して返す。"""
    # シャットダウン時のキュー取り残し競合を防ぐため状態確認と投入をロック内で原子化
    with bridge._queue_state_lock:
        if bridge._shutting_down:
            return int(HTTPStatus.SERVICE_UNAVAILABLE), bridge._make_job_result(
                action, False, "ブリッジサーバー終了のため実行を中止しました"
            )
        if not bridge._use_main_thread_queue:
            run_inline = True
            job: Optional[ExecutionJob] = None
        else:
            run_inline = False
            job = ExecutionJob(payload=payload, action=action)
            bridge._job_queue.put(job)

    if run_inline or job is None:
        return bridge._execute_action(action, payload)

    if not job.done_event.wait(timeout=EXECUTION_WAIT_TIMEOUT_SEC):
        return int(HTTPStatus.GATEWAY_TIMEOUT), bridge._make_job_result(
            action, False, "コマンド実行がタイムアウトしました"
        )
    if job.error is not None:
        raise job.error
    if job.result is None:
        return int(HTTPStatus.INTERNAL_SERVER_ERROR), bridge._make_job_result(
            action, False, "コマンド実行結果を取得できませんでした"
        )
    return job.status_code, job.result

def drain_pending_jobs_locked(bridge: Any) -> None:
    """キューに残留している未処理ジョブへ終了応答を通知して解放する。"""
    while True:
        try:
            job = bridge._job_queue.get_nowait()
        except queue.Empty:
            break
        job.status_code = int(HTTPStatus.SERVICE_UNAVAILABLE)
        job.result = bridge._make_job_result(
            job.action, False, "ブリッジサーバー終了のため実行を中止しました"
        )
        job.done_event.set()

def bind_bridge_server(
    bridge: Any,
    *,
    ipv4_server_cls: type[ThreadingHTTPServer] = ThreadingHTTPServer,
    ipv6_server_cls: type[ThreadingHTTPServer] = IPv6ThreadingHTTPServer,
) -> Tuple[str, int]:
    """ソケットをバインドしてサーバーインスタンスを初期化する(IPv4/IPv6対応)。"""
    if not isinstance(bridge.port, int) or isinstance(bridge.port, bool) or not (0 <= bridge.port <= 65535):
        raise ValueError(f"ポート番号は 0〜65535 の範囲で指定してください: {bridge.port}")
    if bridge._httpd is None:
        handler_cls = bridge._build_handler_class()
        bind_host = normalize_bind_host(bridge.host)
        server_cls = ipv6_server_cls if is_ipv6_literal(bind_host) else ipv4_server_cls
        bridge._httpd = server_cls((bind_host, bridge.port), handler_cls)
        bridge._httpd.daemon_threads = True
    return bridge.server_address

def serve_bridge_forever(
    bridge: Any,
    on_ready: Optional[Callable[[threading.Event], None]] = None,
) -> None:
    """HTTP待受を専用スレッドで稼働させ、起動後処理とコマンド実行はメインスレッドで処理する。"""
    bridge.bind()
    assert bridge._httpd is not None
    bridge._stop_event.clear()
    with bridge._queue_state_lock:
        bridge._shutting_down = False
        bridge._use_main_thread_queue = True
        bridge._startup_ready_in_progress = on_ready is not None

    httpd = bridge._httpd
    bridge._http_thread = threading.Thread(
        target=httpd.serve_forever,
        kwargs={"poll_interval": 0.2},
        daemon=True,
        name="aiskel-bridge-http",
    )
    bridge._http_thread.start()

    try:
        # UI/COM操作を含む起動後処理をメインスレッドで実行しログ混線と終了競合を防ぐ
        if on_ready is not None:
            try:
                on_ready(bridge._stop_event)
            except Exception as e:
                print(f"⚠ ブリッジ起動後処理でエラーが発生しました: {e}", file=sys.stderr, flush=True)

        while not bridge._stop_event.is_set():
            try:
                job = bridge._job_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                status_code, result = bridge._execute_action(job.action, job.payload)
                job.status_code = status_code
                job.result = result
            except KeyboardInterrupt:
                job.status_code = int(HTTPStatus.SERVICE_UNAVAILABLE)
                job.result = bridge._make_job_result(
                    job.action, False, "ブリッジサーバー終了のため実行を中止しました"
                )
                raise
            except SystemExit as e:
                exit_code = normalize_system_exit_code(e)
                job.status_code = int(HTTPStatus.OK)
                job.result = bridge._make_job_result(
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
        bridge._stop_event.set()
        with bridge._queue_state_lock:
            bridge._shutting_down = True
            bridge._use_main_thread_queue = False
            bridge._drain_pending_jobs_locked()

def shutdown_bridge_server(bridge: Any) -> None:
    """起動中サーバーを安全に停止・クローズする。"""
    bridge._stop_event.set()
    with bridge._queue_state_lock:
        bridge._shutting_down = True
        bridge._use_main_thread_queue = False
        bridge._drain_pending_jobs_locked()
    httpd = bridge._httpd
    http_thread = bridge._http_thread
    if httpd is not None and http_thread is not None and http_thread.is_alive():
        # HTTP待受スレッド未起動時にshutdown()を呼ぶとBaseServer内で永久待機するため
        httpd.shutdown()
    if http_thread is not None and http_thread.is_alive():
        http_thread.join(timeout=2.0)
    if httpd is not None:
        # 待受スレッド終了後にソケットを閉じて参照競合を防ぐため
        httpd.server_close()
    bridge._httpd = None
    bridge._http_thread = None
