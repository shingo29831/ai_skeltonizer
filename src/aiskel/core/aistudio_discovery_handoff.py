"""Module: @role: 1度目探索応答(<dirs>/<task>)の検証・分割タスクキュー登録・次タスク引き継ぎを担当する。"""
import secrets
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..config import load_two_stage_config_from_meta
from .aistudio_bridge_state import sanitize_console_text
from .aistudio_handoff.task_queue import (
    DiscoveryTaskQueue,
    clear_pending_task_queue,
    load_pending_task_queue,
    prepare_task_combined_text,
    save_pending_task_queue,
)
from .aistudio_handoff.text import (
    extract_discovery_tasks,
    extract_original_user_request,
    extract_task_handoff_xml,
    format_discovery_handoff_text,
)
from .aistudio_handoff.types import HandoffKind
from .aistudio_prompts import NO_OUTPUT_STOP_SIGNAL, PromptPattern, load_prompt_for_pattern
from .scope_builder import build_scoped_bundle_text

def handle_discovery_handoff(
    bridge: Any,
    payload: Dict[str, object],
    *,
    req_tab_id: str,
    response_text: str,
    new_chat_url: str,
    dedup_key: str,
) -> Tuple[int, Dict[str, object]]:
    """1度目応答の<dirs>/<task>群を解析し、タスクキュー保持の上で先頭タスクの新規タブ引き継ぎを作成する。"""
    tasks = extract_discovery_tasks(response_text)
    if not tasks:
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": "有効な <dirs>...</dirs> ディレクティブが見つかりません",
        }
    first_task = tasks[0]
    organized_task = first_task.task_text
    dir_targets = list(first_task.dirs)
    total_tasks = len(tasks)

    with bridge._exec_lock:
        if dedup_key and dedup_key in bridge._recent_keys:
            return int(HTTPStatus.OK), {
                "ok": True,
                "skipped": True,
                "duplicate_skipped": True,
                "discovery_handoff": True,
                "message": "同一ターンの探索引き継ぎは処理済みのためスキップしました",
            }

        try:
            two_stage_cfg = load_two_stage_config_from_meta(bridge.project_root, bridge.output_dir)
            scoped_bundle_text, resolution = build_scoped_bundle_text(
                bridge.project_root,
                dir_targets,
                default_recursive=False,
                include_deps=two_stage_cfg.scope_include_deps,
            )
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"スコープ限定バンドルの生成に失敗しました: {e}",
            }

        if not scoped_bundle_text.strip():
            missing_list = ", ".join(resolution.missing_specs or dir_targets)
            err_report = (
                "=== 指定ディレクトリ解決エラー ===\n"
                f"1度目の応答の `<dirs>` で指定されたパスがプロジェクト内に存在しませんでした: {missing_list}\n"
                "先の `<ai_directory_index>` に記載された正確なディレクトリパスまたはファイルパスのみを指定し、"
                "`<task>...</task>` と `<dirs>...</dirs>` を再出力してください。\n"
                f"もし探索・変更が一切不要と判明した場合は、`<task>`/`<dirs>` を出力せず空行を挟んで最末尾の単独行に `{NO_OUTPUT_STOP_SIGNAL}` を出力してください。"
            )
            return int(HTTPStatus.OK), {
                "ok": True,
                "skipped": True,
                "discovery_error": True,
                "apply_attempted": True,
                "applied": False,
                "apply_phase": "discovery_scope",
                "apply_message": f"指定パスが見つかりません: {missing_list}",
                "apply_error_report": err_report,
                "message": f"指定されたディレクトリ・ファイルが見つかりませんでした: {missing_list}",
            }

        try:
            discovery_prompt = load_prompt_for_pattern(bridge.project_root, PromptPattern.DISCOVERY)
            second_stage_prompt = load_prompt_for_pattern(bridge.project_root, PromptPattern.DEFAULT)
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"プロンプト定義の読み込みに失敗しました: {e}",
            }

        raw_user_prompt = payload.get("user_prompt_text")
        original_request = extract_original_user_request(
            raw_user_prompt if isinstance(raw_user_prompt, str) else "",
            discovery_prompt=discovery_prompt,
        )

        plan_lines = None
        if total_tasks > 1:
            now = time.time()
            queue = DiscoveryTaskQueue(
                queue_id=secrets.token_urlsafe(12),
                project_root=str(bridge.project_root.resolve()),
                original_request=original_request,
                new_chat_url=new_chat_url,
                tasks=list(tasks),
                current_index=0,
                created_at=now,
                updated_at=now,
            )
            bridge._handoff_manager.set_discovery_queue(queue)
            bridge._handoff_manager.set_pending_external_task_handoff(None)
            if req_tab_id and hasattr(bridge._handoff_manager, "clear_task_handoff_xml"):
                bridge._handoff_manager.clear_task_handoff_xml(req_tab_id)
            save_pending_task_queue(bridge.project_root, queue, bridge.output_dir)
            plan_lines = queue.build_plan_lines(0)
        else:
            bridge._handoff_manager.clear_discovery_queue()
            clear_pending_task_queue(bridge.project_root, bridge.output_dir)

        combined_text = format_discovery_handoff_text(
            second_stage_prompt,
            scoped_bundle_text,
            original_request=original_request,
            organized_task=organized_task,
            missing_specs=resolution.missing_specs,
            task_index=1,
            total_tasks=total_tasks,
            task_plan_lines=plan_lines,
        )

        max_tabs = bridge.get_patch_handoff_max_tabs()
        target_summary = ", ".join(resolution.resolved_specs[:4])
        if len(resolution.resolved_specs) > 4:
            target_summary += f" (+{len(resolution.resolved_specs) - 4})"
        display_target = f"[1/{total_tasks}] {target_summary}" if total_tasks > 1 else target_summary
        notice_prefix = f"タスク 1/{total_tasks} 開始 · " if total_tasks > 1 else ""

        handoff = bridge._handoff_manager.create(
            source_tab_id=req_tab_id,
            new_chat_url=new_chat_url,
            response_text="",
            summary_text=scoped_bundle_text,
            remaining_auto_tabs=max_tabs,
            handoff_kind=HandoffKind.DISCOVERY,
            target_file=display_target,
            raw_combined_text=combined_text,
            source_command_notice=(
                f"{notice_prefix}スコープ限定バンドル生成完了: 対象 {len(resolution.primary_files)} 件 + 直接依存 {len(resolution.dependency_files)} 件"
            ),
            source_command_is_error=False,
        )
        if req_tab_id and hasattr(bridge._handoff_manager, "reset_no_patch_retries"):
            bridge._handoff_manager.reset_no_patch_retries(req_tab_id)
        if dedup_key:
            bridge._recent_keys.append(dedup_key)

    timestamp = datetime.now().strftime("%H:%M:%S")
    if total_tasks > 1:
        print(
            f"\n[{timestamp}] 🔎 探索結果(<dirs>)を検知 ({total_tasks} タスクに分割) -> "
            f"タスク 1/{total_tasks} (対象 {len(resolution.primary_files)} + 依存 {len(resolution.dependency_files)} ファイル) で新タブへ引き継ぎます",
            flush=True,
        )
    else:
        print(
            f"\n[{timestamp}] 🔎 探索結果(<dirs>)を検知 -> スコープ限定バンドル "
            f"(対象 {len(resolution.primary_files)} + 依存 {len(resolution.dependency_files)} ファイル) で2度目タブへ引き継ぎます",
            flush=True,
        )
    return int(HTTPStatus.OK), {
        "ok": True,
        "skipped": False,
        "discovery_handoff": True,
        "task_index": 1,
        "total_tasks": total_tasks,
        "remaining_auto_tabs": max_tabs,
        "handoff_id": handoff.handoff_id,
        "new_chat_url": handoff.new_chat_url,
        "target_file": display_target,
        "scoped_primary_count": len(resolution.primary_files),
        "scoped_dependency_count": len(resolution.dependency_files),
        "message": "スコープ限定バンドルと2度目プロンプトを新規タブへ引き継ぎます",
    }

