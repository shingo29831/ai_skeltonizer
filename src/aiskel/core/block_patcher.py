"""Module: @role: Markdownコードブロックからの関数・クラス単位の構文解析および自動置換・新規追加を実行する。"""
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .patch_committer import commit_patch_transaction
from .patch_result import PatchResult
from .syntax_block_editor import (
    extract_blocks,
    find_block_range,
    find_new_block_insert_index,
    insert_block_into_lines,
)

FILE_HEADER_PATTERN = re.compile(
    r'^[ \t]*(?:ファイルパス|File|ファイル|削除|Delete|Remove):\s*`?([a-zA-Z0-9_/\.\-:\\]+)(?:\s*[:\(]([a-zA-Z0-9_]+)\)?)?`?(?:\s*[\(\[]?(delete|remove|削除)[\)\]]?)?',
    re.IGNORECASE
)


def replace_blocks_in_lines(
    target_lines: List[str],
    source_lines: List[str],
    file_path: Path,
    project_root: Path,
    allow_create: bool = True,
    force_replace: bool = False
) -> Tuple[int, int, int, List[str], List[str]]:
    """行リスト内の指定クラス・関数ブロックを抽出し、置換または新規挿入を行う。"""
    success = 0
    fail = 0
    skipped = 0
    failed_items: List[str] = []
    result_lines = list(target_lines)

    blocks = extract_blocks(source_lines)
    for block_type, block_name, s_start, s_end in blocks:
        new_block_lines = source_lines[s_start:s_end]
        t_start, t_end = find_block_range(result_lines, block_name, block_type)
        type_label = "クラス" if block_type == "class" else "関数"
        try:
            disp_path = file_path.relative_to(project_root)
        except ValueError:
            disp_path = file_path

        block_text = "\n".join(new_block_lines).strip()

        if t_start != -1:
            target_block_text = "\n".join(result_lines[t_start:t_end]).strip()
            if not force_replace and target_block_text == block_text:
                print(f"⏭ {type_label}置換スキップ (適用済み): {block_name} ({disp_path})")
                skipped += 1
            else:
                result_lines = result_lines[:t_start] + new_block_lines + result_lines[t_end:]
                print(f"✔ {type_label}置換成功: {block_name} ({disp_path})")
                success += 1
        elif allow_create:
            # なぜ必要か: モードBで対象ファイル内に存在しないブロックを新規関数・クラスとして追加するため
            current_content = "\n".join(result_lines)
            if not force_replace and block_text in current_content:
                print(f"⏭ {type_label}追加スキップ (適用済み): {block_name} ({disp_path})")
                skipped += 1
            else:
                insert_idx = find_new_block_insert_index(result_lines, file_path)
                result_lines = insert_block_into_lines(result_lines, new_block_lines, insert_idx)
                print(f"✨ {type_label}新規作成成功: {block_name} ({disp_path})")
                success += 1
        else:
            print(f"✖ {type_label}置換失敗: 対象ファイルに{type_label} '{block_name}' が見つかりません。")
            failed_items.append(f"{disp_path}: {type_label} '{block_name}' が見つかりません")
            fail += 1

    return success, fail, skipped, result_lines, failed_items


def apply_block_replacement(
    patch_text: str,
    project_root: Path,
    target_file: Optional[Path] = None,
    dry_run: bool = False
) -> PatchResult:
    """Markdownコードブロック記法から関数・クラス単位の自動置換・追加を実行する。"""
    success_count = 0
    fail_count = 0
    skipped_count = 0
    failed_details: List[str] = []
    modified_files: Set[Path] = set()

    current_file = target_file
    lines = patch_text.splitlines()
    i = 0

    file_contents: Dict[Path, str] = {}
    original_contents: Dict[Path, str] = {}

    while i < len(lines):
        line = lines[i]
        file_match = FILE_HEADER_PATTERN.search(line)
        if file_match:
            raw_path = Path(file_match.group(1))
            current_file = raw_path.resolve() if raw_path.is_absolute() else (project_root / raw_path).resolve()
            if current_file not in file_contents:
                if current_file.exists():
                    orig_text = current_file.read_text(encoding="utf-8")
                    file_contents[current_file] = orig_text
                    original_contents[current_file] = orig_text
                else:
                    file_contents[current_file] = ""
                    original_contents[current_file] = ""

        if line.strip().startswith("```"):
            block_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block_lines.append(lines[i])
                i += 1

            if current_file:
                if current_file not in file_contents:
                    if current_file.exists():
                        orig_text = current_file.read_text(encoding="utf-8")
                        file_contents[current_file] = orig_text
                        original_contents[current_file] = orig_text
                    else:
                        file_contents[current_file] = ""
                        original_contents[current_file] = ""

                if not current_file.exists() and not file_contents[current_file]:
                    file_contents[current_file] = "\n".join(block_lines)
                    success_count += 1
                    modified_files.add(current_file)
                else:
                    target_lines = file_contents[current_file].splitlines()
                    s, f, sk, new_lines, bf = replace_blocks_in_lines(target_lines, block_lines, current_file, project_root)
                    success_count += s
                    fail_count += f
                    skipped_count += sk
                    failed_details.extend(bf)
                    if s > 0:
                        modified_files.add(current_file)
                    file_contents[current_file] = "\n".join(new_lines)
        i += 1

    return commit_patch_transaction(
        file_contents=file_contents,
        original_contents=original_contents,
        modified_files=modified_files,
        files_to_delete=set(),
        project_root=project_root,
        success_count=success_count,
        fail_count=fail_count,
        skipped_count=skipped_count,
        failed_details=failed_details,
        dry_run=dry_run,
    )
