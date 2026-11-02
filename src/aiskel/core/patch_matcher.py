"""Module: @role: パッチ検索テキストとファイル内容の完全・柔軟・ファジー照合置換およびUnified Diff生成を担当する。"""
import difflib
from pathlib import Path
from typing import List, Tuple, Optional

from .syntax_block_editor import clean_excessive_blank_lines

def find_and_replace(
    content: str,
    search_lines: List[str],
    replace_lines: List[str],
    force_replace: bool = False
) -> Tuple[Optional[str], str, bool]:
    """
    完全一致、または柔軟なマッチングで置換・削除を行う
    戻り値: (置換後の文字列, エラーメッセージ, 置換済みフラグ)
    """
    if not search_lines:
        return None, "検索ブロックが空です。", False

    s_start = 0
    while s_start < len(search_lines) and not search_lines[s_start].strip():
        s_start += 1
    s_end = len(search_lines)
    while s_end > s_start and not search_lines[s_end-1].strip():
        s_end -= 1
        
    if s_start >= s_end:
        return None, "検索ブロックに有効なテキストが含まれていません。", False
        
    core_search = search_lines[s_start:s_end]
    is_delete = not any(line.strip() for line in replace_lines)
    
    content_normalized = content.replace("\r\n", "\n")
    search_text = "\n".join(core_search)
    replace_text = "\n".join(replace_lines)
    
    if not force_replace and not is_delete and replace_text.strip() and replace_text in content_normalized:
        return content_normalized, "", True

    content_lines = content_normalized.splitlines()

    # 1. 行単位での完全一致（コード削除時、改行が残ってゴミ空行になるのを根本防止）
    # なぜ必要か: コード削除時に単純文字列置換を行うと行末改行が残留して空行が生成されるため、行単位で安全に消去する
    search_len = len(core_search)
    for i in range(len(content_lines) - search_len + 1):
        if content_lines[i : i + search_len] == core_search:
            new_lines = content_lines[:i] + (replace_lines if not is_delete else []) + content_lines[i + search_len:]
            if is_delete:
                new_lines = clean_excessive_blank_lines(new_lines, i)
            result = "\n".join(new_lines)
            if content.endswith("\n") and not result.endswith("\n"):
                result += "\n"
            return result, "", False

    # 2. 文字列としての完全一致（インライン置換、または改行コード込みの完全一致）
    if search_text in content_normalized:
        if is_delete:
            result = content_normalized.replace(search_text, replace_text)
            cleaned_lines = clean_excessive_blank_lines(result.splitlines())
            result = "\n".join(cleaned_lines)
        else:
            result = content_normalized.replace(search_text, replace_text)
        if content.endswith("\n") and not result.endswith("\n"):
            result += "\n"
        return result, "", False

    # 3. 行単位の柔軟なマッチング (インデント無視)
    stripped_search = [s.strip() for s in core_search]
    for i in range(len(content_lines) - search_len + 1):
        match = True
        for j in range(search_len):
            if content_lines[i+j].strip() != stripped_search[j]:
                match = False
                break
        if match:
            new_lines = content_lines[:i] + (replace_lines if not is_delete else []) + content_lines[i+search_len:]
            if is_delete:
                new_lines = clean_excessive_blank_lines(new_lines, i)
            result = "\n".join(new_lines)
            if content.endswith("\n") and not result.endswith("\n"):
                result += "\n"
            return result, "", False

    # 4. 途中の空行も完全に無視したマッチング
    non_empty_content = [(idx, line.strip()) for idx, line in enumerate(content_lines) if line.strip()]
    non_empty_search = [s.strip() for s in core_search if s.strip()]
    ne_search_len = len(non_empty_search)
    
    if ne_search_len > 0 and len(non_empty_content) >= ne_search_len:
        for i in range(len(non_empty_content) - ne_search_len + 1):
            match = True
            for j in range(ne_search_len):
                if non_empty_content[i+j][1] != non_empty_search[j]:
                    match = False
                    break
            if match:
                start_idx = non_empty_content[i][0]
                end_idx = non_empty_content[i + ne_search_len - 1][0]
                new_lines = content_lines[:start_idx] + (replace_lines if not is_delete else []) + content_lines[end_idx+1:]
                if is_delete:
                    new_lines = clean_excessive_blank_lines(new_lines, start_idx)
                result = "\n".join(new_lines)
                if content.endswith("\n") and not result.endswith("\n"):
                    result += "\n"
                return result, "", False

    # 5. 最初と最後の行によるブロックマッチング
    if ne_search_len >= 2:
        first_line = non_empty_search[0]
        last_line = non_empty_search[-1]
        
        first_matches = [idx for idx, line in non_empty_content if line == first_line]
        last_matches = [idx for idx, line in non_empty_content if line == last_line]
        
        if len(first_matches) == 1 and len(last_matches) == 1:
            start_idx = first_matches[0]
            end_idx = last_matches[0]
            if start_idx < end_idx:
                new_lines = content_lines[:start_idx] + (replace_lines if not is_delete else []) + content_lines[end_idx+1:]
                if is_delete:
                    new_lines = clean_excessive_blank_lines(new_lines, start_idx)
                result = "\n".join(new_lines)
                if content.endswith("\n") and not result.endswith("\n"):
                    result += "\n"
                return result, "", False

    # 6. 行類似度マッチング (Fuzzy Matching): AIの微小なコメント差異や末尾カンマ表記揺れによる置換失敗を防止
    if ne_search_len >= 2 and len(non_empty_content) >= ne_search_len:
        search_block_text = "\n".join(non_empty_search)
        best_ratio = 0.0
        best_start_idx = -1
        best_end_idx = -1
        candidates_count = 0
        
        for window_size in (ne_search_len, ne_search_len - 1, ne_search_len + 1):
            if window_size <= 0 or len(non_empty_content) < window_size:
                continue
            for i in range(len(non_empty_content) - window_size + 1):
                window_lines = [non_empty_content[i + k][1] for k in range(window_size)]
                window_text = "\n".join(window_lines)
                ratio = difflib.SequenceMatcher(None, window_text, search_block_text).ratio()
                if ratio >= 0.85:
                    if ratio > best_ratio + 1e-4:
                        best_ratio = ratio
                        best_start_idx = non_empty_content[i][0]
                        best_end_idx = non_empty_content[i + window_size - 1][0]
                        candidates_count = 1
                    elif abs(ratio - best_ratio) <= 1e-4:
                        candidates_count += 1
                        
        if best_ratio >= 0.85 and candidates_count == 1:
            new_lines = content_lines[:best_start_idx] + (replace_lines if not is_delete else []) + content_lines[best_end_idx + 1:]
            if is_delete:
                new_lines = clean_excessive_blank_lines(new_lines, best_start_idx)
            result = "\n".join(new_lines)
            if content.endswith("\n") and not result.endswith("\n"):
                result += "\n"
            return result, "", False

    # 一致箇所の詳細診断 (エラー表示用)
    best_match_count = 0
    best_match_line = ""
    best_match_file_lineno = -1
    best_match_file_line = ""
    for i in range(len(non_empty_content)):
        match_count = 0
        while i + match_count < len(non_empty_content) and match_count < ne_search_len:
            if non_empty_content[i + match_count][1] == non_empty_search[match_count]:
                match_count += 1
            else:
                break
        if match_count > best_match_count:
            best_match_count = match_count
            if match_count < ne_search_len:
                best_match_line = non_empty_search[match_count]
                if i + match_count < len(non_empty_content):
                    best_match_file_lineno = non_empty_content[i + match_count][0] + 1
                    best_match_file_line = non_empty_content[i + match_count][1]

    if best_match_count > 0:
        file_hint = f" (ファイル行 {best_match_file_lineno})" if best_match_file_lineno > 0 else ""
        diff_detail = f"\n    検索指定: '{best_match_line}'"
        if best_match_file_line:
            diff_detail += f"\n    ファイル: '{best_match_file_line}'"
        return None, f"途中まで一致しましたが、以下の行がファイル内の記述と異なります{file_hint}:{diff_detail}", False

    target_search_line = non_empty_search[0] if non_empty_search else (core_search[0] if core_search else "")
    best_sim_ratio = 0.0
    best_sim_lineno = -1
    best_sim_line = ""
    for idx, line_str in non_empty_content:
        ratio = difflib.SequenceMatcher(None, target_search_line, line_str).ratio()
        if ratio > best_sim_ratio:
            best_sim_ratio = ratio
            best_sim_lineno = idx + 1
            best_sim_line = line_str

    if best_sim_ratio >= 0.6 and best_sim_lineno > 0:
        return None, (
            f"検索テキストがファイル内に見つかりませんでした:\n"
            f"    検索指定: '{target_search_line}'\n"
            f"    類似候補 (ファイル行 {best_sim_lineno}): '{best_sim_line}'"
        ), False

    if target_search_line:
        return None, f"検索テキストがファイル内に見つかりませんでした:\n    検索指定: '{target_search_line}'", False

    return None, "検索テキストがファイル内に見つかりませんでした。", False

def generate_diff_display(file_path: Path, original: str, updated: str, project_root: Path) -> str:
    """変更前後の文字列からカラー付き Unified Diff を生成する"""
    try:
        rel_path = file_path.relative_to(project_root).as_posix()
    except ValueError:
        rel_path = file_path.as_posix()
    
    orig_lines = original.splitlines(keepends=True)
    upd_lines = updated.splitlines(keepends=True)
    to_file = f"b/{rel_path}" if updated else "/dev/null"
    diff = list(difflib.unified_diff(orig_lines, upd_lines, fromfile=f"a/{rel_path}", tofile=to_file))
    if not diff:
        return ""
    
    colored_lines = []
    for line in diff:
        stripped = line.rstrip()
        if stripped.startswith("+++") or stripped.startswith("---"):
            colored_lines.append(f"\033[1m{stripped}\033[0m")
        elif stripped.startswith("+"):
            colored_lines.append(f"\033[32m{stripped}\033[0m")
        elif stripped.startswith("-"):
            colored_lines.append(f"\033[31m{stripped}\033[0m")
        elif stripped.startswith("@@"):
            colored_lines.append(f"\033[36m{stripped}\033[0m")
        else:
            colored_lines.append(stripped)
    return "\n".join(colored_lines)
