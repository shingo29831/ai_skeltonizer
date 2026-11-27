"""Module: @role: コードブロックやコマンド未出力時の再試行指示・変更なし完了・安全停止制御を担当する。"""
import re
from datetime import datetime
from http import HTTPStatus
from typing import Any, Dict, Optional, Tuple

from .aistudio_command import extract_aiskel_copy_commands
from .aistudio_config_infer import resolve_target_config_file
from .aistudio_discovery_handoff import (
    advance_next_discovery_task_handoff,
    capture_task_handoff_xml_from_response,
)
from .aistudio_handoff.task_queue import DiscoveryTaskQueue, load_pending_task_queue
from .aistudio_handoff.text import (
    extract_discovery_tasks,
    has_applicable_patch_in_text,
    has_handoff_stop_signal_in_text,
)
from .aistudio_handoff.types import HandoffKind
from .aistudio_prompts import NO_OUTPUT_STOP_SIGNAL, PATCH_HANDOFF_STOP_SIGNAL

DEFAULT_NO_PATCH_MAX_RETRIES = 2
MAX_NO_PATCH_MAX_RETRIES = 5
VALID_NO_PATCH_MODES = frozenset({"auto", "no_change", "skip"})
NON_DISCOVERY_TASK_MODES = frozenset(
    {
        HandoffKind.FILE_SPLIT.value,
        HandoffKind.DIR_ORGANIZE.value,
        HandoffKind.CONFIG_INFER.value,
    }
)
_DISCOVERY_TASK_TAG_RE = re.compile(r"<task>\s*[\s\S]*?\s*</task>", re.IGNORECASE)

def _resolve_no_patch_organize_policy(bridge: Any, payload: Dict[str, object], req_tab_id: str) -> Tuple[bool, Optional[bool]]:
    raw_allow = payload.get("allow_rename")
    is_organize_mode = bool(payload.get("organize_mode"))
    if isinstance(raw_allow, bool):
        return True, raw_allow
    tracked = bridge._handoff_manager.get_tab_organize_policy(req_tab_id) if req_tab_id else None
    if tracked is not None:
        return True, tracked
    return is_organize_mode, None

def _resolve_no_patch_config_target(bridge: Any, payload: Dict[str, object], req_tab_id: str) -> Optional[str]:
    if payload.get("config_infer_mode") is False:
        if req_tab_id:
            bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
        return None
    tracked = bridge._handoff_manager.get_tab_config_infer_target(req_tab_id) if req_tab_id else None
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

