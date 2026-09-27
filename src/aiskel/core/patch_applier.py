# src/aiskel/core/patch_applier.py
"""Module: @role: パッチテキストの解析およびインメモリ仮想バッファによるトランザクション保証付きファイル置換・新規ノード挿入・コードおよびファイル削除を実行する。"""
import difflib
import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Set

BLOCK_PATTERN = re.compile(
    r'^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:'
    r'(def|function|class)\s+([a-zA-Z0-9_]+)|'
    r'(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>'
    r')'
)

FILE_HEADER_PATTERN = re.compile(
    r'^[ \t]*(?:ファイルパス|File|ファイル|削除|Delete|Remove):\s*`?([a-zA-Z0-9_/\.\-:\\]+)(?:\s*[:\(]([a-zA-Z0-9_]+)\)?)?`?(?:\s*[\(\[]?(delete|remove|削除)[\)\]]?)?',
    re.IGNORECASE
)

DELETE_DIRECTIVE_PATTERN = re.compile(
    r'^[ \t]*(?:#|//|/\*|<!--)?\s*(?:delete|remove|削除)\s*[:\s]+'
    r'(?:def\s+|function\s+|class\s+)?([a-zA-Z0-9_]+)',
    re.IGNORECASE
)

DELETE_FILE_DIRECTIVE_PATTERN = re.compile(
    r'^[ \t]*(?:#|//|/\*|<!--)?\s*(?:delete|remove|削除)\s*(?:file|ファイル)?\s*(?:-->|\*/)?$',
    re.IGNORECASE
)

def _clean_excessive_blank_lines(lines: List[str], change_idx: int = 0) -> List[str]:
    # なぜ必要か: コード削除によって生じる3行以上の過剰な空行をPEP 8等の規約に合わせて最大2行に圧縮するため
    result: List[str] = []
    blank_count = 0
    for line in lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 2:
                result.append(line)
        else:
            blank_count = 0
            result.append(line)
    while result and not result[0].strip():
        result.pop(0)
    return result

def _has_import_statements(lines: List[str]) -> bool:
    import_patterns = [
        re.compile(r'^\s*(?:from\s+[a-zA-Z0-9_\.]+\s+import|import\s+[a-zA-Z0-9_\.]+)'),
        re.compile(r'^\s*(?:export\s+)?import\s+'),
        re.compile(r'^\s*(?:const|let|var)\s+.*=\s*require\('),
        re.compile(r'^\s*#\s*include\s+[<"]'),
        re.compile(r'^\s*(?:package|import)\s+'),
        re.compile(r'^\s*(?:use\s+[a-zA-Z0-9_:]+|extern\s+crate)'),
        re.compile(r'^\s*using\s+[a-zA-Z0-9_\.]+;'),
        re.compile(r'^\s*(?:namespace|use)\s+[a-zA-Z0-9_\\]+;'),
    ]
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*")):
            continue
        for pattern in import_patterns:
            if pattern.search(line):
                return True
    return False

def _extract_import_lines(lines: List[str]) -> List[str]:
    # なぜ必要か: 新規関数ブロックに含まれるimport文を抽出し、既存ファイル上部にマージするため
    import_patterns = [
        re.compile(r'^\s*(?:from\s+[a-zA-Z0-9_\.]+\s+import|import\s+[a-zA-Z0-9_\.]+)'),
        re.compile(r'^\s*(?:export\s+)?import\s+'),
        re.compile(r'^\s*(?:const|let|var)\s+.*=\s*require\('),
        re.compile(r'^\s*#\s*include\s+[<"]'),
        re.compile(r'^\s*(?:package|import)\s+'),
        re.compile(r'^\s*(?:use\s+[a-zA-Z0-9_:]+|extern\s+crate)'),
        re.compile(r'^\s*using\s+[a-zA-Z0-9_\.]+;'),
        re.compile(r'^\s*(?:namespace|use)\s+[a-zA-Z0-9_\\]+;'),
    ]
    extracted = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*")):
            i += 1
            continue
        if any(pattern.search(line) for pattern in import_patterns):
            extracted.append(line)
            open_parens = line.count('(') - line.count(')')
            while open_parens > 0 and i + 1 < len(lines):
                i += 1
                next_line = lines[i]
                extracted.append(next_line)
                open_parens += next_line.count('(') - next_line.count(')')
        i += 1
    return extracted

