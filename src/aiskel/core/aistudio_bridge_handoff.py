"""Module: @role: ブリッジ経由のパッチ引き継ぎ登録・取得・完了通知・自動適用および自動新タブ上限設定を担当する。"""
import sys
from datetime import datetime
from http import HTTPStatus
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from ..config import DEFAULT_PATCH_HANDOFF_MAX_TABS, load_patch_handoff_max_tabs_from_meta
from .aistudio_auto_apply import AutoApplyOutcome, execute_auto_check_patch_detailed
from .aistudio_auto_organize import validate_patch_against_rename_policy
from .aistudio_bridge_types import MAX_ERROR_DETAIL_CHARS, MAX_PATCH_HANDOFF_MAX_TABS
from .aistudio_config_infer import resolve_target_config_file, validate_config_infer_patch
from .aistudio_discovery_handoff import (
    advance_next_discovery_task_handoff,
    handle_discovery_handoff,
)
from .aistudio_handoff import (
    HandoffKind,
    has_applicable_patch_in_text,
    has_handoff_stop_signal_in_text,
    load_project_summary_text,
)
from .aistudio_handoff.task_queue import bridge_terminal_apply_scope
from .aistudio_handoff.text import extract_discovery_tasks
from .aistudio_prompts import (
    PATCH_HANDOFF_STOP_SIGNAL,
    PromptPattern,
    load_prompt_for_pattern,
)

DEFAULT_NEW_CHAT_URL = "https://aistudio.google.com/prompts/new_chat"

def get_patch_handoff_max_tabs(bridge: Any) -> int:
    """非自動生成タブにおけるパッチ検知時の新規タブ自動作成上限回数を返す。"""
    if bridge._explicit_patch_handoff_max_tabs is not None:
        return bridge._explicit_patch_handoff_max_tabs
    configured = load_patch_handoff_max_tabs_from_meta(bridge.project_root, bridge.output_dir)
    if configured is not None:
        return max(0, min(MAX_PATCH_HANDOFF_MAX_TABS, configured))
    return DEFAULT_PATCH_HANDOFF_MAX_TABS

def set_patch_handoff_max_tabs(bridge: Any, max_tabs: int) -> Dict[str, object]:
    """パッチ検知時の新規タブ自動作成上限回数を動的に更新し、最新の紐づけ情報を返す。"""
    normalized = max(0, min(MAX_PATCH_HANDOFF_MAX_TABS, int(max_tabs)))
    with bridge._exec_lock:
        old_limit = bridge.get_patch_handoff_max_tabs()
        bridge._explicit_patch_handoff_max_tabs = normalized
        bridge._handoff_manager.adjust_auto_tab_limits(old_limit, normalized)
        return bridge.get_binding_info()

def handle_handoff_max_tabs(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
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
    _, tab_err = bridge._validate_optional_tab_id(payload, required=False)
    if tab_err is not None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err}

    info = bridge.set_patch_handoff_max_tabs(raw_val)
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{timestamp}] ⚙ パッチ自動新タブ上限を変更しました: {raw_val} 回", flush=True)
    return int(HTTPStatus.OK), info

def _resolve_organize_allow_rename(bridge: Any, payload: Dict[str, object], req_tab_id: str) -> Tuple[bool, Optional[bool]]:
    raw_allow = payload.get("allow_rename")
    is_organize_mode = bool(payload.get("organize_mode"))
    if isinstance(raw_allow, bool):
        return True, raw_allow
    tracked = bridge._handoff_manager.get_tab_organize_policy(req_tab_id)
    if tracked is not None:
        return True, tracked
    return is_organize_mode, None

