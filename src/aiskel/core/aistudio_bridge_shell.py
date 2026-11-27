"""Module: @role: ブリッジ経由の任意シェルコマンド検証・サブプロセス実行・出力捕捉およびプロセスツリー終了制御を担当する。"""
import locale
import math
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Optional, TextIO, Tuple

from .aistudio_bridge_state import bridge_command_scope, sanitize_console_text
from .aistudio_bridge_types import BridgeCommandKind

DEFAULT_SHELL_COMMAND_TIMEOUT_SEC = 60
MAX_SHELL_COMMAND_TIMEOUT_SEC = 110
MAX_SHELL_COMMAND_CHARS = 8192
MAX_SHELL_OUTPUT_CHARS = 32768
_SHELL_CAPTURE_HEAD_BYTES = 262144
_SHELL_CAPTURE_TAIL_BYTES = 524288

def _is_utf8_with_trimmed_edges(raw_bytes: bytes) -> bool:
    if len(raw_bytes) <= 6:
        return False
    for start in range(4):
        for end_trim in range(4):
            end = len(raw_bytes) - end_trim
            if end <= start:
                continue
            try:
                raw_bytes[start:end].decode("utf-8")
                return True
            except UnicodeDecodeError:
                pass
    return False

def _decode_process_output(raw_bytes: bytes, trim_incomplete_edges: bool = False) -> str:
    if not raw_bytes:
        return ""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # バイト切り詰め境界で不完全なUTF-8がcp932等へ誤フォールバックするのを防ぐ
        if trim_incomplete_edges and _is_utf8_with_trimmed_edges(raw_bytes):
            text = raw_bytes.decode("utf-8", errors="replace")
        else:
            preferred = (locale.getpreferredencoding(False) or "").strip()
            text = ""
            if preferred and preferred.lower() not in ("utf-8", "utf8"):
                try:
                    text = raw_bytes.decode(preferred)
                except (UnicodeDecodeError, LookupError):
                    text = ""
            if not text:
                text = raw_bytes.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")

def _truncate_shell_output(text: str, max_chars: int = MAX_SHELL_OUTPUT_CHARS) -> Tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    marker = "\n... (出力が長いため中略) ...\n"
    head_len = max_chars // 3
    tail_len = max(1, max_chars - head_len - len(marker))
    return text[:head_len] + marker + text[-tail_len:], True