def _merge_import_lines(target_lines: List[str], import_lines: List[str]) -> List[str]:
    # なぜ必要か: 新規関数が依存するimport文を重複なくファイルの適切な位置（importブロック末尾）に挿入するため
    if not import_lines:
        return target_lines

    existing_stripped = {l.strip() for l in target_lines}
    new_imports = [l for l in import_lines if l.strip() and l.strip() not in existing_stripped]
    if not new_imports:
        return target_lines

    import_patterns = [
        re.compile(r'^\s*(?:from\s+[a-zA-Z0-9_\.]+\s+import|import\s+[a-zA-Z0-9_\.]+)'),
        re.compile(r'^\s*(?:export\s+)?import\s+'),
        re.compile(r'^\s*(?:const|let|var)\s+.*=\s*require\('),
        re.compile(r'^\s*#\s*include\s+[<"]'),
        re.compile(r'^\s*(?:package|import)\s+'),
        re.compile(r'^\s*(?:use\s+[a-zA-Z0-9_:]+|extern\s+crate)'),
        re.compile(r'^\s*using\s+[a-zA-Z0-9_\.]+;'),
        re.compile(r'^\s*(?:namespace|use)\s+[a-zA-Z0-9_\\]+;'),
    ]
    last_import_idx = -1
    for idx, line in enumerate(target_lines):
        if any(p.search(line) for p in import_patterns):
            last_import_idx = idx

    if last_import_idx != -1:
        insert_idx = last_import_idx + 1
        return target_lines[:insert_idx] + new_imports + target_lines[insert_idx:]

    insert_idx = 0
    in_docstring = False
    docstring_char = ""
    for idx, line in enumerate(target_lines):
        stripped = line.strip()
        if not in_docstring:
            if stripped.startswith(('"""', "'''")):
                docstring_char = stripped[:3]
                if stripped.count(docstring_char) >= 2 and len(stripped) > 3:
                    insert_idx = idx + 1
                    break
                in_docstring = True
            elif stripped.startswith(("#", "//")):
                insert_idx = idx + 1
            elif stripped:
                insert_idx = idx
                break
        else:
            if docstring_char in stripped:
                insert_idx = idx + 1
                break

    return target_lines[:insert_idx] + new_imports + [""] + target_lines[insert_idx:]

def _find_new_block_insert_index(lines: List[str], file_path: Path) -> int:
    # なぜ必要か: Pythonでif __name__ == '__main__':より前、その他の言語では末尾に新規関数を挿入するため
    ext = file_path.suffix.lower()
    if ext == ".py":
        main_pattern = re.compile(r'^[ \t]*if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:')
        for idx, line in enumerate(lines):
            if main_pattern.match(line):
                insert_idx = idx
                while insert_idx > 0 and not lines[insert_idx - 1].strip():
                    insert_idx -= 1
                return insert_idx
    return len(lines)

def _insert_block_into_lines(target_lines: List[str], block_lines: List[str], insert_idx: int) -> List[str]:
    # なぜ必要か: 挿入時に前後コードとの空行（2行）を整え、PEP 8等のスタイル規約を維持するため
    before = target_lines[:insert_idx]
    after = target_lines[insert_idx:]

    while before and not before[-1].strip():
        before.pop()
    while after and not after[0].strip():
        after.pop(0)

    result = list(before)
    if result:
        result.extend(["", ""])
    result.extend(block_lines)
    if after:
        result.extend(["", ""])
        result.extend(after)
    return result

