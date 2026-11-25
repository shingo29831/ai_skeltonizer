"""Module: @role: ブリッジ経由のプロンプト取得・要約統合・肥大化ファイル抽出および自動分割リクエスト処理を担当する。"""
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

from .aistudio_auto_organize import (
    build_organize_combined_text,
    collect_organize_targets_sorted_by_count,
    format_organize_targets_payload,
    validate_organize_target_request,
)
from .aistudio_auto_split import (
    build_split_combined_text,
    collect_bloat_targets_sorted_by_size,
    format_bloat_targets_payload,
    validate_split_target_request,
)
from .aistudio_config_infer import (
    build_config_infer_combined_text,
    load_project_summary_for_config_infer,
)
from .aistudio_handoff import (
    HandoffKind,
    format_summary_prompt_text,
    load_project_summary_text,
)
from .aistudio_prompts import (
    PromptPattern,
    list_prompt_patterns,
    load_prompt_for_pattern,
)

DEFAULT_NEW_CHAT_URL = "https://aistudio.google.com/prompts/new_chat"

def get_prompt_response(
    project_root: Path,
    pattern: str = PromptPattern.DEFAULT.value,
) -> Tuple[int, Dict[str, object]]:
    """指定パターンに対応する自動貼り付け用プロンプト文字列を返す。"""
    try:
        prompt_text = load_prompt_for_pattern(project_root, pattern)
    except ValueError as e:
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": str(e),
            "available_patterns": list(list_prompt_patterns()),
        }
    normalized = (pattern or "").strip().lower() or PromptPattern.DEFAULT.value
    return int(HTTPStatus.OK), {
        "ok": True,
        "pattern": normalized,
        "prompt": prompt_text,
        "available_patterns": list(list_prompt_patterns()),
    }

def handle_summary_prompt(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """最新のプロジェクト要約バンドルと指定パターンのプロンプトを取得し統合テキストを返す。"""
    raw_pattern = payload.get("pattern")
    pattern = (
        str(raw_pattern).strip()
        if isinstance(raw_pattern, str) and raw_pattern.strip()
        else PromptPattern.DEFAULT.value
    )
    try:
        prompt_text = load_prompt_for_pattern(bridge.project_root, pattern)
    except ValueError as e:
        return int(HTTPStatus.BAD_REQUEST), {
            "ok": False,
            "message": str(e),
            "available_patterns": list(list_prompt_patterns()),
        }

    with bridge._exec_lock:
        try:
            summary_text = load_project_summary_text(bridge.project_root)
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"要約バンドルの取得に失敗しました: {e}",
            }

    combined_text = format_summary_prompt_text(prompt_text, summary_text)
    return int(HTTPStatus.OK), {
        "ok": True,
        "pattern": pattern.lower(),
        "prompt": prompt_text,
        "summary": summary_text,
        "combined_text": combined_text,
        "available_patterns": list(list_prompt_patterns()),
    }

def handle_bloat_targets(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """aiskel b 基準でプロジェクト内の肥大化ファイルを検出し、ファイルサイズ降順の一覧を返す。"""
    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    with bridge._exec_lock:
        try:
            bloated, max_lines, max_chars = collect_bloat_targets_sorted_by_size(
                bridge.project_root,
                bridge.output_dir,
            )
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"肥大化ファイルの検出に失敗しました: {e}",
            }

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"\n[{timestamp}] 📊 自動分割対象抽出 (aiskel b): {len(bloated)} 件 "
        f"(基準: 行数 >= {max_lines}, 文字数 >= {max_chars:,})",
        flush=True,
    )
    return int(HTTPStatus.OK), format_bloat_targets_payload(
        bridge.project_root,
        bloated,
        max_lines=max_lines,
        max_chars=max_chars,
    )