def capture_task_handoff_xml_from_response(
    bridge: Any,
    *,
    tab_id: str,
    response_text: str,
) -> str:
    """AI応答に含まれる専用引き継ぎXMLを抽出し、タブ状態と分割タスクキューへ保存する。"""
    handoff_xml = extract_task_handoff_xml(response_text)
    if not handoff_xml:
        return ""
    tab_mode = (
        bridge._handoff_manager.get_tab_mode(tab_id)
        if tab_id and hasattr(bridge._handoff_manager, "get_tab_mode")
        else None
    )
    if tab_mode not in (None, HandoffKind.DISCOVERY.value):
        return ""
    if hasattr(bridge._handoff_manager, "record_task_handoff_xml"):
        bridge._handoff_manager.record_task_handoff_xml(tab_id, handoff_xml)
    queue = bridge._handoff_manager.get_discovery_queue()
    if queue is None:
        queue = load_pending_task_queue(bridge.project_root, bridge.output_dir)
        if queue is not None:
            bridge._handoff_manager.set_discovery_queue(queue)
    if queue is not None and 0 <= queue.current_index < queue.total_tasks:
        queue.handoff_contexts[queue.current_index] = handoff_xml
        queue.updated_at = time.time()
        save_pending_task_queue(bridge.project_root, queue, bridge.output_dir)
    return handoff_xml

