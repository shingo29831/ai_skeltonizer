"""Module: @role: ブリッジ経由のaiskel cコマンド抽出・安全実行およびOSネイティブ自動送信フォールバック処理を担当する。"""
import contextlib
import sys
from datetime import datetime
from http import HTTPStatus
from typing import Any, Dict, List, Tuple

from .aistudio_bridge_shell import (
    DEFAULT_SHELL_COMMAND_TIMEOUT_SEC,
    MAX_SHELL_COMMAND_CHARS,
    MAX_SHELL_COMMAND_TIMEOUT_SEC,
    MAX_SHELL_OUTPUT_CHARS,
    _SHELL_CAPTURE_HEAD_BYTES,
    _SHELL_CAPTURE_TAIL_BYTES,
    _decode_process_output,
    _drain_process_stdout,
    _format_captured_output,
    _is_utf8_with_trimmed_edges,
    _safe_print_line,
    _safe_write_console,
    _terminate_process_tree,
    _truncate_shell_output,
    handle_run_shell_command,
)
from .aistudio_bridge_types import (
    BridgeExecutionResult,
    TeeTextIO,
    extract_error_detail,
    normalize_system_exit_code,
)
from .aistudio_command import (
    CommandValidationError,
    build_safe_copy_argv,
    extract_aiskel_copy_commands,
)

def normalize_incoming_commands(payload: Dict[str, object]) -> List[str]:
    """受信ペイロードから実行対象となるaiskel cコマンド文字列一覧を抽出・正規化する。"""
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

def handle_execute_command(bridge: Any, payload: Dict[str, object]) -> Tuple[int, BridgeExecutionResult]:
    """受信JSONペイロードを検証し、安全なaiskel cコマンドを排他実行する。"""
    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), BridgeExecutionResult(
            ok=False,
            exit_code=1,
            command="",
            argv=(),
            message=tab_error,
            skipped=True,
        )

    commands = bridge._normalize_incoming_commands(payload)
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
    effective_no_focus = bridge.no_focus or (page_focused is False)
    effective_no_paste = bridge.no_paste or (page_focused is False)
    # --no-focus起動時でもフォーカス中ならエラー詳細のクリップボード保存を維持するため非フォーカス時のみ抑止
    effective_no_error_clipboard = page_focused is False

    try:
        argv, display_cmd = build_safe_copy_argv(
            commands,
            project_root=bridge.project_root,
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

    target_title_keywords = bridge._resolve_target_title_keywords(payload)
    # タブ側が既にフォアグラウンドのときは別ブラウザ・別ウィンドウへのフォーカス奪取を防ぐ
    focus_browser = (not effective_no_focus) and (page_focused is not True)

    with bridge._exec_lock:
        if dedup_key and dedup_key in bridge._recent_keys:
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

        tee_stderr = TeeTextIO(sys.stderr)
        system_exit_note = ""
        exec_pasted = False
        exec_paste_reason = ""
        with contextlib.redirect_stderr(tee_stderr):
            try:
                if bridge.structured_command_runner is not None:
                    raw_res = bridge.structured_command_runner(
                        argv,
                        focus_browser=focus_browser,
                        allowed_title_keywords=target_title_keywords,
                    )
                else:
                    raw_res = bridge.command_runner(argv)

                if isinstance(raw_res, int):
                    exit_code = int(raw_res)
                else:
                    exit_code = int(getattr(raw_res, "exit_code", 1))
                    exec_pasted = bool(getattr(raw_res, "pasted", False))
                    exec_paste_reason = str(getattr(raw_res, "paste_reason", "") or "")
            except SystemExit as e:
                # argparse等のSystemExitでブリッジワーカーループが崩壊するのを防止
                exit_code = normalize_system_exit_code(e)
                if isinstance(e.code, str) and e.code.strip():
                    system_exit_note = e.code.strip()

        if exit_code == 0 and dedup_key:
            bridge._recent_keys.append(dedup_key)

        # なぜ必要か: aiskel c失敗時に自動コピーされたエラー内容も成功時同様に貼り付け・自動送信可能にするため
        paste_suppressed = effective_no_focus or effective_no_paste
        pasted = (not paste_suppressed) and exec_pasted
        paste_reason = "" if (pasted or paste_suppressed) else exec_paste_reason
        auto_run_allowed = pasted and (bridge._auto_run_mode != "disabled")
        is_auto_tab = bool(req_tab_id and bridge._handoff_manager.is_auto_generated_tab(req_tab_id))
        auto_run = pasted and (
            (bridge._auto_run_mode == "enabled")
            or (bridge._auto_run_mode != "disabled" and is_auto_tab)
        )

        if exit_code == 0:
            status_msg = "コマンドを実行しました"
        else:
            detail = extract_error_detail(tee_stderr.getvalue()) or system_exit_note
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

def handle_trigger_run(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """JS側のクリック送信が反応しない場合のOSネイティブCtrl+Enter送信フォールバックを実行する。"""
    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    if bridge._auto_run_mode == "disabled" or bridge.no_focus or bridge.no_paste:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "自動送信は無効化されています"}
    # page_focused欠落時の誤爆を防ぐため明示的にTrueのときのみ許可する
    if payload.get("page_focused") is not True:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "タブが非アクティブのため自動送信を中止しました"}

    target_title_keywords = bridge._resolve_target_title_keywords(payload)
    with bridge._exec_lock:
        res = bridge.prompt_run_trigger(
            interactive_only=True,
            allowed_title_keywords=target_title_keywords,
        )
        ok = bool(getattr(res, "success", False))
        reason = str(getattr(res, "reason", "") or "")
        return int(HTTPStatus.OK), {
            "ok": ok,
            "message": "Ctrl+Enterを送信しました" if ok else (reason or "OSショートカット送信に失敗しました"),
        }

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