def _safe_write_console(text: str, stream: Optional[TextIO] = None, flush: bool = False) -> None:
    target = sys.stdout if stream is None else stream
    try:
        target.write(text)
    except UnicodeEncodeError:
        encoding = getattr(target, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        target.write(safe_text)
    if flush:
        target.flush()

def _safe_print_line(text: str, stream: Optional[TextIO] = None) -> None:
    _safe_write_console(f"{text}\n", stream=stream, flush=True)

def _drain_process_stdout(pipe: Any, capture: Dict[str, Any]) -> None:
    head: bytearray = capture["head"]
    tail: bytearray = capture["tail"]
    lock: threading.Lock = capture["lock"]
    if pipe is None:
        return
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            with lock:
                if len(head) < _SHELL_CAPTURE_HEAD_BYTES:
                    need = _SHELL_CAPTURE_HEAD_BYTES - len(head)
                    head.extend(chunk[:need])
                    rem = chunk[need:]
                    if rem:
                        tail.extend(rem)
                else:
                    tail.extend(chunk)
                if len(tail) > _SHELL_CAPTURE_TAIL_BYTES * 2:
                    capture["byte_truncated"] = True
                    del tail[:-_SHELL_CAPTURE_TAIL_BYTES]
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass

def _format_captured_output(capture: Dict[str, Any]) -> Tuple[str, bool]:
    lock: threading.Lock = capture["lock"]
    with lock:
        head = bytes(capture["head"])
        tail = bytes(capture["tail"])
        byte_truncated = bool(capture.get("byte_truncated"))
    if not byte_truncated and len(tail) <= _SHELL_CAPTURE_TAIL_BYTES:
        decoded = _decode_process_output(head + tail, trim_incomplete_edges=False)
        return _truncate_shell_output(decoded)
    head_text = _decode_process_output(head, trim_incomplete_edges=True)
    tail_text = _decode_process_output(tail[-_SHELL_CAPTURE_TAIL_BYTES:], trim_incomplete_edges=True)
    marker = "\n... (出力過多のため中略) ...\n"
    combined = head_text + marker + tail_text
    output_text, _ = _truncate_shell_output(combined)
    return output_text, True

def _terminate_process_tree(proc: Any) -> None:
    # なぜ必要か: shell=Trueタイムアウト時に孫プロセスがPIPEを保持してハングするのを防ぐため
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass

def _is_bridge_shutting_down(bridge: Any, stop_event: Any) -> bool:
    return bool(getattr(bridge, "_shutting_down", False) or (stop_event is not None and stop_event.is_set()))

def _build_shutdown_shell_response(
    command: str,
    exec_cwd: Path,
    output_text: str = "",
    truncated: bool = False,
) -> Tuple[int, Dict[str, object]]:
    return int(HTTPStatus.SERVICE_UNAVAILABLE), {
        "ok": False,
        "exit_code": -1,
        "command": command,
        "cwd": str(exec_cwd),
        "output": output_text,
        "truncated": truncated,
        "timed_out": False,
        "message": "ブリッジサーバー終了のためコマンド実行を中止しました",
    }

def _validate_shell_command_request(
    bridge: Any,
    payload: Dict[str, object],
) -> Tuple[Optional[Tuple[str, int, Path]], Optional[Tuple[int, Dict[str, object]]]]:
    if "tab_id" in payload:
        tab_error = bridge._binding_manager.validate_execute_payload(payload)
        if tab_error is not None:
            return None, (int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error})

    raw_command = payload.get("command")
    if not isinstance(raw_command, str) or not raw_command.strip():
        return None, (int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "実行するコマンドを指定してください"})

    command = raw_command.strip()
    if "\x00" in command:
        return None, (int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "コマンドに不正な制御文字が含まれています"})
    if len(command) > MAX_SHELL_COMMAND_CHARS:
        return None, (
            int(HTTPStatus.BAD_REQUEST),
            {
                "ok": False,
                "message": f"コマンドが長すぎます (上限 {MAX_SHELL_COMMAND_CHARS} 文字)",
            },
        )

    timeout_sec = DEFAULT_SHELL_COMMAND_TIMEOUT_SEC
    raw_timeout = payload.get("timeout_sec")
    if raw_timeout is not None:
        if (
            isinstance(raw_timeout, bool)
            or not isinstance(raw_timeout, (int, float))
            or not math.isfinite(raw_timeout)
            or raw_timeout <= 0
        ):
            return None, (
                int(HTTPStatus.BAD_REQUEST),
                {
                    "ok": False,
                    "message": "timeout_sec は 0 より大きい有限の数値で指定してください",
                },
            )
        timeout_sec = max(1, min(MAX_SHELL_COMMAND_TIMEOUT_SEC, int(raw_timeout)))

    raw_cwd = getattr(bridge, "command_cwd", None) or bridge.project_root
    exec_cwd = Path(raw_cwd).resolve()
    if not exec_cwd.exists() or not exec_cwd.is_dir():
        return None, (
            int(HTTPStatus.BAD_REQUEST),
            {
                "ok": False,
                "message": f"実行ディレクトリが存在しません: {exec_cwd}",
            },
        )

    return (command, timeout_sec, exec_cwd), None