def handle_split_prompt(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """現在の空タブで自動分割を開始するための対象1ファイル分割指示+最新要約テキストを返す。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=bridge._binding_manager.require_binding)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    target_info, target_err = validate_split_target_request(
        bridge.project_root,
        payload,
        output_dir=bridge.output_dir,
    )
    if target_err is not None or target_info is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": target_err or "対象ファイル指定が不正です"}

    with bridge._exec_lock:
        try:
            summary_text = load_project_summary_text(bridge.project_root)
            combined_text = build_split_combined_text(bridge.project_root, target_info, summary_text)
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"分割プロンプトの生成に失敗しました: {e}",
            }
        if req_tab_id:
            bridge._handoff_manager.clear_tab_patch(req_tab_id)
            bridge._handoff_manager.set_tab_organize_policy(req_tab_id, None)
            bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
            bridge._handoff_manager.consume_auto_tab_if_tracked(
                req_tab_id,
                bridge.get_patch_handoff_max_tabs(),
            )
        else:
            bridge._handoff_manager.clear_tab_patch("")

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"\n[{timestamp}] ✂ 自動分割開始 ({target_info['file_index']}/{target_info['total_files']}): "
        f"{target_info['rel_path']}",
        flush=True,
    )
    return int(HTTPStatus.OK), {
        "ok": True,
        "target_file": target_info["rel_path"],
        "combined_text": combined_text,
    }

def handle_split_handoff(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """次の分割対象ファイルを新しいチャットタブへ最新要約とともに引き継ぐレコードを作成する。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=bridge._binding_manager.require_binding)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    target_info, target_err = validate_split_target_request(
        bridge.project_root,
        payload,
        output_dir=bridge.output_dir,
    )
    if target_err is not None or target_info is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": target_err or "対象ファイル指定が不正です"}

    new_chat_url = str(payload.get("new_chat_url") or DEFAULT_NEW_CHAT_URL).strip()
    parsed_url = urlsplit(new_chat_url)
    if parsed_url.scheme.lower() != "https" or (parsed_url.hostname or "").lower() != "aistudio.google.com":
        new_chat_url = DEFAULT_NEW_CHAT_URL

    with bridge._exec_lock:
        try:
            summary_text = load_project_summary_text(bridge.project_root)
            combined_text = build_split_combined_text(bridge.project_root, target_info, summary_text)
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"分割プロンプトの生成に失敗しました: {e}",
            }
        bridge._handoff_manager.clear_tab_patch(req_tab_id)
        bridge._handoff_manager.set_tab_organize_policy(req_tab_id, None)
        bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
        max_tabs = bridge.get_patch_handoff_max_tabs()
        raw_notice = payload.get("source_command_notice")
        cleaned_notice = raw_notice.strip() if isinstance(raw_notice, str) else ""
        source_notice = (cleaned_notice[:357] + "...") if len(cleaned_notice) > 360 else cleaned_notice
        source_is_error = bool(payload.get("source_command_is_error"))
        handoff = bridge._handoff_manager.create(
            source_tab_id="",
            new_chat_url=new_chat_url,
            response_text="",
            summary_text=summary_text,
            remaining_auto_tabs=max_tabs,
            handoff_kind=HandoffKind.FILE_SPLIT,
            target_file=str(target_info["rel_path"]),
            raw_combined_text=combined_text,
            source_command_notice=source_notice,
            source_command_is_error=source_is_error,
        )

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"\n[{timestamp}] ✂ 自動分割引き継ぎ ({target_info['file_index']}/{target_info['total_files']}): "
        f"{target_info['rel_path']} -> 新規タブへ移行します (自動新タブ上限: {max_tabs})",
        flush=True,
    )
    return int(HTTPStatus.OK), {
        "ok": True,
        "skipped": False,
        "remaining_auto_tabs": max_tabs,
        "handoff_id": handoff.handoff_id,
        "new_chat_url": handoff.new_chat_url,
        "target_file": target_info["rel_path"],
        "message": "次ファイルの自動分割を新規タブへ引き継ぎます",
    }

def handle_organize_targets(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """同一階層のファイル数が多いディレクトリを検出し、ファイル数降順の一覧を返す。"""
    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    with bridge._exec_lock:
        try:
            dirs, max_files = collect_organize_targets_sorted_by_count(
                bridge.project_root,
                bridge.output_dir,
            )
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"階層整理対象ディレクトリの検出に失敗しました: {e}",
            }

    total_target_files = sum(item.file_count for item in dirs)
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"\n[{timestamp}] 📁 階層整理対象抽出: 対象ファイル {total_target_files} 件 "
        f"({len(dirs)} 階層, 基準: 同一階層ファイル数 >= {max_files})",
        flush=True,
    )
    return int(HTTPStatus.OK), format_organize_targets_payload(
        bridge.project_root,
        dirs,
        max_files=max_files,
    )