def _find_assignment_end(lines: List[str], header_idx: int) -> int:
    """代入文の開始行から、括弧のネスト・複数行文字列・行継続を考慮して文の終了行を返す"""
    paren_depth = 0
    in_single_quote = False
    in_double_quote = False
    in_triple_single = False
    in_triple_double = False
    in_backtick = False

    end_idx = header_idx
    for idx in range(header_idx, len(lines)):
        line = lines[idx]
        i = 0
        n = len(line)
        has_line_continuation = line.rstrip().endswith('\\')

        while i < n:
            if not in_single_quote and not in_double_quote and not in_backtick:
                if not in_triple_single and line[i:i+3] == '"""':
                    in_triple_double = not in_triple_double
                    i += 3
                    continue
                elif not in_triple_double and line[i:i+3] == "'''":
                    in_triple_single = not in_triple_single
                    i += 3
                    continue

            if in_triple_double or in_triple_single:
                i += 1
                continue

            char = line[i]
            if char == '\\' and i + 1 < n:
                i += 2
                continue

            if char == '"' and not in_single_quote and not in_backtick:
                in_double_quote = not in_double_quote
            elif char == "'" and not in_double_quote and not in_backtick:
                in_single_quote = not in_single_quote
            elif char == '`' and not in_single_quote and not in_double_quote:
                in_backtick = not in_backtick
            elif not in_single_quote and not in_double_quote and not in_backtick:
                if char == '#' or line[i:i+2] == '//':
                    break
                if char in '([{':
                    paren_depth += 1
                elif char in ')]}':
                    paren_depth -= 1
            i += 1

        end_idx = idx + 1
        if (
            paren_depth <= 0
            and not in_single_quote
            and not in_double_quote
            and not in_triple_single
            and not in_triple_double
            and not in_backtick
            and not has_line_continuation
        ):
            break

    return end_idx


def _find_block_range(lines: List[str], block_name: str, block_type: str) -> Tuple[int, int]:
    """
    行リストから指定された関数またはクラスの定義範囲（開始行、終了行）を返す。
    なぜ必要か: f-stringや辞書リテラル内の波括弧をブロック終了と誤認する致命的欠落バグを言語仕様レベルで根本解決
    """
    if block_type in ('def', 'function'):
        pattern = re.compile(
            r'^([ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:'
            r'(?:def|function)\s+' + re.escape(block_name) + r'\s*[\(\{]|'
            r'(?:const|let|var)\s+' + re.escape(block_name) + r'\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>'
            r')'
        )
    elif block_type == 'class':
        # なぜ必要か: Pythonのコロン(:)や型引数([, <)を含めクラス定義シグネチャを言語横断で確実に捕捉
        pattern = re.compile(r'^([ \t]*)(?:export\s+)?(?:default\s+)?class\s+' + re.escape(block_name) + r'(?:[\s\(\{:<\[]|$)')
    elif block_type in ('constant', 'const', 'var', 'variable'):
        # なぜ必要か: モジュール定数・変数代入(型注釈やJSのexport/const/let/var対応)を確実に捕捉
        pattern = re.compile(
            r'^([ \t]*)(?:export\s+)?(?:(?:const|let|var|readonly)\s+)?'
            + re.escape(block_name)
            + r'(?:\s*:[^=]+)?\s*=(?!\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>)'
        )
    else:
        return -1, -1

    header_idx = -1
    base_indent = 0
    for i, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            header_idx = i
            base_indent = len(match.group(1))
            break

    if header_idx == -1:
        return -1, -1

    if block_type in ('constant', 'const', 'var', 'variable'):
        return header_idx, _find_assignment_end(lines, header_idx)

    # なぜ必要か: デコレータ行(@...)を含めたブロック先頭を特定するため
    start_idx = header_idx
    while start_idx > 0:
        prev_line = lines[start_idx - 1]
        prev_stripped = prev_line.strip()
        if not prev_stripped:
            break
        prev_indent = len(prev_line) - len(prev_line.lstrip())
        if prev_indent == base_indent and prev_stripped.startswith('@'):
            start_idx -= 1
        else:
            break

    # なぜ必要か: 複数行シグネチャの末尾を検出し、デコレータやシグネチャ行自体をインデント判定で誤終了させない
    sig_end_idx = header_idx
    paren_depth = 0
    for idx in range(header_idx, len(lines)):
        l = lines[idx]
        paren_depth += (l.count('(') - l.count(')'))
        paren_depth += (l.count('[') - l.count(']'))
        if paren_depth <= 0:
            sig_end_idx = idx
            break

    # なぜ必要か: Python（末尾:）とブレース言語（JS/TS/C/C++）でブロック終端判定を厳密に分岐
    sig_line = lines[sig_end_idx].split('#')[0].rstrip()
    is_python_syntax = sig_line.endswith(':')

    if not is_python_syntax:
        has_brace = False
        brace_depth = 0
        for idx in range(header_idx, len(lines)):
            line = lines[idx]
            open_c = line.count('{')
            close_c = line.count('}')
            if open_c > 0:
                has_brace = True
            brace_depth += (open_c - close_c)
            if has_brace and brace_depth <= 0:
                return start_idx, idx + 1

    # なぜ必要か: シグネチャ完了行の次から走査を開始し、f-stringや辞書リテラル内の{}による誤終了を完全防止
    end_idx = sig_end_idx + 1
    while end_idx < len(lines):
        line = lines[end_idx]
        if line.strip():
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                if not is_python_syntax and line.strip().startswith('}'):
                    end_idx += 1
                break
        end_idx += 1

    return start_idx, end_idx