def _resolve_config_infer_target(bridge: Any, payload: Dict[str, object], req_tab_id: str) -> Optional[str]:
    # クライアントが設定推論モードを明示停止した場合はサーバー側の残留記録も解除する
    if payload.get("config_infer_mode") is False:
        if req_tab_id:
            bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
        return None
    tracked = bridge._handoff_manager.get_tab_config_infer_target(req_tab_id)
    if tracked:
        return tracked
    if payload.get("config_infer_mode") is True:
        raw_target = payload.get("config_infer_target")
        if isinstance(raw_target, str) and raw_target.strip():
            resolved_root = bridge.project_root.resolve()
            try:
                candidate = (resolved_root / raw_target.strip()).resolve()
                if candidate.is_relative_to(resolved_root) and candidate.name == "config.json":
                    return candidate.relative_to(resolved_root).as_posix()
            except (OSError, ValueError):
                pass
        _, default_rel = resolve_target_config_file(bridge.project_root, bridge.output_dir)
        return default_rel
    return None

def run_terminal_auto_apply(
    bridge: Any,
    *,
    req_tab_id: str,
    target_patch: str,
    has_patch: bool,
    stop_signal_detected: bool,
    limit_reached: bool,
    remaining_auto_tabs: int,
    success_message: str,
    log_banner: str,
    allow_rename: Optional[bool] = None,
    config_infer_target: Optional[str] = None,
) -> Tuple[int, Dict[str, object]]:
    """最終フェーズ到達時のクリップボード保存およびaiskel a自動適用を実行する。"""
    copied_to_clipboard = False
    try:
        bridge.clipboard_writer(target_patch)
        copied_to_clipboard = True
    except Exception as e:
        print(f"⚠ 応答のクリップボードコピーに失敗しました: {e}", file=sys.stderr, flush=True)

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{timestamp}] {log_banner}", flush=True)
    policy_violation = (
        validate_config_infer_patch(target_patch, bridge.project_root, target_rel_path=config_infer_target)
        if config_infer_target
        else (
            validate_patch_against_rename_policy(target_patch, bridge.project_root, allow_rename=False)
            if allow_rename is False
            else None
        )
    )
    if policy_violation is not None:
        apply_exit_code = 1
        apply_message, apply_error_report = policy_violation
        apply_phase = "config_policy" if config_infer_target else "organize_policy"
        apply_test_failed = False
    elif hasattr(bridge, "_execute_auto_apply_detailed"):
        begin_fn = getattr(bridge, "begin_terminal_apply", None)
        end_fn = getattr(bridge, "end_terminal_apply", None)
        if callable(begin_fn):
            begin_fn()
        try:
            with bridge_terminal_apply_scope():
                if config_infer_target:
                    outcome = bridge._execute_auto_apply_detailed(
                        target_patch,
                        no_test=True,
                        no_static_analysis=True,
                        allow_config_write=True,
                        config_target=config_infer_target,
                    )
                else:
                    outcome = bridge._execute_auto_apply_detailed(target_patch)
        finally:
            if callable(end_fn):
                end_fn()
        apply_exit_code = outcome.exit_code
        apply_message = outcome.message
        apply_phase = outcome.phase
        apply_error_report = outcome.error_report
        apply_test_failed = bool(getattr(outcome, "test_failed", False))
    else:
        begin_fn = getattr(bridge, "begin_terminal_apply", None)
        end_fn = getattr(bridge, "end_terminal_apply", None)
        if callable(begin_fn):
            begin_fn()
        try:
            with bridge_terminal_apply_scope():
                apply_exit_code, apply_message = bridge._execute_auto_apply(target_patch)
        finally:
            if callable(end_fn):
                end_fn()
        apply_phase = "apply"
        apply_error_report = "" if apply_exit_code == 0 else apply_message
        apply_test_failed = False
    applied = apply_exit_code == 0
    next_task_info: Dict[str, object] = {}
    if applied:
        tab_mode = (
            bridge._handoff_manager.get_tab_mode(req_tab_id)
            if hasattr(bridge._handoff_manager, "get_tab_mode")
            else None
        )
        if hasattr(bridge._handoff_manager, "set_tab_mode"):
            bridge._handoff_manager.set_tab_mode(req_tab_id, None)
        bridge._handoff_manager.consume_detected_patch(req_tab_id, target_patch)
        bridge._handoff_manager.set_tab_organize_policy(req_tab_id, None)
        bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
        if hasattr(bridge._handoff_manager, "reset_no_patch_retries"):
            bridge._handoff_manager.reset_no_patch_retries(req_tab_id)
        if (
            not config_infer_target
            and allow_rename is None
            and tab_mode == HandoffKind.DISCOVERY.value
        ):
            next_task_info = advance_next_discovery_task_handoff(
                bridge,
                source_tab_id=req_tab_id,
                prev_apply_message=apply_message,
                prev_test_failed=apply_test_failed,
                store_pending_external=False,
            )
            if next_task_info.get("error"):
                print(
                    f"⚠ 次タスクの引き継ぎ生成に失敗しました: {next_task_info['error']}",
                    file=sys.stderr,
                    flush=True,
                )

    resp_body: Dict[str, object] = {
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
        "apply_phase": apply_phase,
        "apply_message": apply_message,
        "apply_error_report": apply_error_report,
        "apply_test_failed": apply_test_failed,
        "message": success_message if applied else apply_message,
    }
    if next_task_info.get("advanced"):
        resp_body["next_task_handoff"] = True
        resp_body["handoff_id"] = next_task_info.get("handoff_id")
        resp_body["new_chat_url"] = next_task_info.get("new_chat_url")
        resp_body["task_index"] = next_task_info.get("task_index")
        resp_body["total_tasks"] = next_task_info.get("total_tasks")
        resp_body["target_file"] = next_task_info.get("target_file")
    elif next_task_info.get("all_tasks_completed"):
        resp_body["all_tasks_completed"] = True
        resp_body["total_tasks"] = next_task_info.get("total_tasks")
    elif next_task_info.get("error"):
        resp_body["next_task_error"] = str(next_task_info["error"])
    return int(HTTPStatus.OK), resp_body