def _create_task_handoff_record(
    bridge: Any,
    queue: DiscoveryTaskQueue,
    task_index_0: int,
    *,
    source_tab_id: str,
    prev_apply_message: str,
    prev_test_failed: bool,
    prev_task_outcome: str = "completed",
) -> Tuple[Any, str, str]:
    combined_text, target_summary, primary_cnt, dep_cnt = prepare_task_combined_text(
        bridge.project_root,
        bridge.output_dir,
        queue,
        task_index_0,
    )
    task_num = task_index_0 + 1
    total_tasks = queue.total_tasks
    max_tabs = bridge.get_patch_handoff_max_tabs()
    display_target = f"[{task_num}/{total_tasks}] {target_summary}"
    apply_note = prev_apply_message.strip() or "aiskel a でパッチを適用しました"
    if prev_task_outcome == "no_change":
        prefix_label = "前タスク変更なし完了"
    elif prev_task_outcome == "skipped":
        prefix_label = "前タスクスキップ"
    else:
        prefix_label = "前タスク適用完了"
    notice = (
        f"{prefix_label} ({apply_note}) -> タスク {task_num}/{total_tasks} を開始します "
        f"(対象 {primary_cnt} + 依存 {dep_cnt} ファイル)"
    )
    handoff = bridge._handoff_manager.create(
        source_tab_id=source_tab_id,
        new_chat_url=queue.new_chat_url,
        response_text="",
        summary_text="",
        remaining_auto_tabs=max_tabs,
        handoff_kind=HandoffKind.DISCOVERY,
        target_file=display_target,
        raw_combined_text=combined_text,
        source_command_notice=notice,
        source_command_is_error=prev_test_failed,
    )
    return handoff, display_target, target_summary

