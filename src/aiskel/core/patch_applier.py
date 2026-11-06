"""Module: @role: インメモリ仮想バッファによるトランザクション保証付きファイル置換・新規ノード挿入・コードおよびファイル削除を実行する。"""

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .block_patcher import (
    FILE_HEADER_PATTERN,
    apply_block_replacement,
    replace_blocks_in_lines,
)
from .patch_committer import commit_patch_transaction
from .patch_matcher import find_and_replace, generate_diff_display
from .patch_parser import (
    DELIMITER_END,
    DELIMITER_MID,
    DELIMITER_START,
    PatchChunk,
    check_and_warn_delimiter,
    has_patch_delimiters,
    is_file_deletion_patch,
    parse_patch_chunks,
)
from .patch_result import PatchResult
from .syntax_block_editor import (
    BLOCK_PATTERN,
    DELETE_DIRECTIVE_PATTERN,
    DELETE_FILE_DIRECTIVE_PATTERN,
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


def _safe_read_text(file_path: Path) -> Tuple[Optional[str], Optional[str]]:
    # なぜ必要か: バイナリや非UTF-8ファイルを安全に判別しクラッシュを防ぐため
    try:
        raw_bytes = file_path.read_bytes()
    except OSError as e:
        return None, f"ファイル読み込みエラー: {e}"
    if b"\x00" in raw_bytes[:8192]:
        return None, "バイナリファイルのためテキストパッチを適用できません"
    for enc in ("utf-8", "utf-8-sig", "cp932", "latin-1"):
        try:
            return raw_bytes.decode(enc), None
        except UnicodeDecodeError:
            continue
    return None, "テキストとしてデコードできません"


def apply_patch(
    patch_text: str,
    project_root: Path,
    target_file: Path | None = None,
    force_replace: bool = False,
    revert: bool = False,
    dry_run: bool = False,
) -> PatchResult:
    """
    パッチテキストを解析し、ファイルを書き換える。
    コード削除およびファイル自体の削除操作にも対応する。
    """
    if not has_patch_delimiters(patch_text):
        if is_file_deletion_patch(patch_text):
            pass
        else:
            if revert:
                print("✖ エラー: リバート処理には置換ブロック(<<<< ==== >>>>)が必要です。")
                return PatchResult(0, 1, 0, set(), ["リバート処理には置換ブロック(<<<< ==== >>>>)が必要です"])
            print("ℹ 置換ブロック(<<<<)が見つからないため、関数・クラス単位の自動置換を試みます...")
            return _apply_block_replacement(patch_text, project_root, target_file, dry_run=dry_run)

    chunks, parse_failed_details, parse_fail_count = parse_patch_chunks(
        patch_text, project_root, default_target_file=target_file, revert=revert
    )

    success_count = 0
    fail_count = parse_fail_count
    skipped_count = 0
    failed_details: List[str] = list(parse_failed_details)
    modified_files: Set[Path] = set()
    files_to_delete: Set[Path] = set()

    file_contents: Dict[Path, str] = {}
    original_contents: Dict[Path, str] = {}

    for chunk in chunks:
        current_file = chunk.file_path
        if not current_file:
            continue

        try:
            disp_path = current_file.relative_to(project_root)
        except ValueError:
            disp_path = current_file

        # ブロックを伴わない単体ファイル削除ディレクティブの処理
        if chunk.is_standalone_file_delete:
            if not current_file.exists():
                print(f"⏭ ファイル削除スキップ (既に存在しません): {disp_path}")
                skipped_count += 1
            else:
                files_to_delete.add(current_file)
                modified_files.add(current_file)
                file_contents[current_file] = ""
                original_contents[current_file] = ""
                print(f"🗑 ファイル削除成功: {disp_path}")
                success_count += 1
            continue

        if current_file not in file_contents:
            if chunk.is_file_deletion:
                file_contents[current_file] = ""
                original_contents[current_file] = ""
            elif current_file.exists():
                orig_text, _ = _safe_read_text(current_file)
                file_contents[current_file] = orig_text
                original_contents[current_file] = orig_text
            else:
                file_contents[current_file] = ""
                original_contents[current_file] = ""

        search_lines = chunk.search_lines
        replace_lines = chunk.replace_lines
        block_line_no = chunk.block_line_no
        is_empty_search = not any(line.strip() for line in search_lines)
        is_empty_replace = not any(line.strip() for line in replace_lines)
        content = file_contents[current_file]

        # 1. ファイル丸ごと削除判定
        if chunk.is_file_deletion:
            if not current_file.exists() and not content:
                print(f"⏭ ファイル削除スキップ (既に存在しません): {disp_path}")
                skipped_count += 1
            else:
                files_to_delete.add(current_file)
                modified_files.add(current_file)
                file_contents[current_file] = ""
                print(f"🗑 ファイル削除成功: {disp_path}")
                success_count += 1
            continue

        if content is None:
            action_label = "リバート" if revert else "適用"
            print(f"✖ {action_label}失敗: {disp_path} (パッチ行 {block_line_no})\n  -> バイナリファイルまたは読み込み不能なファイルです")
            failed_details.append(f"{disp_path}: {action_label}失敗 (パッチ行 {block_line_no})\n  -> バイナリファイルまたは読み込み不能なファイルです")
            fail_count += 1
            continue

        # 2. 関数・クラス等の構文ブロック削除ディレクティブ判定
        del_node_name = chunk.del_node_name
        if del_node_name and is_empty_replace:
            content_lines = content.splitlines()
            ok, new_lines = delete_block_from_lines(content_lines, del_node_name)
            if ok:
                new_content = "\n".join(new_lines)
                if new_content and not new_content.endswith("\n"):
                    new_content += "\n"
                file_contents[current_file] = new_content
                print(f"🗑 関数/クラス削除成功: {del_node_name} ({disp_path})")
                success_count += 1
                modified_files.add(current_file)
            else:
                target_b_start, _ = find_block_range(content_lines, del_node_name, "function")
                if target_b_start == -1:
                    target_b_start, _ = find_block_range(content_lines, del_node_name, "class")
                if target_b_start == -1:
                    print(f"⏭ 関数/クラス削除スキップ (既に削除済み): {del_node_name} ({disp_path})")
                    skipped_count += 1
                else:
                    print(f"✖ 関数/クラス削除失敗: {del_node_name} ({disp_path})")
                    failed_details.append(f"{disp_path}: 削除対象の関数/クラス '{del_node_name}' が見つかりません")
                    fail_count += 1
            continue

        # 3. 既存ファイルが存在する場合の処理
        if current_file.exists() or content:
            if is_empty_search:
                if revert:
                    print(f"✖ リバート失敗: 空の検索ブロックに対するリバートは行えません: {disp_path}")
                    failed_details.append(f"{disp_path}: 空の検索ブロックに対するリバートは行えません")
                    fail_count += 1
                    continue

                replace_text = "\n".join(replace_lines).strip()
                if not replace_text:
                    print(f"✖ 置換失敗: 置換後コードが空です: {disp_path}")
                    failed_details.append(f"{disp_path}: 置換後コードが空です")
                    fail_count += 1
                    continue

                content_lines = content.splitlines()
                existing_has_imports = has_import_statements(content_lines)
                replace_has_imports = has_import_statements(replace_lines)
                blocks = extract_blocks(replace_lines)
                existing_blocks = extract_blocks(content_lines)

                # なぜ必要か: 新規関数の追加ブロックにimport文が含まれる場合でも、既存ファイル内の他の関数群を誤って全体置換で上書き消去しないよう判定
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

                if is_partial:
                    import_lines = extract_import_lines(replace_lines)
                    if import_lines:
                        content_lines = merge_import_lines(content_lines, import_lines)

                    if len(blocks) == 1 and len(replace_lines) <= (blocks[0][3] - blocks[0][2] + 10 + len(import_lines)):
                        b_type, b_name, _, _ = blocks[0]
                        import_set = {l.strip() for l in import_lines}
                        single_block_lines = [l for l in replace_lines if l.strip() not in import_set]
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
                                skipped_count += 1
                            else:
                                new_lines = content_lines[:t_start] + single_block_lines + content_lines[t_end:]
                                new_content = "\n".join(new_lines)
                                if not new_content.endswith("\n"):
                                    new_content += "\n"
                                file_contents[current_file] = new_content
                                print(f"✔ {type_label}部分置換成功: {b_name} ({disp_path})")
                                success_count += 1
                                modified_files.add(current_file)
                        else:
                            # なぜ必要か: モードBで既存ファイルに存在しない関数・クラスが指定された場合に新規作成として適切な位置に追加する
                            content_normalized = "\n".join(content_lines)
                            if not force_replace and block_replace_text in content_normalized:
                                print(f"⏭ 追加スキップ (適用済み): {disp_path} ({b_name})")
                                skipped_count += 1
                            else:
                                insert_idx = find_new_block_insert_index(content_lines, current_file)
                                new_lines = insert_block_into_lines(content_lines, single_block_lines, insert_idx)
                                new_content = "\n".join(new_lines)
                                if not new_content.endswith("\n"):
                                    new_content += "\n"
                                file_contents[current_file] = new_content
                                print(f"✨ {type_label}新規作成成功: {b_name} ({disp_path})")
                                success_count += 1
                                modified_files.add(current_file)
                    else:
                        s, f, sk, new_lines, bf = _replace_blocks_in_lines(content_lines, replace_lines, current_file, project_root, allow_create=True, force_replace=force_replace)
                        failed_details.extend(bf)
                        if s > 0:
                            new_content = "\n".join(new_lines)
                            if not new_content.endswith("\n"):
                                new_content += "\n"
                            file_contents[current_file] = new_content
                            success_count += s
                            fail_count += f
                            skipped_count += sk
                            modified_files.add(current_file)
                        else:
                            fail_count += f
                            skipped_count += sk
                else:
                    content_normalized = content.replace("\r\n", "\n").strip()
                    if not force_replace and content_normalized == replace_text:
                        print(f"⏭ 全体置換スキップ (適用済み): {disp_path}")
                        skipped_count += 1
                    else:
                        new_content = "\n".join(replace_lines)
                        if not new_content.endswith("\n"):
                            new_content += "\n"
                        file_contents[current_file] = new_content
                        print(f"✔ ファイル全体置換成功: {disp_path}")
                        success_count += 1
                        modified_files.add(current_file)
            else:
                # モードAでの関数シグネチャ単位での関数丸ごと削除チェック
                if is_empty_replace:
                    first_non_empty = next((l for l in search_lines if l.strip()), "")
                    b_match = BLOCK_PATTERN.match(first_non_empty)
                    if b_match:
                        b_type = b_match.group(1) or 'function'
                        b_name = b_match.group(2) or b_match.group(3)
                        content_lines = content.splitlines()
                        ok, new_lines = delete_block_from_lines(content_lines, b_name, b_type)
                        if ok:
                            new_content = "\n".join(new_lines)
                            if new_content and not new_content.endswith("\n"):
                                new_content += "\n"
                            file_contents[current_file] = new_content
                            print(f"🗑 関数/クラス削除成功: {b_name} ({disp_path})")
                            success_count += 1
                            modified_files.add(current_file)
                            continue

                new_content, error_msg, is_skipped = find_and_replace(content, search_lines, replace_lines, force_replace)
                if is_skipped:
                    action_label = "リバート" if revert else ("削除" if is_empty_replace else "適用")
                    print(f"⏭ {action_label}スキップ (適用済み): {disp_path}")
                    skipped_count += 1
                elif new_content is not None:
                    file_contents[current_file] = new_content
                    action_label = "リバート" if revert else ("削除" if is_empty_replace else "適用")
                    print(f"✔ {action_label}成功: {disp_path}")
                    success_count += 1
                    modified_files.add(current_file)
                else:
                    action_label = "リバート" if revert else ("削除" if is_empty_replace else "適用")
                    print(f"✖ {action_label}失敗: {disp_path} (パッチ行 {block_line_no})\n  -> {error_msg}")
                    failed_details.append(f"{disp_path}: {action_label}失敗 (パッチ行 {block_line_no})\n  -> {error_msg}")
                    fail_count += 1
        else:
            if revert:
                print(f"✖ リバート失敗: 対象ファイルが存在しません: {disp_path}")
                failed_details.append(f"{disp_path}: 対象ファイルが存在しません")
                fail_count += 1
            else:
                new_content = "\n".join(replace_lines)
                if new_content and not new_content.endswith("\n"):
                    new_content += "\n"
                file_contents[current_file] = new_content
                print(f"✨ 新規作成成功: {disp_path}")
                success_count += 1
                modified_files.add(current_file)

    return commit_patch_transaction(
        file_contents=file_contents,
        original_contents=original_contents,
        modified_files=modified_files,
        files_to_delete=files_to_delete,
        project_root=project_root,
        success_count=success_count,
        fail_count=fail_count,
        skipped_count=skipped_count,
        failed_details=failed_details,
        dry_run=dry_run,
    )
