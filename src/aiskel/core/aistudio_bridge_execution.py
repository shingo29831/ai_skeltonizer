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

        if exit_code == 0:
            if dedup_key:
                bridge._recent_keys.append(dedup_key)
            if hasattr(bridge._handoff_manager, "reset_no_patch_retries"):
                bridge._handoff_manager.reset_no_patch_retries(req_tab_id)

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
