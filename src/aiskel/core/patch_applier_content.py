"""Module: @role: 仮想バッファ上のテキスト置換・構文ブロック置換/削除・新規ファイル作成を担当する。"""

from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .block_patcher import replace_blocks_in_lines
from .patch_applier_types import PatchApplyState
from .patch_matcher import find_and_replace
from .syntax_block_editor import (
    BLOCK_PATTERN,
    delete_block_from_lines,
    extract_blocks,
    extract_import_lines,
    find_block_range,
    find_new_block_insert_index,
    has_import_statements,
    insert_block_into_lines,
    merge_import_lines,
)

def apply_block_deletion_chunk(
    state: PatchApplyState,
    current_file: Path,
    disp_path: Path,
    content: str,
    del_node_name: str,
) -> None:
    """削除ディレクティブ指定の関数・クラス構文ブロックを仮想バッファから削除する。"""
    content_lines = content.splitlines()
    ok, new_lines = delete_block_from_lines(content_lines, del_node_name)
    if ok:
        new_content = "\n".join(new_lines)
        if new_content and not new_content.endswith("\n"):
            new_content += "\n"
        state.file_contents[current_file] = new_content
        print(f"🗑 関数/クラス削除成功: {del_node_name} ({disp_path})")
        state.success_count += 1
        state.modified_files.add(current_file)
        return

    target_b_start, _ = find_block_range(content_lines, del_node_name, "function")
    if target_b_start == -1:
        target_b_start, _ = find_block_range(content_lines, del_node_name, "class")
    if target_b_start == -1:
        print(f"⏭ 関数/クラス削除スキップ (既に削除済み): {del_node_name} ({disp_path})")
        state.skipped_count += 1
    else:
        print(f"✖ 関数/クラス削除失敗: {del_node_name} ({disp_path})")
        state.failed_details.append(f"{disp_path}: 削除対象の関数/クラス '{del_node_name}' が見つかりません")
        state.fail_count += 1

def apply_empty_search_chunk(
    state: PatchApplyState,
    current_file: Path,
    disp_path: Path,
    project_root: Path,
    content: str,
    replace_lines: List[str],
    *,
    force_replace: bool = False,
    revert: bool = False,
    replace_blocks_fn: Callable[..., Tuple[int, int, int, List[str], List[str]]] = replace_blocks_in_lines,
) -> None:
    """検索ブロックが空のモードBチャンクを部分置換・新規挿入または全体置換として適用する。"""
    if revert:
        print(f"✖ リバート失敗: 空の検索ブロックに対するリバートは行えません: {disp_path}")
        state.failed_details.append(f"{disp_path}: 空の検索ブロックに対するリバートは行えません")
        state.fail_count += 1
        return

    replace_text = "\n".join(replace_lines).strip()
    if not replace_text:
        print(f"✖ 置換失敗: 置換後コードが空です: {disp_path}")
        state.failed_details.append(f"{disp_path}: 置換後コードが空です")
        state.fail_count += 1
        return

    content_lines = content.splitlines()
    existing_has_imports = has_import_statements(content_lines)
    replace_has_imports = has_import_statements(replace_lines)
    blocks = extract_blocks(replace_lines)
    existing_blocks = extract_blocks(content_lines)

    # なぜ必要か: import付きブロック追加時に既存ファイルの他関数群を上書き消去しないため
    is_partial = False
    if blocks:
        if not replace_has_imports:
            if existing_has_imports or existing_blocks or len(content_lines) > len(replace_lines):
                is_partial = True
        else:
            existing_names = {b[1] for b in existing_blocks}
            replace_names = {b[1] for b in blocks}
            if existing_names and not existing_names.issubset(replace_names):
                is_partial = True

    if not is_partial:
        content_normalized = content.replace("\r\n", "\n").strip()
        if not force_replace and content_normalized == replace_text:
            print(f"⏭ 全体置換スキップ (適用済み): {disp_path}")
            state.skipped_count += 1
            return
        new_content = "\n".join(replace_lines)
        if not new_content.endswith("\n"):
            new_content += "\n"
        state.file_contents[current_file] = new_content
        print(f"✔ ファイル全体置換成功: {disp_path}")
        state.success_count += 1
        state.modified_files.add(current_file)
        return

    import_lines = extract_import_lines(replace_lines)
    if import_lines:
        content_lines = merge_import_lines(content_lines, import_lines)

    if len(blocks) == 1 and len(replace_lines) <= (blocks[0][3] - blocks[0][2] + 10 + len(import_lines)):
        b_type, b_name, _, _ = blocks[0]
        import_set = {line.strip() for line in import_lines}
        single_block_lines = [line for line in replace_lines if line.strip() not in import_set]
        while single_block_lines and not single_block_lines[0].strip():
            single_block_lines.pop(0)
        while single_block_lines and not single_block_lines[-1].strip():
            single_block_lines.pop()

        t_start, t_end = find_block_range(content_lines, b_name, b_type)
        type_label = "クラス" if b_type == "class" else "関数"
        block_replace_text = "\n".join(single_block_lines).strip()

        if t_start != -1:
            target_block_text = "\n".join(content_lines[t_start:t_end]).strip()
            if not force_replace and target_block_text == block_replace_text:
                print(f"⏭ 置換スキップ (適用済み): {disp_path} ({b_name})")
                state.skipped_count += 1
            else:
                new_lines = content_lines[:t_start] + single_block_lines + content_lines[t_end:]
                new_content = "\n".join(new_lines)
                if not new_content.endswith("\n"):
                    new_content += "\n"
                state.file_contents[current_file] = new_content
                print(f"✔ {type_label}部分置換成功: {b_name} ({disp_path})")
                state.success_count += 1
                state.modified_files.add(current_file)
            return

        # なぜ必要か: モードBで未存在の関数・クラスを新規作成として適切な位置へ追加するため
        content_normalized = "\n".join(content_lines)
        if not force_replace and block_replace_text in content_normalized:
            print(f"⏭ 追加スキップ (適用済み): {disp_path} ({b_name})")
            state.skipped_count += 1
        else:
            insert_idx = find_new_block_insert_index(content_lines, current_file)
            new_lines = insert_block_into_lines(content_lines, single_block_lines, insert_idx)
            new_content = "\n".join(new_lines)
            if not new_content.endswith("\n"):
                new_content += "\n"
            state.file_contents[current_file] = new_content
            print(f"✨ {type_label}新規作成成功: {b_name} ({disp_path})")
            state.success_count += 1
            state.modified_files.add(current_file)
        return

    s, f, sk, new_lines, bf = replace_blocks_fn(
        content_lines, replace_lines, current_file, project_root, allow_create=True, force_replace=force_replace
    )
    state.failed_details.extend(bf)
    if s > 0:
        new_content = "\n".join(new_lines)
        if not new_content.endswith("\n"):
            new_content += "\n"
        state.file_contents[current_file] = new_content
        state.success_count += s
        state.fail_count += f
        state.skipped_count += sk
        state.modified_files.add(current_file)
    else:
        state.fail_count += f
        state.skipped_count += sk