def advance_next_discovery_task_handoff(
    bridge: Any,
    *,
    source_tab_id: str = "",
    prev_apply_message: str = "",
    prev_test_failed: bool = False,
    store_pending_external: bool = False,
    prev_task_outcome: str = "completed",
) -> Dict[str, object]:
    """aiskel a成功後または変更なし完了後に次タスクが存在する場合は新タブ引き継ぎを作成し、全完了時はキューを解放する。"""
    queue = bridge._handoff_manager.get_discovery_queue()
    if queue is None:
        queue = load_pending_task_queue(bridge.project_root, bridge.output_dir)
        if queue is not None:
            bridge._handoff_manager.set_discovery_queue(queue)
    if queue is None:
        return {"has_queue": False, "advanced": False, "all_tasks_completed": False}

    normalized_outcome = prev_task_outcome if prev_task_outcome in ("completed", "no_change", "skipped") else "completed"
    if not queue.has_next():
        total = queue.total_tasks
        bridge._handoff_manager.clear_discovery_queue()
        clear_pending_task_queue(bridge.project_root, bridge.output_dir)
        timestamp = datetime.now().strftime("%H:%M:%S")
        done_verb = "の適用が完了しました" if normalized_outcome == "completed" else "の進行が完了しました"
        print(f"\n[{timestamp}] 🎉 分割タスク全 {total} 件{done_verb}！", flush=True)
        return {
            "has_queue": True,
            "advanced": False,
            "all_tasks_completed": True,
            "total_tasks": total,
            "prev_task_outcome": normalized_outcome,
        }

    next_index_0 = queue.current_index + 1
    prev_outcomes = dict(queue.task_outcomes)
    prev_contexts = dict(queue.handoff_contexts)
    if normalized_outcome == "skipped":
        # スキップ完了時は先行ターンで捕捉済みの引き継ぎXMLを次タスクへ引き渡さない
        queue.handoff_contexts.pop(queue.current_index, None)
    elif (
        queue.current_index not in queue.handoff_contexts
        and hasattr(bridge._handoff_manager, "get_task_handoff_xml")
    ):
        mgr_xml = bridge._handoff_manager.get_task_handoff_xml(source_tab_id)
        if isinstance(mgr_xml, str) and mgr_xml.strip():
            queue.handoff_contexts[queue.current_index] = mgr_xml.strip()
    if normalized_outcome != "completed":
        queue.task_outcomes[queue.current_index] = normalized_outcome
    try:
        handoff, display_target, target_summary = _create_task_handoff_record(
            bridge,
            queue,
            next_index_0,
            source_tab_id=source_tab_id,
            prev_apply_message=prev_apply_message,
            prev_test_failed=prev_test_failed,
            prev_task_outcome=normalized_outcome,
        )
    except (OSError, ValueError, RuntimeError) as e:
        queue.task_outcomes = prev_outcomes
        queue.handoff_contexts = prev_contexts
        return {
            "has_queue": True,
            "advanced": False,
            "all_tasks_completed": False,
            "error": str(e),
        }

    queue.current_index = next_index_0
    queue.updated_at = time.time()
    save_pending_task_queue(bridge.project_root, queue, bridge.output_dir)
    if hasattr(bridge._handoff_manager, "clear_task_handoff_xml"):
        bridge._handoff_manager.clear_task_handoff_xml(source_tab_id)

    task_num = next_index_0 + 1
    total_tasks = queue.total_tasks
    if store_pending_external:
        bridge._handoff_manager.set_pending_external_task_handoff(
            {
                "handoff_id": handoff.handoff_id,
                "new_chat_url": handoff.new_chat_url,
                "task_index": task_num,
                "total_tasks": total_tasks,
                "target_file": display_target,
            }
        )

    timestamp = datetime.now().strftime("%H:%M:%S")
    step_label = (
        "変更なし完了"
        if normalized_outcome == "no_change"
        else ("スキップ" if normalized_outcome == "skipped" else "適用完了")
    )
    print(
        f"\n[{timestamp}] 🚀 タスク {task_num - 1}/{total_tasks} {step_label} -> "
        f"タスク {task_num}/{total_tasks} ({target_summary}) を新規タブへ引き継ぎます",
        flush=True,
    )
    return {
        "has_queue": True,
        "advanced": True,
        "all_tasks_completed": False,
        "prev_task_outcome": normalized_outcome,
        "handoff_id": handoff.handoff_id,
        "new_chat_url": handoff.new_chat_url,
        "task_index": task_num,
        "total_tasks": total_tasks,
        "target_file": display_target,
    }