def _wait_for_shell_process(bridge: Any, proc: Any, stop_event: Any, timeout_sec: int) -> Tuple[bool, bool]:
    deadline = time.monotonic() + timeout_sec
    timed_out = False
    aborted = False
    while True:
        if _is_bridge_shutting_down(bridge, stop_event):
            aborted = True
            _terminate_process_tree(proc)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process_tree(proc)
            break
        try:
            proc.wait(timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    return timed_out, aborted

def handle_run_shell_command(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """拡張機能等から指定された任意コマンドをaiskel起動パス上で実行し、終了コードと出力を返す。"""
    validated, error_response = _validate_shell_command_request(bridge, payload)
    if error_response is not None or validated is None:
        assert error_response is not None
        return error_response
    command, timeout_sec, exec_cwd = validated

    stop_event = getattr(bridge, "_stop_event", None)
    if _is_bridge_shutting_down(bridge, stop_event):
        return _build_shutdown_shell_response(command, exec_cwd)

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    shell_lock = getattr(bridge, "_shell_cmd_lock", None) or bridge._exec_lock
    register_proc = getattr(bridge, "_register_shell_proc", None)
    unregister_proc = getattr(bridge, "_unregister_shell_proc", None)

    with shell_lock:
        if _is_bridge_shutting_down(bridge, stop_event):
            return _build_shutdown_shell_response(command, exec_cwd)

        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_cmd_log = sanitize_console_text(command, 200)
        _safe_print_line(f"\n[{timestamp}] 💻 拡張機能からコマンド実行 ({exec_cwd}): {safe_cmd_log}")

        raw_tab_id = payload.get("tab_id")
        req_tab_id = raw_tab_id.strip() if isinstance(raw_tab_id, str) else ""

        proc: Any = None
        reader_thread: Optional[threading.Thread] = None
        with bridge_command_scope(
            bridge,
            kind=BridgeCommandKind.RUN_COMMAND.value,
            command=command,
            tab_id=req_tab_id,
        ):
            try:
                popen_kwargs: Dict[str, Any] = {
                    "shell": True,
                    "cwd": exec_cwd,
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "env": env,
                }
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                else:
                    popen_kwargs["start_new_session"] = True

                proc = subprocess.Popen(command, **popen_kwargs)
                if callable(register_proc):
                    register_proc(proc)
                capture: Dict[str, Any] = {
                    "head": bytearray(),
                    "tail": bytearray(),
                    "byte_truncated": False,
                    "lock": threading.Lock(),
                }
                reader_thread = threading.Thread(
                    target=_drain_process_stdout,
                    args=(proc.stdout, capture),
                    daemon=True,
                )
                reader_thread.start()

                timed_out, aborted = _wait_for_shell_process(bridge, proc, stop_event, timeout_sec)

                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
                reader_thread.join(timeout=1.0)

                output_text, truncated = _format_captured_output(capture)

                if aborted:
                    return _build_shutdown_shell_response(command, exec_cwd, output_text=output_text, truncated=truncated)

                if timed_out:
                    status_msg = f"コマンド実行がタイムアウトしました ({timeout_sec}秒)"
                    _safe_print_line(f"[{datetime.now().strftime('%H:%M:%S')}] ✖ {status_msg}", stream=sys.stderr)
                    return int(HTTPStatus.OK), {
                        "ok": False,
                        "exit_code": -1,
                        "command": command,
                        "cwd": str(exec_cwd),
                        "output": output_text,
                        "truncated": truncated,
                        "timed_out": True,
                        "message": status_msg,
                    }

                exit_code = int(proc.returncode) if proc.returncode is not None else -1
                if output_text.strip():
                    preview = output_text if len(output_text) <= 4000 else output_text[-4000:]
                    _safe_write_console(preview, flush=False)
                    if not preview.endswith("\n"):
                        _safe_write_console("\n", flush=False)
                    sys.stdout.flush()
                status_msg = (
                    "コマンドを実行しました (終了コード 0)"
                    if exit_code == 0
                    else f"コマンドが終了コード {exit_code} で終了しました"
                )
                _safe_print_line(f"[{datetime.now().strftime('%H:%M:%S')}] {status_msg}")
                return int(HTTPStatus.OK), {
                    "ok": exit_code == 0,
                    "exit_code": exit_code,
                    "command": command,
                    "cwd": str(exec_cwd),
                    "output": output_text,
                    "truncated": truncated,
                    "timed_out": False,
                    "message": status_msg,
                }
            except Exception as exc:
                status_msg = f"コマンドの実行に失敗しました: {exc}"
                _safe_print_line(f"[{datetime.now().strftime('%H:%M:%S')}] ✖ {status_msg}", stream=sys.stderr)
                return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                    "ok": False,
                    "exit_code": -1,
                    "command": command,
                    "cwd": str(exec_cwd),
                    "output": "",
                    "truncated": False,
                    "timed_out": False,
                    "message": status_msg,
                }
            finally:
                if proc is not None:
                    if proc.poll() is None:
                        _terminate_process_tree(proc)
                        try:
                            proc.wait(timeout=1.0)
                        except Exception:
                            pass
                    if callable(unregister_proc):
                        unregister_proc(proc)
                    if proc.stdout is not None:
                        try:
                            proc.stdout.close()
                        except Exception:
                            pass
                if reader_thread is not None and reader_thread.is_alive():
                    reader_thread.join(timeout=0.5)