def handle_organize_prompt(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """現在の空タブで自動階層整理を開始するための対象ディレクトリ整理指示+最新要約テキストを返す。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=bridge._binding_manager.require_binding)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    target_info, target_err = validate_organize_target_request(
        bridge.project_root,
        payload,
        output_dir=bridge.output_dir,
    )
    if target_err is not None or target_info is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": target_err or "対象ディレクトリ指定が不正です"}

    if int(target_info["file_count"]) == 0:
        return int(HTTPStatus.OK), {
            "ok": True,
            "skipped_empty": True,
            "target_dir": target_info["rel_dir"],
            "file_count": 0,
            "message": f"対象ディレクトリ直下に整理対象ファイルが残っていません: {target_info['rel_dir']}",
        }

    allow_rename = bool(target_info["allow_rename"])
    with bridge._exec_lock:
        try:
            summary_text = load_project_summary_text(bridge.project_root)
            combined_text = build_organize_combined_text(bridge.project_root, target_info, summary_text)
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"階層整理プロンプトの生成に失敗しました: {e}",
            }
        if req_tab_id:
            bridge._handoff_manager.clear_tab_patch(req_tab_id)
            bridge._handoff_manager.set_tab_organize_policy(req_tab_id, allow_rename)
            bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
            bridge._handoff_manager.consume_auto_tab_if_tracked(
                req_tab_id,
                bridge.get_patch_handoff_max_tabs(),
            )
        else:
            bridge._handoff_manager.clear_tab_patch("")

    timestamp = datetime.now().strftime("%H:%M:%S")
    rename_label = "リネーム許可" if allow_rename else "リネーム禁止"
    print(
        f"\n[{timestamp}] 📁 階層整理開始 ({target_info['dir_index']}/{target_info['total_dirs']}): "
        f"{target_info['rel_dir']} ({target_info['file_count']} ファイル / {rename_label})",
        flush=True,
    )
    return int(HTTPStatus.OK), {
        "ok": True,
        "target_dir": target_info["rel_dir"],
        "file_count": target_info["file_count"],
        "allow_rename": allow_rename,
        "combined_text": combined_text,
    }

def handle_organize_handoff(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """次の階層整理対象ディレクトリを新しいチャットタブへ最新要約とともに引き継ぐレコードを作成する。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=bridge._binding_manager.require_binding)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    target_info, target_err = validate_organize_target_request(
        bridge.project_root,
        payload,
        output_dir=bridge.output_dir,
    )
    if target_err is not None or target_info is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": target_err or "対象ディレクトリ指定が不正です"}

    if int(target_info["file_count"]) == 0:
        return int(HTTPStatus.OK), {
            "ok": True,
            "skipped_empty": True,
            "target_dir": target_info["rel_dir"],
            "file_count": 0,
            "message": f"対象ディレクトリ直下に整理対象ファイルが残っていません: {target_info['rel_dir']}",
        }

    new_chat_url = str(payload.get("new_chat_url") or DEFAULT_NEW_CHAT_URL).strip()
    parsed_url = urlsplit(new_chat_url)
    if parsed_url.scheme.lower() != "https" or (parsed_url.hostname or "").lower() != "aistudio.google.com":
        new_chat_url = DEFAULT_NEW_CHAT_URL

    allow_rename = bool(target_info["allow_rename"])
    with bridge._exec_lock:
        try:
            summary_text = load_project_summary_text(bridge.project_root)
            combined_text = build_organize_combined_text(bridge.project_root, target_info, summary_text)
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"階層整理プロンプトの生成に失敗しました: {e}",
            }
        bridge._handoff_manager.clear_tab_patch(req_tab_id)
        bridge._handoff_manager.set_tab_organize_policy(req_tab_id, None)
        bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
        max_tabs = bridge.get_patch_handoff_max_tabs()
        raw_notice = payload.get("source_command_notice")
        cleaned_notice = raw_notice.strip() if isinstance(raw_notice, str) else ""
        source_notice = (cleaned_notice[:357] + "...") if len(cleaned_notice) > 360 else cleaned_notice
        source_is_error = bool(payload.get("source_command_is_error"))
        handoff = bridge._handoff_manager.create(
            source_tab_id="",
            new_chat_url=new_chat_url,
            response_text="",
            summary_text=summary_text,
            remaining_auto_tabs=max_tabs,
            handoff_kind=HandoffKind.DIR_ORGANIZE,
            target_file=str(target_info["rel_dir"]),
            raw_combined_text=combined_text,
            source_command_notice=source_notice,
            source_command_is_error=source_is_error,
            allow_rename=allow_rename,
        )

    timestamp = datetime.now().strftime("%H:%M:%S")
    rename_label = "リネーム許可" if allow_rename else "リネーム禁止"
    print(
        f"\n[{timestamp}] 📁 階層整理引き継ぎ ({target_info['dir_index']}/{target_info['total_dirs']}): "
        f"{target_info['rel_dir']} ({target_info['file_count']} ファイル / {rename_label}) -> 新規タブへ移行します (自動新タブ上限: {max_tabs})",
        flush=True,
    )
    return int(HTTPStatus.OK), {
        "ok": True,
        "skipped": False,
        "remaining_auto_tabs": max_tabs,
        "handoff_id": handoff.handoff_id,
        "new_chat_url": handoff.new_chat_url,
        "target_dir": target_info["rel_dir"],
        "file_count": target_info["file_count"],
        "allow_rename": allow_rename,
        "message": "次ディレクトリの階層整理を新規タブへ引き継ぎます",
    }