def handle_run_shell_command(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """拡張機能等から指定された任意コマンドをaiskel起動パス上で実行し、終了コードと出力を返す。"""
    if "tab_id" in payload:
        tab_error = bridge._binding_manager.validate_execute_payload(payload)
        if tab_error is not None:
            return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    raw_command = payload.get("command")
    if not isinstance(raw_command, str) or not raw_command.strip():
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "実行するコマンドを指定してください"}

    command = raw_command.strip()
    if "\x00" in command:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "コマンドに不正な制御文字が含まれています"}
    if len(command) > MAX_SHELL_COMMAND_CHARS:
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": f"コマンドが長すぎます (上限 {MAX_SHELL_COMMAND_CHARS} 文字)",
        }

    timeout_sec = DEFAULT_SHELL_COMMAND_TIMEOUT_SEC
    raw_timeout = payload.get("timeout_sec")
    if raw_timeout is not None:
        if (
            isinstance(raw_timeout, bool)
            or not isinstance(raw_timeout, (int, float))
            or not math.isfinite(raw_timeout)
            or raw_timeout <= 0
        ):
            return int(HTTPStatus.BAD_REQUEST), {
                "ok": False,
                "message": "timeout_sec は 0 より大きい有限の数値で指定してください",
            }
        timeout_sec = max(1, min(MAX_SHELL_COMMAND_TIMEOUT_SEC, int(raw_timeout)))

    raw_cwd = getattr(bridge, "command_cwd", None) or bridge.project_root
    exec_cwd = Path(raw_cwd).resolve()
    if not exec_cwd.exists() or not exec_cwd.is_dir():
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": f"実行ディレクトリが存在しません: {exec_cwd}",
        }

    stop_event = getattr(bridge, "_stop_event", None)
    if getattr(bridge, "_shutting_down", False) or (stop_event is not None and stop_event.is_set()):
        return int(HTTPStatus.SERVICE_UNAVAILABLE), {
            "ok": False,
            "exit_code": -1,
            "command": command,
            "cwd": str(exec_cwd),
            "output": "",
            "truncated": False,
            "timed_out": False,
            "message": "ブリッジサーバー終了のためコマンド実行を中止しました",
        }

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    shell_lock = getattr(bridge, "_shell_cmd_lock", None) or bridge._exec_lock
    register_proc = getattr(bridge, "_register_shell_proc", None)
    unregister_proc = getattr(bridge, "_unregister_shell_proc", None)

    with shell_lock:
        if getattr(bridge, "_shutting_down", False) or (stop_event is not None and stop_event.is_set()):
            return int(HTTPStatus.SERVICE_UNAVAILABLE), {
                "ok": False,
                "exit_code": -1,
                "command": command,
                "cwd": str(exec_cwd),
                "output": "",
                "truncated": False,
                "timed_out": False,
                "message": "ブリッジサーバー終了のためコマンド実行を中止しました",
            }

        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_cmd_log = sanitize_console_text(command, 200)
        _safe_print_line(f"\n[{timestamp}] 💻 拡張機能からコマンド実行 ({exec_cwd}): {safe_cmd_log}")

        proc: Any = None
        reader_thread: Optional[threading.Thread] = None
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

            deadline = time.monotonic() + timeout_sec
            timed_out = False
            aborted = False
            while True:
                if getattr(bridge, "_shutting_down", False) or (stop_event is not None and stop_event.is_set()):
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

            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            reader_thread.join(timeout=1.0)

            output_text, truncated = _format_captured_output(capture)

            if aborted:
                status_msg = "ブリッジサーバー終了のためコマンド実行を中止しました"
                return int(HTTPStatus.SERVICE_UNAVAILABLE), {
                    "ok": False,
                    "exit_code": -1,
                    "command": command,
                    "cwd": str(exec_cwd),
                    "output": output_text,
                    "truncated": truncated,
                    "timed_out": False,
                    "message": status_msg,
                }

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