def _build_no_patch_retry_report(
    *,
    tab_mode: Optional[str],
    has_pending_patch: bool,
    has_code_blocks: bool,
    config_infer_target: Optional[str],
    queue: Optional[DiscoveryTaskQueue],
    has_apply_command_mention: bool = False,
    is_first_stage_discovery: bool = False,
    has_task_tags: bool = False,
) -> str:
    if is_first_stage_discovery:
        missing_detail = (
            "`<task>...</task>` は出力されていましたが、対応する有効な `<dirs>...</dirs>`（実在パス指定）が見つかりませんでした。"
            if has_task_tags
            else "応答に有効な `<task>...</task>` と `<dirs>...</dirs>` のペアが含まれていませんでした。"
        )
        return (
            "=== 探索出力フォーマットエラー（<task> / <dirs> 未出力） ===\n"
            "現在は**1度目探索フェーズ（タスク分割・対象選定）**です。"
            "このフェーズでは `aiskel a` パッチや `aiskel c` コマンドは実行・出力しません。\n"
            f"{missing_detail}\n"
            "- 修正パッチ（`<<<<`・`====`・`>>>>`）や `aiskel c` コマンドは出力せず、"
            "先の `<ai_directory_index>` に記載された実在ディレクトリ・ファイルパスを指定して "
            "`<task>...</task>` と `<dirs>...</dirs>` の組を出力してください。\n"
            "- `...` や `path/to/dir1` などのダミーパスは使用しないでください。\n"
            f"- 対象なし・変更不要・質問のみで `<dirs>` を出力せず終了する場合は、空行を挟んで最末尾の単独行に `{NO_OUTPUT_STOP_SIGNAL}` を出力してください。"
        )
    task_hint = ""
    if queue is not None:
        task_hint = f"（現在タスク {queue.current_index + 1}/{queue.total_tasks}）"
    if has_pending_patch:
        return (
            "=== パッチ未出力エラー（レビュー応答） ===\n"
            "先のパッチに対するレビュー応答に、修正パッチ（コードブロック内の `ファイルパス:` / `<<<<`・`====`・`>>>>` / `<commit>`）"
            f"も完了合図（`{PATCH_HANDOFF_STOP_SIGNAL}`）も含まれていませんでした。\n"
            "- 修正が必要な場合は、現行コードベースへ単体適用できる修正パッチをコードブロックで出力してください。\n"
            f"- 修正が不要な場合は、コードブロック外の単独行に `{PATCH_HANDOFF_STOP_SIGNAL}` を出力してください。"
        )
    if config_infer_target:
        return (
            f"=== パッチ未出力エラー ({config_infer_target}) ===\n"
            f"`{config_infer_target}` に対する適用可能なパッチが出力されませんでした。\n"
            "コードブロック内に `ファイルパス:` / `<<<<`・`====`・`>>>>` / `<commit>` 形式でパッチを出力するか、"
            f"変更不要の場合のみコードブロック外に `{PATCH_HANDOFF_STOP_SIGNAL}` を単独行で出力してください。"
        )
    patch_format_note = (
        "`移動:` / `名前変更:` または `ファイルパス:` / `<<<<`・`====`・`>>>>` / `<commit>` 形式のパッチ"
        if tab_mode == HandoffKind.DIR_ORGANIZE.value
        else "`ファイルパス:` / `<<<<`・`====`・`>>>>` / `<commit>` 形式のパッチ"
    )
    if has_apply_command_mention:
        return (
            f"=== パッチ未添付エラー{task_hint} ===\n"
            f"`aiskel a` の実行指示が記載されていましたが、適用可能なパッチ（{patch_format_note}）が含まれていませんでした。\n"
            "`aiskel a` 単体ではパッチ本体なしに適用を実行できないため、以下を出力してください。\n"
            f"- 変更を適用する場合: コードブロック内に {patch_format_note} を出力してください。\n"
            f"- 変更が一切不要な場合のみ: コードブロック外の単独行に `{PATCH_HANDOFF_STOP_SIGNAL}` を出力してください。"
        )
    if has_code_blocks:
        return (
            f"=== パッチ形式不備エラー{task_hint} ===\n"
            f"応答内のコードブロックに `aiskel a` で適用可能なパッチ（{patch_format_note}）"
            "および `aiskel c` コマンドが含まれていませんでした。\n"
            "- コードを調査・取得する場合は `aiskel c ...` コマンドを出力してください。\n"
            "- 変更を適用する場合は、通常の解説用コードではなく `aiskel a` 準拠のパッチブロックを出力してください。\n"
            f"- 調査のみ・修正不要で変更が一切ない場合のみ、コードブロック外の単独行に `{PATCH_HANDOFF_STOP_SIGNAL}` を出力してください。"
        )
        return (
            f"=== コードブロック／コマンド未出力エラー{task_hint} ===\n"
            "推論結果にコードブロック・`aiskel c` コマンド・適用可能パッチのいずれも出力されなかったため、"
            "`aiskel a` を実行できず次の処理へ進めませんでした。\n"
            "以下のいずれかを必ず出力してください。\n"
            "1. 追加ファイル確認が必要な場合: コードブロック内に `aiskel c ...` コマンド\n"
            f"2. 実装・修正を行う場合: コードブロック内に {patch_format_note}\n"
            f"3. このタスクでコード変更が一切不要、または確認のみで終了する場合のみ: 空行を挟んでコードブロック外の最末尾単独行に `{NO_OUTPUT_STOP_SIGNAL}`（または `{PATCH_HANDOFF_STOP_SIGNAL}`）"
        )

