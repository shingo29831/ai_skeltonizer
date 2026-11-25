"""Module: @role: インメモリ仮想バッファによるトランザクション保証付きファイル置換・新規ノード挿入・コードおよびファイル削除を実行する。"""

from pathlib import Path
from typing import Dict, Optional, Sequence, Union

from ...config import (
    WritePathPolicy,
    load_write_path_policy_from_meta,
)
from ..block_patcher import (
    apply_block_replacement,
    replace_blocks_in_lines,
)
from .applier_content import (
    apply_block_deletion_chunk,
    apply_empty_search_chunk,
    apply_new_file_chunk,
    apply_search_replace_chunk,
)
from .applier_file_ops import (
    apply_block_file_delete,
    apply_file_move_chunk,
    apply_standalone_file_delete,
    check_move_destination_policy,
    check_path_with_dynamic_dirs,
    ensure_file_buffer_loaded,
    is_under_extra_allowed_dirs,
    is_within_project,
    safe_read_text,
)
from .applier_types import PatchApplyState
from .committer import commit_patch_transaction
from .matcher import find_and_replace
from .parser import (
    DELIMITER_END,
    DELIMITER_MID,
    DELIMITER_START,
    check_and_warn_delimiter,
    has_patch_delimiters,
    is_file_deletion_patch,
    is_file_move_patch,
    parse_patch_chunks,
)
from .result import PatchResult
from ..syntax_block_editor import (
    clean_excessive_blank_lines,
    delete_block_from_lines,
    extract_blocks,
    extract_import_lines,
    find_assignment_end,
    find_block_range,
    find_new_block_insert_index,
    has_import_statements,
    insert_block_into_lines,
    merge_import_lines,
)

_clean_excessive_blank_lines = clean_excessive_blank_lines
_has_import_statements = has_import_statements
_extract_import_lines = extract_import_lines
_merge_import_lines = merge_import_lines
_find_new_block_insert_index = find_new_block_insert_index
_insert_block_into_lines = insert_block_into_lines
_find_assignment_end = find_assignment_end
_find_block_range = find_block_range
_delete_block_from_lines = delete_block_from_lines
_extract_blocks = extract_blocks
_find_and_replace = find_and_replace
_replace_blocks_in_lines = replace_blocks_in_lines
_apply_block_replacement = apply_block_replacement
_check_and_warn_delimiter = check_and_warn_delimiter
_DELIMITER_START = DELIMITER_START
_DELIMITER_MID = DELIMITER_MID
_DELIMITER_END = DELIMITER_END

_safe_read_text = safe_read_text
_is_within_project = is_within_project
_is_under_extra_allowed_dirs = is_under_extra_allowed_dirs
_check_path_with_dynamic_dirs = check_path_with_dynamic_dirs
_check_move_destination_policy = check_move_destination_policy

