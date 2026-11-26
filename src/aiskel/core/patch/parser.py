"""Module: @role: パッチテキストの構文解析、ファイルヘッダーおよび置換・削除チャンクの抽出・検証を担当する。"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import List, Optional, Tuple

from ..block_patcher import FILE_HEADER_PATTERN
from ..syntax_block_editor import (
    DELETE_DIRECTIVE_PATTERN,
    DELETE_FILE_DIRECTIVE_PATTERN,
)

DELIMITER_START = re.compile(r"^[ \t]*<{4,}\s*$", re.MULTILINE)
DELIMITER_MID = re.compile(r"^[ \t]*={4,}\s*$", re.MULTILINE)
DELIMITER_END = re.compile(r"^[ \t]*>{4,}\s*$", re.MULTILINE)
MOVE_HEADER_PATTERN = re.compile(
    r"^[ \t]*(?:移動|名前変更|リネーム|ファイル移動|move|mv|rename|ファイルパス|File(?:\s+path)?|ファイル)"
    r"[ \t]*[:：][ \t]*(.+?)[ \t]*(?:->|=>)[ \t]*(.+?)[ \t]*$",
    re.IGNORECASE,
)


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
    is_file_move: bool = False
    move_target_path: Optional[Path] = None


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


def _strip_path_wrappers(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("`", '"', "'"):
        return s[1:-1].strip()
    return s.strip("`").strip()


def _resolve_patch_path(raw_str: str, project_root: Path) -> Path:
    raw_path = Path(raw_str)
    candidate = raw_path if raw_path.is_absolute() else (project_root / raw_path)
    # なぜ必要か: case-insensitive FSでresolve()が移動先ファイル名の大文字小文字を既存名へ上書きするのを防ぐため
    normalized = Path(os.path.abspath(str(candidate)))
    if normalized.name:
        return normalized.parent.resolve() / normalized.name
    return normalized.resolve()


def is_file_deletion_patch(patch_text: str) -> bool:
    """区切り記号がないパッチで、明示的なファイル削除ディレクティブのみが含まれているか判定する"""
    in_block = False
    for line in patch_text.splitlines():
        if not in_block and DELIMITER_START.match(line):
            in_block = True
            continue
        if in_block:
            if DELIMITER_END.match(line):
                in_block = False
            continue
        m = FILE_HEADER_PATTERN.search(line)
        if m and (
            bool(m.group(3))
            or line.strip().lower().startswith(("削除:", "delete:", "remove:"))
        ):
            return True
    return False


def is_file_move_patch(patch_text: str) -> bool:
    """置換ブロック外の行に明示的なファイル移動・名前変更ディレクティブが含まれるか判定する"""
    in_block = False
    for line in patch_text.splitlines():
        if not in_block and DELIMITER_START.match(line):
            in_block = True
            continue
        if in_block:
            if DELIMITER_END.match(line):
                in_block = False
            continue
        if MOVE_HEADER_PATTERN.match(line):
            return True
    return False


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
        move_match = MOVE_HEADER_PATTERN.match(line)
        if move_match:
            raw_src_str = _strip_path_wrappers(move_match.group(1))
            raw_dst_str = _strip_path_wrappers(move_match.group(2))
            if not raw_src_str or not raw_dst_str or "->" in raw_src_str or "=>" in raw_src_str:
                failed_details.append(f"行 {i + 1}: 移動/名前変更のパス指定が不正です: {line.strip()}")
                fail_count += 1
                i += 1
                continue

            src_path = _resolve_patch_path(raw_src_str, project_root)
            dst_is_dir_hint = raw_dst_str.endswith(("/", "\\"))
            dst_path = _resolve_patch_path(raw_dst_str, project_root)
            if dst_is_dir_hint or (dst_path.exists() and dst_path.is_dir()):
                dst_path = _resolve_patch_path(str(dst_path / src_path.name), project_root)

            # なぜ必要か: 順方向基準で後続編集の対象パスを確定し、リバート時は逆順・逆方向で安全に復元するため
            current_file = dst_path
            target_node = None
            file_delete_flag = False
            chunk_src, chunk_dst = (dst_path, src_path) if revert else (src_path, dst_path)
            chunks.append(
                PatchChunk(
                    file_path=chunk_src,
                    target_node=None,
                    is_file_deletion=False,
                    is_standalone_file_delete=False,
                    search_lines=[],
                    replace_lines=[],
                    block_line_no=i + 1,
                    is_file_move=True,
                    move_target_path=chunk_dst,
                )
            )
            i += 1
            continue

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

    if revert and any(c.is_file_move for c in chunks):
        chunks.reverse()

    return chunks, failed_details, fail_count