def _clear_tab_task_state(bridge: Any, req_tab_id: str) -> None:
    if not req_tab_id:
        return
    if hasattr(bridge._handoff_manager, "set_tab_mode"):
        bridge._handoff_manager.set_tab_mode(req_tab_id, None)
    bridge._handoff_manager.set_tab_organize_policy(req_tab_id, None)
    bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
    if hasattr(bridge._handoff_manager, "reset_no_patch_retries"):
        bridge._handoff_manager.reset_no_patch_retries(req_tab_id)
    if hasattr(bridge._handoff_manager, "clear_task_handoff_xml"):
        bridge._handoff_manager.clear_task_handoff_xml(req_tab_id)

def _attach_next_task_fields(resp_body: Dict[str, object], next_info: Dict[str, object]) -> None:
    if next_info.get("advanced"):
        resp_body["handoff_id"] = next_info.get("handoff_id")
        resp_body["new_chat_url"] = next_info.get("new_chat_url")
        resp_body["task_index"] = next_info.get("task_index")
        resp_body["total_tasks"] = next_info.get("total_tasks")
        resp_body["target_file"] = next_info.get("target_file")
    elif next_info.get("all_tasks_completed"):
        resp_body["total_tasks"] = next_info.get("total_tasks")
    elif next_info.get("error"):
        resp_body["next_task_error"] = str(next_info["error"])