def apply_search_replace_chunk(
    state: PatchApplyState,
    current_file: Path,
    disp_path: Path,
    content: str,
    search_lines: List[str],
    replace_lines: List[str],
    block_line_no: int,
    *,
    force_replace: bool = False,
    revert: bool = False,
    find_and_replace_fn: Callable[..., Tuple[Optional[str], str, bool]] = find_and_replace,
) -> None:
    """検索・置換ブロックを持つモードA/Cチャンクを仮想バッファへ適用する。"""
    is_empty_replace = not any(line.strip() for line in replace_lines)
    if is_empty_replace:
        first_non_empty = next((line for line in search_lines if line.strip()), "")
        b_match = BLOCK_PATTERN.match(first_non_empty)
        if b_match:
            b_type = b_match.group(1) or "function"
            # なぜ必要か: C#等の型修飾子付きメソッド定義(グループ4)の削除失敗・例外を防止
            b_name = b_match.group(2) or b_match.group(3) or b_match.group(4)
            if b_name:
                content_lines = content.splitlines()
                ok, new_lines = delete_block_from_lines(content_lines, b_name, b_type)
                if ok:
                    new_content = "\n".join(new_lines)
                    if new_content and not new_content.endswith("\n"):
                        new_content += "\n"
                    state.file_contents[current_file] = new_content
                    print(f"🗑 関数/クラス削除成功: {b_name} ({disp_path})")
                    state.success_count += 1
                    state.modified_files.add(current_file)
                    return

    new_content, error_msg, is_skipped = find_and_replace_fn(content, search_lines, replace_lines, force_replace)
    action_label = "リバート" if revert else ("削除" if is_empty_replace else "適用")
    if is_skipped:
        print(f"⏭ {action_label}スキップ (適用済み): {disp_path}")
        state.skipped_count += 1
    elif new_content is not None:
        state.file_contents[current_file] = new_content
        print(f"✔ {action_label}成功: {disp_path}")
        state.success_count += 1
        state.modified_files.add(current_file)
    else:
        print(f"✖ {action_label}失敗: {disp_path} (パッチ行 {block_line_no})\n  -> {error_msg}")
        state.failed_details.append(f"{disp_path}: {action_label}失敗 (パッチ行 {block_line_no})\n  -> {error_msg}")
        state.fail_count += 1

def apply_new_file_chunk(
    state: PatchApplyState,
    current_file: Path,
    disp_path: Path,
    replace_lines: List[str],
    *,
    revert: bool = False,
) -> None:
    """対象ファイルが未存在時の新規作成またはリバート失敗を記録する。"""
    if revert:
        print(f"✖ リバート失敗: 対象ファイルが存在しません: {disp_path}")
        state.failed_details.append(f"{disp_path}: 対象ファイルが存在しません")
        state.fail_count += 1
        return

    new_content = "\n".join(replace_lines)
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    state.file_contents[current_file] = new_content
    print(f"✨ 新規作成成功: {disp_path}")
    state.success_count += 1
    state.modified_files.add(current_file)
