"""Module: @role: 1度目探索応答(<dirs>/<task>)の検証・スコープ限定バンドル生成・2度目タブ引き継ぎ登録を担当する。"""
from datetime import datetime
from http import HTTPStatus
from typing import Any, Dict, Tuple

from ..config import load_two_stage_config_from_meta
from .aistudio_handoff.text import (
    extract_discovery_directives,
    extract_original_user_request,
    format_discovery_handoff_text,
)
from .aistudio_handoff.types import HandoffKind
from .aistudio_prompts import PromptPattern, load_prompt_for_pattern
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
    """1度目応答の<dirs>/<task>を解析し、スコープ限定バンドルと2度目プロンプトで新規タブ引き継ぎを作成する。"""
    parsed = extract_discovery_directives(response_text)
    if parsed is None:
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": "有効な <dirs>...</dirs> ディレクティブが見つかりません",
        }
    organized_task, dir_targets = parsed

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
                "`<task>...</task>` と `<dirs>...</dirs>` を再出力してください。"
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
        combined_text = format_discovery_handoff_text(
            second_stage_prompt,
            scoped_bundle_text,
            original_request=original_request,
            organized_task=organized_task,
            missing_specs=resolution.missing_specs,
        )

        max_tabs = bridge.get_patch_handoff_max_tabs()
        target_summary = ", ".join(resolution.resolved_specs[:4])
        if len(resolution.resolved_specs) > 4:
            target_summary += f" (+{len(resolution.resolved_specs) - 4})"

        handoff = bridge._handoff_manager.create(
            source_tab_id=req_tab_id,
            new_chat_url=new_chat_url,
            response_text="",
            summary_text=scoped_bundle_text,
            remaining_auto_tabs=max_tabs,
            handoff_kind=HandoffKind.DISCOVERY,
            target_file=target_summary,
            raw_combined_text=combined_text,
            source_command_notice=(
                f"スコープ限定バンドル生成完了: 対象 {len(resolution.primary_files)} 件 + 直接依存 {len(resolution.dependency_files)} 件"
            ),
            source_command_is_error=False,
        )
        if dedup_key:
            bridge._recent_keys.append(dedup_key)

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"\n[{timestamp}] 🔎 探索結果(<dirs>)を検知 -> スコープ限定バンドル "
        f"(対象 {len(resolution.primary_files)} + 依存 {len(resolution.dependency_files)} ファイル) で2度目タブへ引き継ぎます",
        flush=True,
    )
    return int(HTTPStatus.OK), {
        "ok": True,
        "skipped": False,
        "discovery_handoff": True,
        "remaining_auto_tabs": max_tabs,
        "handoff_id": handoff.handoff_id,
        "new_chat_url": handoff.new_chat_url,
        "target_file": target_summary,
        "scoped_primary_count": len(resolution.primary_files),
        "scoped_dependency_count": len(resolution.dependency_files),
        "message": "スコープ限定バンドルと2度目プロンプトを新規タブへ引き継ぎます",
    }