def handle_tasks_no_patch(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """コードブロックやコマンド未出力で推論が完了した際の再試行指示・変更なし完了・安全停止を制御する。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(
        payload,
        required=bridge._binding_manager.require_binding,
    )
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    raw_mode = str(payload.get("mode") or payload.get("completion_kind") or "auto").strip().lower()
    if raw_mode not in VALID_NO_PATCH_MODES:
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": f"不正な mode 指定です: {raw_mode}",
        }

    raw_max_retries = payload.get("max_retries")
    if raw_max_retries is None:
        max_retries = DEFAULT_NO_PATCH_MAX_RETRIES
    elif isinstance(raw_max_retries, bool) or not isinstance(raw_max_retries, int):
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": "max_retries を整数で指定してください",
        }
    else:
        max_retries = max(0, min(MAX_NO_PATCH_MAX_RETRIES, raw_max_retries))

    raw_response = payload.get("response_text")
    response_text = raw_response if isinstance(raw_response, str) else ""
    if response_text.strip():
        if has_applicable_patch_in_text(response_text):
            return int(HTTPStatus.BAD_REQUEST), {
                "ok": False,
                "message": "応答テキストに適用可能なパッチが含まれているため /api/handoff を使用してください",
            }
        if extract_aiskel_copy_commands(response_text):
            return int(HTTPStatus.BAD_REQUEST), {
                "ok": False,
                "message": "応答テキストに aiskel c コマンドが含まれているため /api/execute を使用してください",
            }

    stop_signal_detected = (payload.get("stop_handoff") is True) or (
        bool(response_text.strip()) and has_handoff_stop_signal_in_text(response_text)
    )
    effective_mode = "no_change" if (raw_mode == "auto" and stop_signal_detected) else raw_mode
    has_code_blocks = payload.get("has_code_blocks") is True
    has_apply_command_mention = payload.get("has_apply_command_mention") is True
    allow_advance_on_exhausted = payload.get("allow_advance_on_exhausted") is True
    store_pending_external = payload.get("store_pending_external") is True
    turn_id = str(payload.get("turn_id") or "").strip()
    dedup_key = f"NO_PATCH::{req_tab_id}::{effective_mode}::{turn_id}" if turn_id else ""

    with bridge._exec_lock:
        tab_mode = (
            bridge._handoff_manager.get_tab_mode(req_tab_id)
            if req_tab_id and hasattr(bridge._handoff_manager, "get_tab_mode")
            else None
        )
        if response_text.strip():
            discovery_allowed_by_mode = (
                payload.get("discovery_mode") is True
                or (
                    payload.get("discovery_mode") is not False
                    and tab_mode not in (HandoffKind.DISCOVERY.value, *NON_DISCOVERY_TASK_MODES)
                )
            )
            if discovery_allowed_by_mode and not stop_signal_detected and extract_discovery_tasks(response_text):
                return int(HTTPStatus.BAD_REQUEST), {
                    "ok": False,
                    "message": "応答テキストに探索ディレクティブ(<dirs>)が含まれているため /api/handoff を使用してください",
                }

        if dedup_key and dedup_key in bridge._recent_keys:
            return int(HTTPStatus.OK), {
                "ok": True,
                "skipped": True,
                "duplicate_skipped": True,
                "message": "同一ターンの未出力完了通知は処理済みのためスキップしました",
            }

        is_organize_mode, allow_rename = _resolve_no_patch_organize_policy(bridge, payload, req_tab_id)
        if is_organize_mode and allow_rename is not None and req_tab_id:
            bridge._handoff_manager.set_tab_organize_policy(req_tab_id, allow_rename)
        config_infer_target = _resolve_no_patch_config_target(bridge, payload, req_tab_id)
        if config_infer_target and req_tab_id:
            bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, config_infer_target)

        pending_patch = bridge._handoff_manager.get_last_detected_patch(req_tab_id)
        queue = bridge._handoff_manager.get_discovery_queue()
        if queue is None:
            queue = load_pending_task_queue(bridge.project_root, bridge.output_dir)
            if queue is not None:
                bridge._handoff_manager.set_discovery_queue(queue)
        has_queue = queue is not None
        has_next_task = bool(queue is not None and queue.has_next())
        is_first_stage_discovery = payload.get("discovery_mode") is True
        is_discovery_tab_eligible = (
            (tab_mode not in NON_DISCOVERY_TASK_MODES)
            and (not is_organize_mode)
            and (allow_rename is None)
            and (not config_infer_target)
            and (not is_first_stage_discovery)
        )

        if is_first_stage_discovery and stop_signal_detected:
            # 1度目探索が終了サインで完結した場合は既存キューや旧パッチへ干渉せず正常終了させる
            if req_tab_id and hasattr(bridge._handoff_manager, "reset_no_patch_retries"):
                bridge._handoff_manager.reset_no_patch_retries(req_tab_id)
            if dedup_key:
                bridge._recent_keys.append(dedup_key)
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(
                f"\n[{timestamp}] ✅ 1度目探索の終了合図を検知 -> タスク分割を行わず正常終了しました",
                flush=True,
            )
            return int(HTTPStatus.OK), {
                "ok": True,
                "action": "discovery_stopped",
                "skipped": True,
                "should_retry": False,
                "exhausted": False,
                "discovery_mode": True,
                "discovery_stopped": True,
                "stop_signal_detected": True,
                "has_queue": has_queue,
                "message": "終了合図を検知したため1度目探索を終了しました",
            }

        if effective_mode in ("no_change", "skip"):
            if effective_mode == "no_change" and pending_patch:
                return int(HTTPStatus.BAD_REQUEST), {
                    "ok": False,
                    "message": "保留中の適用対象パッチが存在するため、完了合図の適用には /api/handoff を使用してください",
                }

            if (
                has_queue
                and is_discovery_tab_eligible
                and (tab_mode == HandoffKind.DISCOVERY.value or raw_mode in ("no_change", "skip"))
            ):
                outcome_kind = "skipped" if effective_mode == "skip" else "no_change"
                if outcome_kind != "skipped" and response_text.strip():
                    capture_task_handoff_xml_from_response(
                        bridge,
                        tab_id=req_tab_id,
                        response_text=response_text,
                    )
                prev_msg = "タスクをスキップしました" if outcome_kind == "skipped" else "変更なし完了（パッチ出力なし）"
                next_info = advance_next_discovery_task_handoff(
                    bridge,
                    source_tab_id=req_tab_id,
                    prev_apply_message=prev_msg,
                    prev_test_failed=False,
                    store_pending_external=store_pending_external,
                    prev_task_outcome=outcome_kind,
                )
                has_error = bool(next_info.get("error"))
                if not has_error:
                    _clear_tab_task_state(bridge, req_tab_id)
                    if effective_mode == "skip" and pending_patch:
                        bridge._handoff_manager.consume_detected_patch(req_tab_id, pending_patch)
                    if dedup_key:
                        bridge._recent_keys.append(dedup_key)
                action = (
                    "advanced"
                    if next_info.get("advanced")
                    else ("completed" if next_info.get("all_tasks_completed") else "error")
                )
                resp_body: Dict[str, object] = {
                    "ok": True,
                    "action": action,
                    "should_retry": False,
                    "exhausted": False,
                    "has_queue": True,
                    "no_change_completed": (not has_error) and (outcome_kind == "no_change"),
                    "task_skipped": (not has_error) and (outcome_kind == "skipped"),
                    "stop_signal_detected": stop_signal_detected,
                    "next_task_handoff": bool(next_info.get("advanced")),
                    "all_tasks_completed": bool(next_info.get("all_tasks_completed")),
                    "message": (
                        f"タスク {next_info.get('task_index')}/{next_info.get('total_tasks')} へ進行します"
                        if next_info.get("advanced")
                        else (
                            f"分割タスク全 {next_info.get('total_tasks')} 件が完了しました"
                            if next_info.get("all_tasks_completed")
                            else f"次タスク引き継ぎに失敗しました: {next_info.get('error', '')}"
                        )
                    ),
                }
                _attach_next_task_fields(resp_body, next_info)
                return int(HTTPStatus.OK), resp_body

            _clear_tab_task_state(bridge, req_tab_id)
            if effective_mode == "skip" and pending_patch:
                bridge._handoff_manager.consume_detected_patch(req_tab_id, pending_patch)
            if dedup_key:
                bridge._recent_keys.append(dedup_key)
            return int(HTTPStatus.OK), {
                "ok": True,
                "action": "no_op",
                "skipped": True,
                "should_retry": False,
                "exhausted": False,
                "has_queue": has_queue,
                "stop_signal_detected": stop_signal_detected,
                "has_apply_command_mention": has_apply_command_mention,
                "message": "待機中の次タスクがないためスキップしました",
            }

        retry_count = (
            bridge._handoff_manager.increment_no_patch_retries(req_tab_id)
            if hasattr(bridge._handoff_manager, "increment_no_patch_retries")
            else 1
        )
        effective_tab_mode = (
            HandoffKind.DIR_ORGANIZE.value
            if (is_organize_mode and not tab_mode)
            else (HandoffKind.CONFIG_INFER.value if (config_infer_target and not tab_mode) else tab_mode)
        )
        active_queue_for_hint = queue if is_discovery_tab_eligible else None
        has_task_tags = bool(response_text.strip() and _DISCOVERY_TASK_TAG_RE.search(response_text))
        if retry_count <= max_retries:
            error_report = _build_no_patch_retry_report(
                tab_mode=effective_tab_mode,
                has_pending_patch=False if is_first_stage_discovery else bool(pending_patch),
                has_code_blocks=has_code_blocks,
                config_infer_target=config_infer_target,
                queue=active_queue_for_hint,
                has_apply_command_mention=has_apply_command_mention,
                is_first_stage_discovery=is_first_stage_discovery,
                has_task_tags=has_task_tags,
            )
            if dedup_key:
                bridge._recent_keys.append(dedup_key)
            timestamp = datetime.now().strftime("%H:%M:%S")
            detect_label = "1度目探索タスク(<task>/<dirs>)未出力" if is_first_stage_discovery else "コードブロック・パッチ未出力"
            print(
                f"\n[{timestamp}] ⚠ {detect_label}を検知 ({retry_count}/{max_retries}) -> 再送指示を返却します",
                flush=True,
            )
            return int(HTTPStatus.OK), {
                "ok": True,
                "action": "retry",
                "should_retry": True,
                "exhausted": False,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "discovery_mode": is_first_stage_discovery,
                "has_queue": has_queue,
                "has_next_task": has_next_task,
                "has_pending_patch": False if is_first_stage_discovery else bool(pending_patch),
                "tab_mode": effective_tab_mode or "",
                "apply_error_report": error_report,
                "retry_prompt": error_report,
                "message": f"{detect_label}のため再出力を要求します ({retry_count}/{max_retries})",
            }

        if allow_advance_on_exhausted and has_queue and is_discovery_tab_eligible:
            next_info = advance_next_discovery_task_handoff(
                bridge,
                source_tab_id=req_tab_id,
                prev_apply_message="コードブロック未出力スキップ（再試行上限到達）",
                prev_test_failed=False,
                store_pending_external=store_pending_external,
                prev_task_outcome="skipped",
            )
            has_error = bool(next_info.get("error"))
            if not has_error:
                _clear_tab_task_state(bridge, req_tab_id)
                if pending_patch:
                    bridge._handoff_manager.consume_detected_patch(req_tab_id, pending_patch)
                if dedup_key:
                    bridge._recent_keys.append(dedup_key)
            action = (
                "error"
                if has_error
                else ("completed" if next_info.get("all_tasks_completed") else "skipped_advance")
            )
            resp_body = {
                "ok": True,
                "action": action,
                "should_retry": False,
                "exhausted": True,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "has_queue": True,
                "task_skipped": not has_error,
                "next_task_handoff": bool(next_info.get("advanced")),
                "all_tasks_completed": bool(next_info.get("all_tasks_completed")),
                "message": (
                    f"次タスク引き継ぎに失敗しました: {next_info.get('error', '')}"
                    if has_error
                    else "再試行上限に達したため次タスクへ進行しました"
                ),
            }
            _attach_next_task_fields(resp_body, next_info)
            return int(HTTPStatus.OK), resp_body

        if dedup_key:
            bridge._recent_keys.append(dedup_key)
        timestamp = datetime.now().strftime("%H:%M:%S")
        stop_label = "1度目探索タスク(<task>/<dirs>)未出力" if is_first_stage_discovery else "コードブロック・パッチ未出力"
        print(
            f"\n[{timestamp}] ✖ {stop_label}が再試行上限 ({max_retries} 回) に達したため自動進行を停止しました",
            flush=True,
        )
        return int(HTTPStatus.OK), {
            "ok": True,
            "action": "exhausted",
            "should_retry": False,
            "exhausted": True,
            "retry_count": retry_count,
            "max_retries": max_retries,
            "discovery_mode": is_first_stage_discovery,
            "has_queue": has_queue,
            "has_next_task": has_next_task,
            "can_skip_task": has_queue and is_discovery_tab_eligible,
            "has_pending_patch": False if is_first_stage_discovery else bool(pending_patch),
            "tab_mode": effective_tab_mode or "",
            "task_index": (queue.current_index + 1) if (queue is not None and is_discovery_tab_eligible) else None,
            "total_tasks": queue.total_tasks if (queue is not None and is_discovery_tab_eligible) else None,
            "apply_error_report": "",
            "message": f"{stop_label}が連続したため自動進行を停止しました（再試行上限 {max_retries} 回到達）",
        }