def _delete_block_from_lines(target_lines: List[str], block_name: str, block_type: str = 'function') -> Tuple[bool, List[str]]:
    # なぜ必要か: 指定された関数やクラスの全構文範囲（デコレータ含む）を検出し、安全かつ過剰空行を残さず消去するため
    t_start, t_end = _find_block_range(target_lines, block_name, block_type)
    if t_start == -1 and block_type == 'function':
        t_start, t_end = _find_block_range(target_lines, block_name, 'class')
    if t_start == -1:
        return False, target_lines
    
    new_lines = target_lines[:t_start] + target_lines[t_end:]
    cleaned_lines = _clean_excessive_blank_lines(new_lines, t_start)
    return True, cleaned_lines

def _extract_blocks(lines: List[str]) -> List[Tuple[str, str, int, int]]:
    # なぜ必要か: クラス定義(コロンや型引数)及び関数定義を確実に捕捉する汎用ブロック正規表現
    block_regex = re.compile(
        r'^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:'
        r'(def|class|function)\s+([a-zA-Z0-9_]+)(?:[\s\(\{:<\[]|$)|'
        r'(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>'
        r')'
    )
    const_regex = re.compile(
        r'^([ \t]*)(?:export\s+)?(?:(?:const|let|var|readonly)\s+)?'
        r'([a-zA-Z_][a-zA-Z0-9_]*)(?:\s*:[^=]+)?\s*=(?!\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>)'
    )
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = block_regex.match(line) or BLOCK_PATTERN.match(line)
        if match:
            b_type = match.group(1) or 'function'
            b_name = match.group(2) or match.group(3)
            actual_start = i
            while actual_start > 0:
                prev_line = lines[actual_start - 1]
                prev_stripped = prev_line.strip()
                if not prev_stripped:
                    break
                if prev_stripped.startswith('@'):
                    actual_start -= 1
                else:
                    break
            _, s_end = _find_block_range(lines[i:], b_name, b_type)
            if s_end != -1:
                s_end += i
                blocks.append((b_type, b_name, actual_start, s_end))
                i = max(s_end - 1, i)
                i += 1
                continue

        # なぜ必要か: トップレベル定数・設定値代入をノード一覧やサジェストに反映しAI要求不整合を根本解決
        const_match = const_regex.match(line)
        if const_match:
            indent = len(const_match.group(1))
            b_name = const_match.group(2)
            if (indent == 0 or (b_name.isupper() and len(b_name) > 1)) and b_name not in ('if', 'for', 'while', 'return'):
                s_start, s_end = _find_block_range(lines[i:], b_name, 'constant')
                if s_end != -1:
                    s_end += i
                    blocks.append(('constant', b_name, i, s_end))
                    i = max(s_end - 1, i)
        i += 1
    return blocks

