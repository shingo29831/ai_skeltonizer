"""Module: @role: パッチテキストの構文解析、ファイルヘッダーおよび置換・削除チャンクの抽出・検証を担当する。"""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import List, Optional, Tuple

from .block_patcher import FILE_HEADER_PATTERN
from .syntax_block_editor import (
    DELETE_DIRECTIVE_PATTERN,
    DELETE_FILE_DIRECTIVE_PATTERN,
)

DELIMITER_START = re.compile(r"^[ \t]*<{4,}\s*$", re.MULTILINE)
DELIMITER_MID = re.compile(r"^[ \t]*={4,}\s*$", re.MULTILINE)
DELIMITER_END = re.compile(r"^[ \t]*>{4,}\s*$", re.MULTILINE)


@dataclass
class PatchChunk:
    file_path: Optional[Path]
    target_node: Optional[str]
    is_file_deletion: bool
    is_standalone_file_delete: bool
    search_lines: List[str]
    replace_lines: List[str]
    block_line_no: int
    del_node_name: Optional[str] = None


def check_and_warn_delimiter(line: str, line_no: int) -> None:
    # なぜ必要か: AIが標準仕様(4文字)と異なる文字数の区切りを出力した際にユーザーに注意喚起するため
    stripped = line.strip()
    if len(stripped) != 4:
        print(f"⚠ 警告: 区切り記号の長さが標準(4文字)と異なります (行 {line_no}: '{stripped}' -> 補正して継続)")


def has_patch_delimiters(patch_text: str) -> bool:
    """パッチテキストに置換ブロックの区切り記号が全て揃っているか判定する"""
    return bool(
        DELIMITER_START.search(patch_text)
        and DELIMITER_MID.search(patch_text)
        and DELIMITER_END.search(patch_text)
    )


def is_file_deletion_patch(patch_text: str) -> bool:
    """区切り記号がないパッチで、明示的なファイル削除ディレクティブのみが含まれているか判定する"""
    file_header_matches = list(FILE_HEADER_PATTERN.finditer(patch_text))
    return bool(
        file_header_matches
        and any(
            bool(m.group(3))
            or any(line.strip().lower().startswith(kw) for kw in ("削除:", "delete:", "remove:"))
            for m in file_header_matches
            for line in patch_text.splitlines()
            if m.group(0) in line
        )
    )


def parse_patch_chunks(
    patch_text: str,
    project_root: Path,
    default_target_file: Optional[Path] = None,
    revert: bool = False,
) -> Tuple[List[PatchChunk], List[str], int]:
    """パッチテキストを行単位で走査し、ファイル別・操作別のパッチチャンク群を抽出する"""
    lines = patch_text.splitlines()
    chunks: List[PatchChunk] = []
    failed_details: List[str] = []
    fail_count = 0

    current_file: Optional[Path] = default_target_file
    target_node: Optional[str] = None
    file_delete_flag: bool = False

    i = 0
    while i < len(lines):
        line = lines[i]
        file_match = FILE_HEADER_PATTERN.search(line)
        if file_match:
            raw_path = Path(file_match.group(1))
            current_file = raw_path.resolve() if raw_path.is_absolute() else (project_root / raw_path).resolve()
            target_node = file_match.group(2)
            file_delete_flag = bool(file_match.group(3)) or any(
                line.strip().lower().startswith(kw) for kw in ("削除:", "delete:", "remove:")
            )

            # ブロックを伴わない単体ファイル削除ディレクティブの処理
            if file_delete_flag and (i + 1 >= len(lines) or not DELIMITER_START.match(lines[i + 1])):
                chunks.append(
                    PatchChunk(
                        file_path=current_file,
                        target_node=target_node,
                        is_file_deletion=True,
                        is_standalone_file_delete=True,
                        search_lines=[],
                        replace_lines=[],
                        block_line_no=i + 1,
                    )
                )
                i += 1
                continue

            i += 1
            continue

        if DELIMITER_START.match(line):
            block_line_no = i + 1
            check_and_warn_delimiter(line, block_line_no)
            if not current_file:
                print(f"⚠ 警告: ファイルパスが指定されていないため、ブロックをスキップします (行: {i+1})")
                failed_details.append(f"行 {i+1}: ファイルパスが指定されていません")
                fail_count += 1
                while i < len(lines) and not DELIMITER_END.match(lines[i]):
                    i += 1
                while i + 1 < len(lines) and DELIMITER_END.match(lines[i + 1]):
                    i += 1
                i += 1
                continue

            search_lines: List[str] = []
            replace_lines: List[str] = []

            i += 1
            # なぜ必要か: AIが<<<<を重複出力した場合に余分な区切り行をスキップするため
            while i < len(lines) and DELIMITER_START.match(lines[i]):
                print(f"⚠ 警告: 重複した開始区切り行を検出しました (行 {i + 1}: '{lines[i].strip()}' -> スキップ)")
                i += 1

            while i < len(lines) and not DELIMITER_MID.match(lines[i]):
                search_lines.append(lines[i])
                i += 1

            if i < len(lines) and DELIMITER_MID.match(lines[i]):
                check_and_warn_delimiter(lines[i], i + 1)
                i += 1

            # なぜ必要か: AIが====の数や行を多く重複出力した場合に余分な区切りをスキップするため
            while i < len(lines) and DELIMITER_MID.match(lines[i]):
                print(f"⚠ 警告: 重複した中央区切り行を検出しました (行 {i + 1}: '{lines[i].strip()}' -> スキップ)")
                i += 1

            while i < len(lines) and not DELIMITER_END.match(lines[i]):
                replace_lines.append(lines[i])
                i += 1

            if i < len(lines) and DELIMITER_END.match(lines[i]):
                check_and_warn_delimiter(lines[i], i + 1)

            # なぜ必要か: AIが>>>>の数や行を多く重複出力した場合に余分な区切りをスキップするため
            while i + 1 < len(lines) and DELIMITER_END.match(lines[i + 1]):
                print(f"⚠ 警告: 重複した終了区切り行を検出しました (行 {i + 2}: '{lines[i+1].strip()}' -> スキップ)")
                i += 1

            if revert:
                search_lines, replace_lines = replace_lines, search_lines

            is_empty_search = not any(l.strip() for l in search_lines)
            is_empty_replace = not any(l.strip() for l in replace_lines)

            # 1. ファイル丸ごと削除判定
            is_file_del = file_delete_flag or any(
                DELETE_FILE_DIRECTIVE_PATTERN.match(l)
                for l in (replace_lines if not is_empty_replace else search_lines)
            )

            # 2. 関数・クラス等の構文ブロック削除ディレクティブ判定
            del_node = target_node
            if not del_node:
                for r_line in (replace_lines if not is_empty_replace else search_lines):
                    del_match = DELETE_DIRECTIVE_PATTERN.match(r_line)
                    if del_match:
                        del_node = del_match.group(1)
                        break

            chunks.append(
                PatchChunk(
                    file_path=current_file,
                    target_node=target_node,
                    is_file_deletion=is_file_del,
                    is_standalone_file_delete=False,
                    search_lines=search_lines,
                    replace_lines=replace_lines,
                    block_line_no=block_line_no,
                    del_node_name=del_node,
                )
            )
            i += 1
            continue

        i += 1

    return chunks, failed_details, fail_count