def handle_handoff_create(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """<<<< ==== >>>> 出力応答を受領し、応答コピー・要約読込・引き継ぎレコード登録またはaiskel a自動適用を実行する。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=bridge._binding_manager.require_binding)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    raw_response = payload.get("response_text")
    response_text = raw_response if isinstance(raw_response, str) else ""
    stop_requested = (payload.get("stop_handoff") is True) or has_handoff_stop_signal_in_text(response_text)
    has_patch = bool(response_text.strip() and has_applicable_patch_in_text(response_text))
    current_tab_mode = (
        bridge._handoff_manager.get_tab_mode(req_tab_id)
        if req_tab_id and hasattr(bridge._handoff_manager, "get_tab_mode")
        else None
    )
    discovery_allowed_by_mode = (
        payload.get("discovery_mode") is True
        or (
            payload.get("discovery_mode") is not False
            and current_tab_mode not in (
                HandoffKind.DISCOVERY.value,
                HandoffKind.FILE_SPLIT.value,
                HandoffKind.DIR_ORGANIZE.value,
                HandoffKind.CONFIG_INFER.value,
            )
        )
    )
    has_discovery = bool(
        not has_patch
        and not stop_requested
        and discovery_allowed_by_mode
        and extract_discovery_tasks(response_text)
    )

    turn_id = str(payload.get("turn_id") or "").strip()
    new_chat_url = str(payload.get("new_chat_url") or DEFAULT_NEW_CHAT_URL).strip()
    parsed_new_url = urlsplit(new_chat_url)
    if parsed_new_url.scheme.lower() != "https" or (parsed_new_url.hostname or "").lower() != "aistudio.google.com":
        new_chat_url = DEFAULT_NEW_CHAT_URL

    if has_discovery:
        discovery_dedup_key = f"DISCOVERY::{req_tab_id}::{turn_id}" if turn_id else ""
        return handle_discovery_handoff(
            bridge,
            payload,
            req_tab_id=req_tab_id,
            response_text=response_text,
            new_chat_url=new_chat_url,
            dedup_key=discovery_dedup_key,
        )

    if not has_patch and not stop_requested:
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": "パッチ区切り記号(<<<< ==== >>>>)または有効な移動・名前変更ディレクティブを含む応答テキストがありません",
        }

    # コード修正パッチを出力したAI自身の終了判断は無効化し、次タブのAIへ判定を委ねる
    ignored_self_stop = False
    if has_patch and stop_requested:
        stop_requested = False
        ignored_self_stop = True

    dedup_key = f"HANDOFF::{req_tab_id}::{turn_id}" if turn_id else ""

    with bridge._exec_lock:
        if dedup_key and dedup_key in bridge._recent_keys:
            return int(HTTPStatus.OK), {
                "ok": True,
                "skipped": True,
                "duplicate_skipped": True,
                "stop_signal_detected": stop_requested,
                "has_patch": has_patch,
                "message": "同一ターンのパッチ引き継ぎは処理済みのためスキップしました",
            }

        is_organize_mode, allow_rename = _resolve_organize_allow_rename(bridge, payload, req_tab_id)
        if is_organize_mode and allow_rename is not None and req_tab_id:
            bridge._handoff_manager.set_tab_organize_policy(req_tab_id, allow_rename)
        config_infer_target = _resolve_config_infer_target(bridge, payload, req_tab_id)
        if config_infer_target and req_tab_id:
            bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, config_infer_target)

        max_tabs = bridge.get_patch_handoff_max_tabs()
        remaining_before = bridge._handoff_manager.get_remaining_auto_tabs(req_tab_id, default_limit=max_tabs)
        if stop_requested:
            if has_patch:
                bridge._handoff_manager.record_detected_patch(response_text, tab_id=req_tab_id)
            target_patch = response_text if has_patch else bridge._handoff_manager.get_last_detected_patch(req_tab_id)
            if not target_patch:
                tab_mode = (
                    bridge._handoff_manager.get_tab_mode(req_tab_id)
                    if req_tab_id and hasattr(bridge._handoff_manager, "get_tab_mode")
                    else None
                )
                next_task_info: Dict[str, object] = {}
                if (
                    not config_infer_target
                    and allow_rename is None
                    and tab_mode == HandoffKind.DISCOVERY.value
                ):
                    next_task_info = advance_next_discovery_task_handoff(
                        bridge,
                        source_tab_id=req_tab_id,
                        prev_apply_message="変更なし完了（完了合図検知）",
                        prev_test_failed=False,
                        store_pending_external=False,
                        prev_task_outcome="no_change",
                    )
                if not next_task_info.get("error"):
                    if req_tab_id:
                        if hasattr(bridge._handoff_manager, "set_tab_mode"):
                            bridge._handoff_manager.set_tab_mode(req_tab_id, None)
                        bridge._handoff_manager.set_tab_organize_policy(req_tab_id, None)
                        bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
                        if hasattr(bridge._handoff_manager, "reset_no_patch_retries"):
                            bridge._handoff_manager.reset_no_patch_retries(req_tab_id)
                    if dedup_key:
                        bridge._recent_keys.append(dedup_key)
                timestamp = datetime.now().strftime("%H:%M:%S")
                if next_task_info.get("error"):
                    print(
                        f"⚠ 次タスクの引き継ぎ生成に失敗しました: {next_task_info['error']}",
                        file=sys.stderr,
                        flush=True,
                    )
                elif not next_task_info.get("advanced") and not next_task_info.get("all_tasks_completed"):
                    print(
                        f"\n[{timestamp}] ✅ 欠陥なし完了合図({PATCH_HANDOFF_STOP_SIGNAL})を検知 -> 適用対象の置換コードがないため新規タブは作成しません",
                        flush=True,
                    )
                resp_body: Dict[str, object] = {
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
                if next_task_info.get("advanced"):
                    task_idx = next_task_info.get("task_index")
                    task_tot = next_task_info.get("total_tasks")
                    resp_body["no_change_completed"] = True
                    resp_body["next_task_handoff"] = True
                    resp_body["handoff_id"] = next_task_info.get("handoff_id")
                    resp_body["new_chat_url"] = next_task_info.get("new_chat_url")
                    resp_body["task_index"] = task_idx
                    resp_body["total_tasks"] = task_tot
                    resp_body["target_file"] = next_task_info.get("target_file")
                    resp_body["message"] = f"完了合図により変更なし完了とし、次タスク ({task_idx}/{task_tot}) へ引き継ぎます"
                elif next_task_info.get("all_tasks_completed"):
                    resp_body["no_change_completed"] = True
                    resp_body["all_tasks_completed"] = True
                    resp_body["total_tasks"] = next_task_info.get("total_tasks")
                    resp_body["message"] = "完了合図により変更なし完了とし、全分割タスクが完了しました"
                elif next_task_info.get("error"):
                    err_text = str(next_task_info["error"])
                    resp_body["next_task_error"] = err_text
                    resp_body["message"] = f"完了合図を検知しましたが、次タスクの引き継ぎ生成に失敗しました: {err_text}"
                return int(HTTPStatus.OK), resp_body
            # 最終適用フェーズ到達後は適用失敗リトライ時に再度レビュー新タブを開かないよう残数を確定消費
            bridge._handoff_manager.consume_auto_tab_if_tracked(req_tab_id, 0)
            status_code, body = bridge._run_terminal_auto_apply(
                req_tab_id=req_tab_id,
                target_patch=target_patch,
                has_patch=has_patch,
                stop_signal_detected=True,
                limit_reached=False,
                remaining_auto_tabs=0,
                success_message="完了合図を検知したため最後に検知した置換コードを aiskel a で適用しました",
                log_banner=f"✅ 欠陥なし完了合図({PATCH_HANDOFF_STOP_SIGNAL})を検知 -> 最後に検知した置換コードを aiskel a で実行します",
                allow_rename=allow_rename,
                config_infer_target=config_infer_target,
            )
            if dedup_key:
                bridge._recent_keys.append(dedup_key)
            return status_code, body

        if remaining_before <= 0:
            if ignored_self_stop:
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(
                    f"\n[{timestamp}] ⚠ 修正パッチを含む応答内の終了合図は無効化しました（自動新タブ上限到達のため適用へ進みます）",
                    flush=True,
                )
            bridge._handoff_manager.record_detected_patch(response_text, tab_id=req_tab_id)
            status_code, body = bridge._run_terminal_auto_apply(
                req_tab_id=req_tab_id,
                target_patch=response_text,
                has_patch=True,
                stop_signal_detected=False,
                limit_reached=True,
                remaining_auto_tabs=0,
                success_message="新規タブ自動作成の回数制限に達したため最後に検知した置換コードを aiskel a で適用しました",
                log_banner="📋 パッチ出力(<<<< ==== >>>>)を検知（新規タブ自動作成の回数制限到達） -> 最後に検知した置換コードを aiskel a で実行します",
                allow_rename=allow_rename,
                config_infer_target=config_infer_target,
            )
            body["self_stop_ignored"] = ignored_self_stop
            if dedup_key:
                bridge._recent_keys.append(dedup_key)
            return status_code, body

        check_outcome: Optional[AutoApplyOutcome] = None
        if hasattr(bridge, "_execute_auto_check"):
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{timestamp}] 🔍 パッチ出力(<<<< ==== >>>>)を検知 -> aiskel a --check を実行します", flush=True)
            check_outcome = (
                bridge._execute_auto_check(
                    response_text,
                    allow_config_write=True,
                    config_target=config_infer_target,
                )
                if config_infer_target
                else bridge._execute_auto_check(response_text)
            )
        elif getattr(bridge, "command_runner", None) is not None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{timestamp}] 🔍 パッチ出力(<<<< ==== >>>>)を検知 -> aiskel a --check を実行します", flush=True)
            check_outcome = execute_auto_check_patch_detailed(
                project_root=bridge.project_root,
                patch_text=response_text,
                command_runner=bridge.command_runner,
                max_error_chars=MAX_ERROR_DETAIL_CHARS,
                allow_config_write=bool(config_infer_target),
                config_target=config_infer_target,
            )

        check_attempted = check_outcome is not None
        check_passed = True if check_outcome is None else (check_outcome.exit_code == 0)
        check_exit_code: Optional[int] = None if check_outcome is None else check_outcome.exit_code
        check_message = "" if check_outcome is None else check_outcome.message
        check_error_report = ""
        if check_outcome is not None and check_outcome.exit_code != 0:
            check_error_report = check_outcome.error_report or check_outcome.message

        policy_violation = (
            validate_config_infer_patch(response_text, bridge.project_root, target_rel_path=config_infer_target)
            if config_infer_target
            else (
                validate_patch_against_rename_policy(response_text, bridge.project_root, allow_rename=False)
                if allow_rename is False
                else None
            )
        )
        if policy_violation is not None:
            policy_short, policy_report = policy_violation
            check_passed = False
            check_exit_code = 1 if (check_exit_code is None or check_exit_code == 0) else check_exit_code
            check_message = f"{check_message} / {policy_short}" if check_message else policy_short
            check_error_report = f"{check_error_report}\n\n{policy_report}".strip()

        try:
            summary_text = load_project_summary_text(bridge.project_root)
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"要約バンドルの取得に失敗しました: {e}",
            }

        copied_to_clipboard = False
        try:
            bridge.clipboard_writer(response_text)
            copied_to_clipboard = True
        except Exception as e:
            print(f"⚠ 応答のクリップボードコピーに失敗しました: {e}", file=sys.stderr, flush=True)

        bridge._handoff_manager.record_detected_patch(response_text, tab_id=req_tab_id)
        next_remaining = remaining_before - 1
        review_prompt = load_prompt_for_pattern(bridge.project_root, PromptPattern.PATCH_REVIEW)
        if config_infer_target:
            config_note = (
                f"=== 設定ファイル推論モード補足 ({config_infer_target}) ===\n"
                f"現在は `{config_infer_target}` のみを更新する設定推論モードです。"
                "有効なJSON構文であること、プロジェクトに実在しない架空のテスト/静的解析コマンドを設定していないこと、"
                "および他ファイルへの変更が含まれていないことを必ず確認してください。"
            )
            review_prompt = f"{review_prompt}\n\n{config_note}"
        elif is_organize_mode:
            if allow_rename is True:
                policy_title = "ファイル名変更許可・段階的整理"
                policy_detail = (
                    "現在は**段階的階層整理モード（ファイル名変更許可）**です。"
                    "移動・リネームに伴うimport・参照更新漏れがないか確認してください。"
                )
            elif allow_rename is False:
                policy_title = "ファイル名変更禁止・段階的整理"
                policy_detail = (
                    "現在は**段階的階層整理モード（ファイル名変更禁止）**です。"
                    "ファイル名（basename）を変更せずディレクトリ移動のみになっているか必ず確認してください。"
                )
            else:
                policy_title = "段階的整理"
                policy_detail = "現在は**段階的階層整理モード**です。"
            # レビューAIが未選択の残存ファイルまで一括移動させようとする暴走を防止
            incremental_note = (
                "対象ディレクトリの負荷とトークン消費を抑えるため、AIが**1つの責務グループのみを選択して少しずつ整理する段階的整理**を行っています。\n"
                "- 対象ディレクトリ直下に未選択のファイルが多数残っていてもそれは意図通り（後続ステップで整理）であるため、未選択ファイルを勝手に追加移動させないでください。\n"
                "- 今回選択されたファイル群の移動先の妥当性、新規サブディレクトリ直下の `__init__.py`（`@role` 記述）、およびプロジェクト全体の `import`・参照更新漏れがないかのみを厳密に検証してください。"
            )
            policy_note = f"=== 階層整理モード補足（{policy_title}）===\n{policy_detail}\n{incremental_note}"
            review_prompt = f"{review_prompt}\n\n{policy_note}"
        handoff = bridge._handoff_manager.create(
            source_tab_id=req_tab_id,
            new_chat_url=new_chat_url,
            response_text=response_text,
            summary_text=summary_text,
            review_prompt=review_prompt,
            remaining_auto_tabs=next_remaining,
            check_error=check_error_report or None,
            source_command_notice=check_message if (check_attempted or policy_violation is not None) else "",
            source_command_is_error=(check_attempted or policy_violation is not None) and (not check_passed),
            allow_rename=allow_rename,
            config_infer_target=config_infer_target,
        )
        bridge._handoff_manager.consume_auto_tab_if_tracked(req_tab_id, next_remaining)
        if dedup_key:
            bridge._recent_keys.append(dedup_key)

        timestamp = datetime.now().strftime("%H:%M:%S")
        if ignored_self_stop:
            print(
                f"\n[{timestamp}] ⚠ 修正パッチを含む応答内の終了合図は無効化しました（次タブのAIが終了可否を判定します）",
                flush=True,
            )
        if check_attempted and not check_passed:
            print(
                f"\n[{timestamp}] ⚠ aiskel a --check でエラーを検出しました (終了コード: {check_exit_code}) -> 応答をコピーし、エラー結果を含めて新規タブへ引き継ぎます (新規タブ残り回数: {next_remaining})",
                flush=True,
            )
        else:
            print(
                f"\n[{timestamp}] 📋 パッチ出力(<<<< ==== >>>>)を検知 -> 応答をコピーし、新規タブへ要約とともに引き継ぎます (新規タブ残り回数: {next_remaining})",
                flush=True,
            )
        return int(HTTPStatus.OK), {
            "ok": True,
            "skipped": False,
            "limit_reached": False,
            "self_stop_ignored": ignored_self_stop,
            "remaining_auto_tabs": next_remaining,
            "copied_to_clipboard": copied_to_clipboard,
            "check_attempted": check_attempted,
            "check_passed": check_passed,
            "check_exit_code": check_exit_code,
            "check_message": check_message,
            "check_error_report": check_error_report,
            "handoff_id": handoff.handoff_id,
            "new_chat_url": handoff.new_chat_url,
            "message": "パッチ応答をコピーし、新規タブ引き継ぎを準備しました",
        }

def handle_handoff_claim(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """新規タブからの引き継ぎ取得要求を処理し、必要時は新規タブ紐づけも同時に確定する。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=bridge._binding_manager.require_binding)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    raw_handoff_id = payload.get("handoff_id")
    if raw_handoff_id is not None and not isinstance(raw_handoff_id, str):
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "handoff_id を文字列で指定してください"}
    handoff_id = raw_handoff_id.strip() if isinstance(raw_handoff_id, str) else ""

    item = bridge._handoff_manager.claim(handoff_id=handoff_id, tab_id=req_tab_id)
    if item is None:
        return int(HTTPStatus.OK), {"ok": True, "found": False}

    binding_info: Optional[Dict[str, object]] = None
    if req_tab_id:
        current_info = bridge.get_binding_info()
        if not current_info.get("bound") or current_info.get("tab_id") != req_tab_id:
            try:
                binding_info = bridge.bind_tab(
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

def handle_handoff_ack(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """新規タブへの貼り付け完了通知を受理し、引き継ぎレコードを解放する。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=False)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    raw_handoff_id = payload.get("handoff_id")
    if raw_handoff_id is not None and not isinstance(raw_handoff_id, str):
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "handoff_id を文字列で指定してください"}
    handoff_id = raw_handoff_id.strip() if isinstance(raw_handoff_id, str) else ""
    acked = bridge._handoff_manager.ack(handoff_id=handoff_id, tab_id=req_tab_id)
    return int(HTTPStatus.OK), {"ok": True, "acked": acked}