def _find_and_replace(content: str, search_lines: List[str], replace_lines: List[str], force_replace: bool = False) -> Tuple[Optional[str], str, bool]:
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
                new_lines = _clean_excessive_blank_lines(new_lines, i)
            result = "\n".join(new_lines)
            if content.endswith("\n") and not result.endswith("\n"):
                result += "\n"
            return result, "", False

    # 2. 文字列としての完全一致（インライン置換、または改行コード込みの完全一致）
    if search_text in content_normalized:
        if is_delete:
            result = content_normalized.replace(search_text, replace_text)
            cleaned_lines = _clean_excessive_blank_lines(result.splitlines())
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
                new_lines = _clean_excessive_blank_lines(new_lines, i)
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
                    new_lines = _clean_excessive_blank_lines(new_lines, start_idx)
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
                    new_lines = _clean_excessive_blank_lines(new_lines, start_idx)
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
                new_lines = _clean_excessive_blank_lines(new_lines, best_start_idx)
            result = "\n".join(new_lines)
            if content.endswith("\n") and not result.endswith("\n"):
                result += "\n"
            return result, "", False

    # 一致箇所の詳細診断 (エラー表示用)
    best_match_count = 0
    best_match_line = ""
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

    if best_match_count > 0:
        return None, f"途中まで一致しましたが、以下の行がファイル内の記述と異なります:\n    '{best_match_line}'", False

    return None, "検索テキストがファイル内に見つかりませんでした。", False

def _replace_blocks_in_lines(
    target_lines: List[str],
    source_lines: List[str],
    file_path: Path,
    project_root: Path,
    allow_create: bool = True,
    force_replace: bool = False
) -> Tuple[int, int, int, List[str]]:
    success = 0
    fail = 0
    skipped = 0
    result_lines = list(target_lines)
    
    blocks = _extract_blocks(source_lines)
    for block_type, block_name, s_start, s_end in blocks:
        new_block_lines = source_lines[s_start:s_end]
        t_start, t_end = _find_block_range(result_lines, block_name, block_type)
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
                insert_idx = _find_new_block_insert_index(result_lines, file_path)
                result_lines = _insert_block_into_lines(result_lines, new_block_lines, insert_idx)
                print(f"✨ {type_label}新規作成成功: {block_name} ({disp_path})")
                success += 1
        else:
            print(f"✖ {type_label}置換失敗: 対象ファイルに{type_label} '{block_name}' が見つかりません。")
            fail += 1
        
    return success, fail, skipped, result_lines

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

def _apply_block_replacement(patch_text: str, project_root: Path, target_file: Path | None, dry_run: bool = False) -> Tuple[int, int, int, Set[Path]]:
    success_count = 0
    fail_count = 0
    skipped_count = 0
    modified_files: Set[Path] = set()
    
    current_file = target_file
    lines = patch_text.splitlines()
    i = 0
    
    file_contents: Dict[Path, List[str]] = {}
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
                    file_contents[current_file] = orig_text.splitlines()
                    original_contents[current_file] = orig_text
                else:
                    file_contents[current_file] = []
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
                        file_contents[current_file] = orig_text.splitlines()
                        original_contents[current_file] = orig_text
                    else:
                        file_contents[current_file] = []
                        original_contents[current_file] = ""
                    
                if not current_file.exists() and not file_contents[current_file]:
                    file_contents[current_file].extend(block_lines)
                    success_count += 1
                    modified_files.add(current_file)
                else:
                    s, f, sk, new_lines = _replace_blocks_in_lines(file_contents[current_file], block_lines, current_file, project_root)
                    success_count += s
                    fail_count += f
                    skipped_count += sk
                    if s > 0:
                        modified_files.add(current_file)
                    file_contents[current_file] = new_lines
        i += 1

    if fail_count > 0:
        return success_count, fail_count, skipped_count, set()

    if dry_run:
        for path in modified_files:
            orig = original_contents.get(path, "")
            upd = "\n".join(file_contents[path])
            diff_text = generate_diff_display(path, orig, upd, project_root)
            if diff_text:
                print(diff_text)
        return success_count, fail_count, skipped_count, modified_files

    for path in modified_files:
        if path in file_contents:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_content = "\n".join(file_contents[path])
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            path.write_text(new_content, encoding="utf-8")
        
    return success_count, fail_count, skipped_count, modified_files