def handle_tasks_advance(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """外部CLIからのaiskel a成功通知を受理し、次タスクの新タブ引き継ぎを生成する。"""
    clean_tid, tab_err = bridge._validate_optional_tab_id(payload, required=False)
    if tab_err is not None or clean_tid is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    raw_root = payload.get("project_root")
    if isinstance(raw_root, str) and raw_root.strip():
        try:
            if Path(raw_root.strip()).resolve() != bridge.project_root:
                return int(HTTPStatus.CONFLICT), {
                    "ok": False,
                    "message": "対象プロジェクトがブリッジと一致しません",
                }
        except (OSError, ValueError):
            return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "project_root が不正です"}

    is_terminal_fn = getattr(bridge, "is_terminal_apply_in_progress", None)
    if callable(is_terminal_fn) and is_terminal_fn():
        return int(HTTPStatus.OK), {
            "ok": True,
            "skipped_bridge_apply": True,
            "has_queue": True,
            "advanced": False,
            "all_tasks_completed": False,
        }

    raw_reason = str(payload.get("reason") or payload.get("outcome") or "completed").strip().lower()
    if raw_reason == "no_change":
        prev_outcome = "no_change"
        default_msg = "変更なし完了"
    elif raw_reason in ("skip", "skipped"):
        prev_outcome = "skipped"
        default_msg = "タスクをスキップしました"
    else:
        prev_outcome = "completed"
        default_msg = "aiskel a でパッチを適用しました"
    raw_prev_msg = payload.get("prev_apply_message")
    prev_msg = sanitize_console_text(raw_prev_msg, 200) if isinstance(raw_prev_msg, str) and raw_prev_msg.strip() else default_msg
    raw_store = payload.get("store_pending_external")
    store_pending_external = raw_store if isinstance(raw_store, bool) else True

    with bridge._exec_lock:
        binding_info = bridge._binding_manager.get_info()
        if not binding_info.get("bound"):
            queue = bridge._handoff_manager.get_discovery_queue() or load_pending_task_queue(
                bridge.project_root,
                bridge.output_dir,
            )
            return int(HTTPStatus.OK), {
                "ok": True,
                "has_queue": queue is not None,
                "advanced": False,
                "all_tasks_completed": False,
                "no_bound_tab": True,
            }
        if prev_outcome != "skipped":
            raw_handoff_source = payload.get("task_handoff_xml")
            if not isinstance(raw_handoff_source, str) or not raw_handoff_source.strip():
                raw_handoff_source = payload.get("response_text")
            if isinstance(raw_handoff_source, str) and raw_handoff_source.strip():
                capture_task_handoff_xml_from_response(
                    bridge,
                    tab_id=clean_tid,
                    response_text=raw_handoff_source,
                )
        res = advance_next_discovery_task_handoff(
            bridge,
            source_tab_id=clean_tid,
            prev_apply_message=prev_msg,
            prev_test_failed=False,
            store_pending_external=store_pending_external,
            prev_task_outcome=prev_outcome,
        )
        if clean_tid and prev_outcome != "completed" and not res.get("error") and (res.get("advanced") or res.get("all_tasks_completed")):
            if hasattr(bridge._handoff_manager, "set_tab_mode"):
                bridge._handoff_manager.set_tab_mode(clean_tid, None)
            if hasattr(bridge._handoff_manager, "reset_no_patch_retries"):
                bridge._handoff_manager.reset_no_patch_retries(clean_tid)
    if res.get("error"):
        return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
            "ok": False,
            "message": f"次タスク引き継ぎの生成に失敗しました: {res['error']}",
        }
    return int(HTTPStatus.OK), {
        "ok": True,
        "has_queue": bool(res.get("has_queue")),
        "advanced": bool(res.get("advanced")),
        "all_tasks_completed": bool(res.get("all_tasks_completed")),
        "prev_task_outcome": res.get("prev_task_outcome"),
        "handoff_id": res.get("handoff_id"),
        "new_chat_url": res.get("new_chat_url"),
        "task_index": res.get("task_index"),
        "total_tasks": res.get("total_tasks"),
        "target_file": res.get("target_file"),
    }

def handle_tasks_consume_open(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """保留中の外部次タスク起動通知を1度だけ原子的に取得して返す。"""
    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    item: Optional[Dict[str, object]] = bridge._handoff_manager.consume_pending_external_task_handoff()
    if not item:
        return int(HTTPStatus.OK), {"ok": True, "found": False}

    hid = str(item.get("handoff_id") or "")
    has_active_fn = getattr(bridge._handoff_manager, "has_active_handoff", None)
    if callable(has_active_fn) and not has_active_fn(hid):
        # バックグラウンド遅延でTTL失効済みの場合はキューの現在タスクから引き継ぎを再生成する
        with bridge._exec_lock:
            queue = bridge._handoff_manager.get_discovery_queue() or load_pending_task_queue(
                bridge.project_root,
                bridge.output_dir,
            )
            if queue is None or not (0 <= queue.current_index < queue.total_tasks):
                return int(HTTPStatus.OK), {"ok": True, "found": False}
            try:
                handoff, display_target, _ = _create_task_handoff_record(
                    bridge,
                    queue,
                    queue.current_index,
                    source_tab_id="",
                    prev_apply_message="aiskel a でパッチを適用しました",
                    prev_test_failed=False,
                )
            except (OSError, ValueError, RuntimeError):
                return int(HTTPStatus.OK), {"ok": True, "found": False}
            item = {
                "handoff_id": handoff.handoff_id,
                "new_chat_url": handoff.new_chat_url,
                "task_index": queue.current_index + 1,
                "total_tasks": queue.total_tasks,
                "target_file": display_target,
            }

    return int(HTTPStatus.OK), {
        "ok": True,
        "found": True,
        "handoff_id": item.get("handoff_id"),
        "new_chat_url": item.get("new_chat_url"),
        "task_index": item.get("task_index"),
        "total_tasks": item.get("total_tasks"),
        "target_file": item.get("target_file"),
    }

def handle_tasks_clear(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """保持中の分割タスクキューを破棄する。"""
    _, tab_err = bridge._validate_optional_tab_id(payload, required=False)
    if tab_err is not None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err}
    with bridge._exec_lock:
        bridge._handoff_manager.clear_discovery_queue()
        clear_pending_task_queue(bridge.project_root, bridge.output_dir)
    return int(HTTPStatus.OK), {"ok": True, "cleared": True}