def handle_config_prompt(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """aiskel同期を実行し、現在の空タブへ設定ファイル推論プロンプト・要約・config.jsonを統合して返す。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=bridge._binding_manager.require_binding)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    with bridge._exec_lock:
        try:
            summary_text = load_project_summary_for_config_infer(bridge.project_root, bridge.output_dir)
            combined_text, target_rel_path, config_exists = build_config_infer_combined_text(
                bridge.project_root,
                summary_text,
                output_dir=bridge.output_dir,
            )
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"設定推論プロンプトの生成に失敗しました: {e}",
            }
        if req_tab_id:
            bridge._handoff_manager.clear_tab_patch(req_tab_id)
            bridge._handoff_manager.set_tab_organize_policy(req_tab_id, None)
            bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, target_rel_path)
            bridge._handoff_manager.consume_auto_tab_if_tracked(
                req_tab_id,
                bridge.get_patch_handoff_max_tabs(),
            )
        else:
            bridge._handoff_manager.clear_tab_patch("")

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"\n[{timestamp}] ⚙ 設定推論開始: aiskel 同期完了 -> {target_rel_path} を推論します",
        flush=True,
    )
    return int(HTTPStatus.OK), {
        "ok": True,
        "target_file": target_rel_path,
        "config_exists": config_exists,
        "combined_text": combined_text,
    }

def handle_config_handoff(bridge: Any, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
    """aiskel同期を実行し、設定ファイル推論タスクを新しいチャットタブへ引き継ぐレコードを作成する。"""
    req_tab_id, tab_err = bridge._validate_optional_tab_id(payload, required=bridge._binding_manager.require_binding)
    if tab_err is not None or req_tab_id is None:
        return int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": tab_err or "tab_id が不正です"}

    tab_error = bridge._binding_manager.validate_execute_payload(payload)
    if tab_error is not None:
        return int(HTTPStatus.CONFLICT), {"ok": False, "skipped": True, "message": tab_error}

    new_chat_url = str(payload.get("new_chat_url") or DEFAULT_NEW_CHAT_URL).strip()
    parsed_url = urlsplit(new_chat_url)
    if parsed_url.scheme.lower() != "https" or (parsed_url.hostname or "").lower() != "aistudio.google.com":
        new_chat_url = DEFAULT_NEW_CHAT_URL

    with bridge._exec_lock:
        try:
            summary_text = load_project_summary_for_config_infer(bridge.project_root, bridge.output_dir)
            combined_text, target_rel_path, config_exists = build_config_infer_combined_text(
                bridge.project_root,
                summary_text,
                output_dir=bridge.output_dir,
            )
        except (OSError, ValueError, RuntimeError) as e:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {
                "ok": False,
                "message": f"設定推論プロンプトの生成に失敗しました: {e}",
            }
        bridge._handoff_manager.clear_tab_patch(req_tab_id)
        bridge._handoff_manager.set_tab_organize_policy(req_tab_id, None)
        bridge._handoff_manager.set_tab_config_infer_target(req_tab_id, None)
        max_tabs = bridge.get_patch_handoff_max_tabs()
        handoff = bridge._handoff_manager.create(
            source_tab_id="",
            new_chat_url=new_chat_url,
            response_text="",
            summary_text=summary_text,
            remaining_auto_tabs=max_tabs,
            handoff_kind=HandoffKind.CONFIG_INFER,
            target_file=target_rel_path,
            raw_combined_text=combined_text,
            config_infer_target=target_rel_path,
        )

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"\n[{timestamp}] ⚙ 設定推論引き継ぎ: aiskel 同期完了 -> {target_rel_path} を新規タブで推論します (自動新タブ上限: {max_tabs})",
        flush=True,
    )
    return int(HTTPStatus.OK), {
        "ok": True,
        "skipped": False,
        "remaining_auto_tabs": max_tabs,
        "handoff_id": handoff.handoff_id,
        "new_chat_url": handoff.new_chat_url,
        "target_file": target_rel_path,
        "config_exists": config_exists,
        "message": "設定ファイル推論を新規タブへ引き継ぎます",
    }