def apply_patch(
    patch_text: str,
    project_root: Path,
    target_file: Path | None = None,
    force_replace: bool = False,
    revert: bool = False,
    dry_run: bool = False
) -> Tuple[int, int, int, Set[Path]]:
    """
    パッチテキストを解析し、トランザクション保証(All-or-Nothing)の下でファイルを書き換える。
    コード削除およびファイル自体の削除操作にも対応する。
    """
    if "<<<<" not in patch_text or "====" not in patch_text or ">>>>" not in patch_text:
        # 明示的なファイル削除ディレクティブのみのパッチ判定
        file_header_matches = list(FILE_HEADER_PATTERN.finditer(patch_text))
        if file_header_matches and any(bool(m.group(3)) or any(line.strip().lower().startswith(kw) for kw in ("削除:", "delete:", "remove:")) for m in file_header_matches for line in patch_text.splitlines() if m.group(0) in line):
            pass
        else:
            if revert:
                print("✖ エラー: リバート処理には置換ブロック(<<<< ==== >>>>)が必要です。")
                return 0, 1, 0, set()
            print("ℹ 置換ブロック(<<<<)が見つからないため、関数・クラス単位の自動置換を試みます...")
            return _apply_block_replacement(patch_text, project_root, target_file, dry_run=dry_run)

    success_count = 0
    fail_count = 0
    skipped_count = 0
    modified_files: Set[Path] = set()
    files_to_delete: Set[Path] = set()

    file_contents: Dict[Path, str] = {}
    original_contents: Dict[Path, str] = {}

    current_file: Path | None = target_file
    target_node: Optional[str] = None
    file_delete_flag: bool = False

    lines = patch_text.splitlines()
    i = 0
    
    while i < len(lines):
        line = lines[i]
        file_match = FILE_HEADER_PATTERN.search(line)
        if file_match:
            raw_path = Path(file_match.group(1))
            current_file = raw_path.resolve() if raw_path.is_absolute() else (project_root / raw_path).resolve()
            target_node = file_match.group(2)
            file_delete_flag = bool(file_match.group(3)) or any(line.strip().lower().startswith(kw) for kw in ("削除:", "delete:", "remove:"))
            
            if current_file not in file_contents:
                if current_file.exists():
                    orig_text = current_file.read_text(encoding="utf-8")
                    file_contents[current_file] = orig_text
                    original_contents[current_file] = orig_text
                else:
                    file_contents[current_file] = ""
                    original_contents[current_file] = ""
            
            # ブロックを伴わない単体ファイル削除ディレクティブの処理
            if file_delete_flag and (i + 1 >= len(lines) or "<<<<" not in lines[i+1]):
                try:
                    disp_path = current_file.relative_to(project_root)
                except ValueError:
                    disp_path = current_file
                if not current_file.exists():
                    print(f"⏭ ファイル削除スキップ (既に存在しません): {disp_path}")
                    skipped_count += 1
                else:
                    files_to_delete.add(current_file)
                    modified_files.add(current_file)
                    file_contents[current_file] = ""
                    print(f"🗑 ファイル削除成功: {disp_path}")
                    success_count += 1
                i += 1
                continue

            i += 1
            continue

        if line.strip() == "<<<<":
            if not current_file:
                print(f"⚠ 警告: ファイルパスが指定されていないため、ブロックをスキップします (行: {i+1})")
                fail_count += 1
                while i < len(lines) and lines[i].strip() != ">>>>":
                    i += 1
                i += 1
                continue

            if current_file not in file_contents:
                if current_file.exists():
                    orig_text = current_file.read_text(encoding="utf-8")
                    file_contents[current_file] = orig_text
                    original_contents[current_file] = orig_text
                else:
                    file_contents[current_file] = ""
                    original_contents[current_file] = ""

            search_lines = []
            replace_lines = []
            
            i += 1
            while i < len(lines) and lines[i].strip() != "====":
                search_lines.append(lines[i])
                i += 1
                
            i += 1
            while i < len(lines) and lines[i].strip() != ">>>>":
                replace_lines.append(lines[i])
                i += 1

            if revert:
                search_lines, replace_lines = replace_lines, search_lines

            try:
                disp_path = current_file.relative_to(project_root)
            except ValueError:
                disp_path = current_file

            is_empty_search = not any(line.strip() for line in search_lines)
            is_empty_replace = not any(line.strip() for line in replace_lines)
            content = file_contents[current_file]

            # 1. ファイル丸ごと削除判定
            is_file_deletion = file_delete_flag or any(DELETE_FILE_DIRECTIVE_PATTERN.match(l) for l in (replace_lines if not is_empty_replace else search_lines))
            if is_file_deletion:
                if not current_file.exists() and not content:
                    print(f"⏭ ファイル削除スキップ (既に存在しません): {disp_path}")
                    skipped_count += 1
                else:
                    files_to_delete.add(current_file)
                    modified_files.add(current_file)
                    file_contents[current_file] = ""
                    print(f"🗑 ファイル削除成功: {disp_path}")
                    success_count += 1
                i += 1
                continue

            # 2. 関数・クラス等の構文ブロック削除ディレクティブ判定
            del_node_name = target_node
            if not del_node_name:
                for r_line in (replace_lines if not is_empty_replace else search_lines):
                    del_match = DELETE_DIRECTIVE_PATTERN.match(r_line)
                    if del_match:
                        del_node_name = del_match.group(1)
                        break

            if del_node_name and is_empty_replace:
                content_lines = content.splitlines()
                ok, new_lines = _delete_block_from_lines(content_lines, del_node_name)
                if ok:
                    new_content = "\n".join(new_lines)
                    if new_content and not new_content.endswith("\n"):
                        new_content += "\n"
                    file_contents[current_file] = new_content
                    print(f"🗑 関数/クラス削除成功: {del_node_name} ({disp_path})")
                    success_count += 1
                    modified_files.add(current_file)
                else:
                    # 既に削除済みかチェック
                    target_b_start, _ = _find_block_range(content_lines, del_node_name, "function")
                    if target_b_start == -1:
                        target_b_start, _ = _find_block_range(content_lines, del_node_name, "class")
                    if target_b_start == -1:
                        print(f"⏭ 関数/クラス削除スキップ (既に削除済み): {del_node_name} ({disp_path})")
                        skipped_count += 1
                    else:
                        print(f"✖ 関数/クラス削除失敗: {del_node_name} ({disp_path})")
                        fail_count += 1
                i += 1
                continue

            # 3. 既存ファイルが存在する場合の処理
            if current_file.exists() or content:
                if is_empty_search:
                    if revert:
                        print(f"✖ リバート失敗: 空の検索ブロックに対するリバートは行えません: {disp_path}")
                        fail_count += 1
                        i += 1
                        continue

                    replace_text = "\n".join(replace_lines).strip()
                    if not replace_text:
                        print(f"✖ 置換失敗: 置換後コードが空です: {disp_path}")
                        fail_count += 1
                        i += 1
                        continue

                    content_lines = content.splitlines()
                    existing_has_imports = _has_import_statements(content_lines)
                    replace_has_imports = _has_import_statements(replace_lines)
                    blocks = _extract_blocks(replace_lines)
                    existing_blocks = _extract_blocks(content_lines)

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
                        import_lines = _extract_import_lines(replace_lines)
                        if import_lines:
                            content_lines = _merge_import_lines(content_lines, import_lines)

                        if len(blocks) == 1 and len(replace_lines) <= (blocks[0][3] - blocks[0][2] + 10 + len(import_lines)):
                            b_type, b_name, _, _ = blocks[0]
                            import_set = {l.strip() for l in import_lines}
                            single_block_lines = [l for l in replace_lines if l.strip() not in import_set]
                            while single_block_lines and not single_block_lines[0].strip():
                                single_block_lines.pop(0)
                            while single_block_lines and not single_block_lines[-1].strip():
                                single_block_lines.pop()

                            t_start, t_end = _find_block_range(content_lines, b_name, b_type)
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
                                    insert_idx = _find_new_block_insert_index(content_lines, current_file)
                                    new_lines = _insert_block_into_lines(content_lines, single_block_lines, insert_idx)
                                    new_content = "\n".join(new_lines)
                                    if not new_content.endswith("\n"):
                                        new_content += "\n"
                                    file_contents[current_file] = new_content
                                    print(f"✨ {type_label}新規作成成功: {b_name} ({disp_path})")
                                    success_count += 1
                                    modified_files.add(current_file)
                        else:
                            s, f, sk, new_lines = _replace_blocks_in_lines(content_lines, replace_lines, current_file, project_root, allow_create=True, force_replace=force_replace)
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
                            ok, new_lines = _delete_block_from_lines(content_lines, b_name, b_type)
                            if ok:
                                new_content = "\n".join(new_lines)
                                if new_content and not new_content.endswith("\n"):
                                    new_content += "\n"
                                file_contents[current_file] = new_content
                                print(f"🗑 関数/クラス削除成功: {b_name} ({disp_path})")
                                success_count += 1
                                modified_files.add(current_file)
                                i += 1
                                continue

                    new_content, error_msg, is_skipped = _find_and_replace(content, search_lines, replace_lines, force_replace)
                    if is_skipped:
                        action_label = "リバート" if revert else ("削除" if is_empty_replace else "置換")
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
                        print(f"✖ {action_label}失敗: {disp_path}\n  -> {error_msg}")
                        fail_count += 1
            else:
                if revert:
                    print(f"✖ リバート失敗: 対象ファイルが存在しません: {disp_path}")
                    fail_count += 1
                else:
                    new_content = "\n".join(replace_lines)
                    if new_content and not new_content.endswith("\n"):
                        new_content += "\n"
                    file_contents[current_file] = new_content
                    print(f"✨ 新規作成成功: {disp_path}")
                    success_count += 1
                    modified_files.add(current_file)
                
        i += 1

    # トランザクション判定: 1箇所でも失敗した場合はディスクへの書き込み・削除を行わず中止
    if fail_count > 0:
        print(f"\n⛔ 置換エラーが発生したため、トランザクションを中断しました。ディスクへの書き込み・削除は一切行われていません (All-or-Nothing)。")
        return success_count, fail_count, skipped_count, set()

    # Dry-runプレビュー出力
    if dry_run:
        print("\n🔍 [DRY-RUN] 差分プレビュー (ディスクは変更されません):")
        for path in modified_files:
            orig = original_contents.get(path, "")
            diff_text = generate_diff_display(path, orig, file_contents.get(path, ""), project_root)
            if diff_text:
                print(diff_text)
        return success_count, fail_count, skipped_count, modified_files

    # トランザクション確定: 全ての変更・削除をディスクに一括反映
    for path in files_to_delete:
        if path.exists():
            path.unlink()
            try:
                parent = path.parent
                if parent != project_root and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

    for path in modified_files:
        if path not in files_to_delete and path in file_contents:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file_contents[path], encoding="utf-8")

    return success_count, fail_count, skipped_count, modified_files