def apply_patch(
    patch_text: str,
    project_root: Path,
    target_file: Path | None = None,
    force_replace: bool = False,
    revert: bool = False,
    dry_run: bool = False,
    show_diff: bool = True,
    allowed_write_paths: Optional[Sequence[Union[str, Path]]] = None,
) -> PatchResult:
    """
    パッチテキストを解析し、ファイルを書き換える。
    コード削除およびファイル自体の削除操作にも対応する。
    """
    write_policy = (
        load_write_path_policy_from_meta(project_root)
        if allowed_write_paths is None
        else WritePathPolicy.from_allowed_paths(allowed_write_paths, project_root)
    )

    if not has_patch_delimiters(patch_text):
        if is_file_deletion_patch(patch_text) or is_file_move_patch(patch_text):
            pass
        else:
            if revert:
                print("✖ エラー: リバート処理には置換ブロック(<<<< ==== >>>>)が必要です。")
                return PatchResult(0, 1, 0, set(), ["リバート処理には置換ブロック(<<<< ==== >>>>)が必要です"])
            # Markdown見出し等の====を除外しつつ<<<<や>>>>の片側欠落による誤フォールバックを防ぐ
            if DELIMITER_START.search(patch_text) or DELIMITER_END.search(patch_text):
                err_msg = "置換ブロックの区切り記号(<<<< ==== >>>>)が不完全です"
                print(f"✖ 適用失敗: {err_msg}")
                return PatchResult(0, 1, 0, set(), [err_msg])
            print("ℹ 置換ブロック(<<<<)が見つからないため、関数・クラス単位の自動置換を試みます...")
            block_kwargs: Dict[str, object] = {"dry_run": dry_run}
            if not show_diff:
                block_kwargs["show_diff"] = False
            if allowed_write_paths is not None:
                block_kwargs["allowed_write_paths"] = allowed_write_paths
            return _apply_block_replacement(patch_text, project_root, target_file, **block_kwargs)

    chunks, parse_failed_details, parse_fail_count = parse_patch_chunks(
        patch_text, project_root, default_target_file=target_file, revert=revert
    )
    state = PatchApplyState(
        fail_count=parse_fail_count,
        failed_details=list(parse_failed_details),
    )

    for chunk in chunks:
        current_file = chunk.file_path
        if not current_file:
            continue

        try:
            disp_path = current_file.relative_to(project_root)
        except ValueError:
            disp_path = current_file

        if chunk.is_file_move and chunk.move_target_path is not None:
            apply_file_move_chunk(
                state,
                chunk,
                project_root,
                write_policy,
                disp_path,
                force_replace=force_replace,
                revert=revert,
            )
            continue

        allowed, reject_reason = check_path_with_dynamic_dirs(write_policy, current_file, state.extra_allowed_dirs)
        if not allowed:
            action_label = "リバート" if revert else (
                "ファイル削除" if (chunk.is_standalone_file_delete or chunk.is_file_deletion) else "書き込み"
            )
            print(f"✖ {action_label}拒否 (許可されていないパス): {disp_path} (パッチ行 {chunk.block_line_no})")
            state.failed_details.append(
                f"{disp_path}: {action_label}拒否 (パッチ行 {chunk.block_line_no})\n"
                f"  -> {reject_reason}"
            )
            state.fail_count += 1
            continue

        if chunk.is_standalone_file_delete:
            apply_standalone_file_delete(state, current_file, disp_path)
            continue

        ensure_file_buffer_loaded(state, current_file, is_file_deletion=chunk.is_file_deletion)

        search_lines = chunk.search_lines
        replace_lines = chunk.replace_lines
        is_empty_search = not any(line.strip() for line in search_lines)
        is_empty_replace = not any(line.strip() for line in replace_lines)
        content = state.file_contents[current_file]

        if chunk.is_file_deletion:
            apply_block_file_delete(state, current_file, disp_path, content)
            continue

        if content is None:
            action_label = "リバート" if revert else "適用"
            print(
                f"✖ {action_label}失敗: {disp_path} (パッチ行 {chunk.block_line_no})\n"
                "  -> バイナリファイルまたは読み込み不能なファイルです"
            )
            state.failed_details.append(
                f"{disp_path}: {action_label}失敗 (パッチ行 {chunk.block_line_no})\n"
                "  -> バイナリファイルまたは読み込み不能なファイルです"
            )
            state.fail_count += 1
            continue

        if chunk.del_node_name and is_empty_replace:
            apply_block_deletion_chunk(state, current_file, disp_path, content, chunk.del_node_name)
            continue

        if (current_file.exists() and current_file not in state.files_to_delete) or content:
            if is_empty_search:
                apply_empty_search_chunk(
                    state,
                    current_file,
                    disp_path,
                    project_root,
                    content,
                    replace_lines,
                    force_replace=force_replace,
                    revert=revert,
                    replace_blocks_fn=_replace_blocks_in_lines,
                )
            else:
                apply_search_replace_chunk(
                    state,
                    current_file,
                    disp_path,
                    content,
                    search_lines,
                    replace_lines,
                    chunk.block_line_no,
                    force_replace=force_replace,
                    revert=revert,
                    find_and_replace_fn=find_and_replace,
                )
        else:
            apply_new_file_chunk(state, current_file, disp_path, replace_lines, revert=revert)

    commit_kwargs: Dict[str, object] = {}
    if not show_diff:
        commit_kwargs["show_diff"] = False
    if allowed_write_paths is not None:
        commit_kwargs["allowed_write_paths"] = allowed_write_paths
    if state.binary_contents:
        commit_kwargs["binary_contents"] = state.binary_contents
    if state.move_sources:
        commit_kwargs["move_sources"] = state.move_sources
    if state.extra_allowed_dirs:
        commit_kwargs["extra_allowed_dirs"] = sorted(state.extra_allowed_dirs)
    if state.case_rename_targets:
        commit_kwargs["case_rename_targets"] = sorted(state.case_rename_targets)
    return commit_patch_transaction(
        file_contents=state.file_contents,
        original_contents=state.original_contents,
        modified_files=state.modified_files,
        files_to_delete=state.files_to_delete,
        project_root=project_root,
        success_count=state.success_count,
        fail_count=state.fail_count,
        skipped_count=state.skipped_count,
        failed_details=state.failed_details,
        dry_run=dry_run,
        **commit_kwargs,
    )
