"""Module: @role: ソースコード内の関数・クラス・定数ブロックの構文範囲検出、削除・挿入、およびインポート文マージを担当する。"""
import re
from pathlib import Path
from typing import List, Tuple, Optional, Set

MODIFIER_PREFIX = (
    r'(?:(?:public|private|protected|internal|file|static|abstract|sealed|'
    r'final|partial|readonly|override|virtual|new|async|unsafe|extern|export|default|inline)\s+)*'
)

CONTROL_FLOW_KEYWORDS = {
    'if', 'else', 'for', 'foreach', 'while', 'do', 'switch', 'case', 'catch',
    'finally', 'using', 'lock', 'fixed', 'return', 'throw', 'new', 'get', 'set'
}

BLOCK_PATTERN = re.compile(
    r'^[ \t]*' + MODIFIER_PREFIX + r'(?:'
    r'(def|function|func|fn|class|interface|struct|record|enum)\s+([a-zA-Z0-9_]+)(?:[\s\(\{:<\[]|$)|'
    r'(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>|'
    r'(?:(?:public|private|protected|internal|file|static|abstract|sealed|final|partial|readonly|override|virtual|new|async|unsafe|extern)\s+)+(?:[a-zA-Z0-9_<>\[\],\?\s\*&]+\s+)?([a-zA-Z0-9_]+)\s*\('
    r')'
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

def clean_excessive_blank_lines(lines: List[str], change_idx: int = 0) -> List[str]:
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

def has_import_statements(lines: List[str]) -> bool:
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

def extract_import_lines(lines: List[str]) -> List[str]:
    # なぜ必要か: 関数内ローカルimportの誤抽出を防ぎ、トップレベルimportのみを抽出するため
    import_patterns = [
        re.compile(r'^(?:from\s+[a-zA-Z0-9_\.]+\s+import|import\s+[a-zA-Z0-9_\.]+)'),
        re.compile(r'^(?:export\s+)?import\s+'),
        re.compile(r'^(?:const|let|var)\s+.*=\s*require\('),
        re.compile(r'^#\s*include\s+[<"]'),
        re.compile(r'^(?:package|import)\s+'),
        re.compile(r'^(?:use\s+[a-zA-Z0-9_:]+|extern\s+crate)'),
        re.compile(r'^using\s+[a-zA-Z0-9_\.]+;'),
        re.compile(r'^(?:namespace|use)\s+[a-zA-Z0-9_\\]+;'),
    ]
    extracted = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith((' ', '\t')):
            i += 1
            continue
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

def _find_import_end_index(lines: List[str], start_idx: int) -> int:
    # なぜ必要か: 複数行インポート文(括弧ネストや行継続バックスラッシュ)の真の終端行を正確に特定するため
    paren_depth = 0
    i = start_idx
    while i < len(lines):
        line = lines[i]
        code_part = line.split('#')[0]
        paren_depth += (code_part.count('(') - code_part.count(')'))
        paren_depth += (code_part.count('[') - code_part.count(']'))
        paren_depth += (code_part.count('{') - code_part.count('}'))
        has_continuation = code_part.rstrip().endswith('\\')
        i += 1
        if paren_depth <= 0 and not has_continuation:
            break
    return i

def merge_import_lines(target_lines: List[str], import_lines: List[str]) -> List[str]:
    # なぜ必要か: 新規関数が依存するimport文を重複なくファイルの適切な位置（importブロック末尾）に挿入するため
    if not import_lines:
        return target_lines

    existing_stripped = {l.strip() for l in target_lines}
    new_imports = [l.strip() for l in import_lines if l.strip() and l.strip() not in existing_stripped]
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
    last_import_end_idx = -1
    idx = 0
    while idx < len(target_lines):
        line = target_lines[idx]
        if any(p.search(line) for p in import_patterns):
            end_idx = _find_import_end_index(target_lines, idx)
            last_import_end_idx = end_idx
            idx = end_idx
        else:
            idx += 1

    if last_import_end_idx != -1:
        insert_idx = last_import_end_idx
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

def find_new_block_insert_index(lines: List[str], file_path: Path) -> int:
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

def insert_block_into_lines(target_lines: List[str], block_lines: List[str], insert_idx: int) -> List[str]:
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

def find_assignment_end(lines: List[str], header_idx: int) -> int:
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

def find_block_range(lines: List[str], block_name: str, block_type: str) -> Tuple[int, int]:
    """
    行リストから指定された関数またはクラスの定義範囲（開始行、終了行）を返す。
    なぜ必要か: f-stringや辞書リテラル内の波括弧をブロック終了と誤認する致命的欠落バグを言語仕様レベルで根本解決
    """
    # なぜ必要か: None等の不正値によるre.escapeのTypeErrorクラッシュを未然防止
    if not block_name:
        return -1, -1

    if block_type in ('def', 'function', 'func', 'fn', 'method'):
        pattern = re.compile(
            r'^([ \t]*)' + MODIFIER_PREFIX + r'(?:'
            r'(?:def|function|func|fn)\s+' + re.escape(block_name) + r'\s*[\(\{]|'
            r'(?:const|let|var)\s+' + re.escape(block_name) + r'\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>|'
            r'(?:[a-zA-Z0-9_<>\[\],\?\s\*&]+\s+)?' + re.escape(block_name) + r'\s*\([^{;]*\)\s*(?:where\s+[^{]+)?(?:=>|\{|$)'
            r')'
        )
    elif block_type in ('class', 'interface', 'struct', 'record', 'enum', 'type'):
        # なぜ必要か: C#/TS等の修飾子(public/internal等)や型種別(interface/struct等)を捕捉
        pattern = re.compile(
            r'^([ \t]*)' + MODIFIER_PREFIX +
            r'(?:class|interface|struct|record|enum)\s+' + re.escape(block_name) + r'(?:[\s\(\{:<\[]|$)'
        )
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
        return header_idx, find_assignment_end(lines, header_idx)

    # なぜ必要か: Pythonのデコレータ(@)やC#の属性([Attr])を含めたブロック先頭を特定
    start_idx = header_idx
    while start_idx > 0:
        prev_line = lines[start_idx - 1]
        prev_stripped = prev_line.strip()
        if not prev_stripped:
            break
        prev_indent = len(prev_line) - len(prev_line.lstrip())
        is_annot = prev_stripped.startswith('@') or (prev_stripped.startswith('[') and prev_stripped.endswith(']'))
        if prev_indent == base_indent and is_annot:
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

def delete_block_from_lines(target_lines: List[str], block_name: str, block_type: str = 'function') -> Tuple[bool, List[str]]:
    # なぜ必要か: 指定された関数やクラスの全構文範囲（デコレータ含む）を検出し、安全かつ過剰空行を残さず消去するため
    if not block_name:
        return False, target_lines
    t_start, t_end = find_block_range(target_lines, block_name, block_type)
    if t_start == -1 and block_type == 'function':
        t_start, t_end = find_block_range(target_lines, block_name, 'class')
    if t_start == -1:
        return False, target_lines
    
    new_lines = target_lines[:t_start] + target_lines[t_end:]
    cleaned_lines = clean_excessive_blank_lines(new_lines, t_start)
    return True, cleaned_lines

def extract_blocks(lines: List[str]) -> List[Tuple[str, str, int, int]]:
    # なぜ必要か: C#/TS/Java等の修飾子や型種別(interface/struct等)を網羅的に捕捉
    type_regex = re.compile(
        r'^[ \t]*' + MODIFIER_PREFIX +
        r'(class|interface|struct|record|enum)\s+([a-zA-Z0-9_]+)(?:[\s\(\{:<\[]|$)'
    )
    func_regex = re.compile(
        r'^[ \t]*' + MODIFIER_PREFIX +
        r'(?:'
        r'(def|function|func|fn)\s+([a-zA-Z0-9_]+)(?:[\s\(\{:<\[]|$)|'
        r'(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>'
        r')'
    )
    typed_method_regex = re.compile(
        r'^[ \t]*(?:(?:public|private|protected|internal|file|static|abstract|sealed|final|partial|readonly|override|virtual|new|async|unsafe|extern)\s+)+'
        r'(?:[a-zA-Z0-9_<>\[\],\?\s\*&]+\s+)?'
        r'([a-zA-Z0-9_]+)\s*\([^{;]*\)\s*(?:where\s+[^{]+)?(?:=>|\{|$)'
    )
    const_regex = re.compile(
        r'^([ \t]*)(?:export\s+)?(?:(?:const|let|var|readonly)\s+)?'
        r'([a-zA-Z_][a-zA-Z0-9_]*)(?:\s*:[^=]+)?\s*=(?!\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>)'
    )
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        b_type = None
        b_name = None

        type_match = type_regex.match(line)
        if type_match:
            b_type = type_match.group(1)
            b_name = type_match.group(2)
        else:
            func_match = func_regex.match(line)
            if func_match:
                b_type = func_match.group(1) or 'function'
                b_name = func_match.group(2) or func_match.group(3)
            else:
                method_match = typed_method_regex.match(line)
                if method_match:
                    candidate_name = method_match.group(1)
                    if candidate_name not in CONTROL_FLOW_KEYWORDS:
                        b_type = 'function'
                        b_name = candidate_name

        if b_type and b_name:
            actual_start = i
            while actual_start > 0:
                prev_line = lines[actual_start - 1]
                prev_stripped = prev_line.strip()
                if not prev_stripped:
                    break
                is_annot = prev_stripped.startswith('@') or (prev_stripped.startswith('[') and prev_stripped.endswith(']'))
                if is_annot:
                    actual_start -= 1
                else:
                    break
            _, s_end = find_block_range(lines[i:], b_name, b_type)
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
                s_start, s_end = find_block_range(lines[i:], b_name, 'constant')
                if s_end != -1:
                    s_end += i
                    blocks.append(('constant', b_name, i, s_end))
                    i = max(s_end - 1, i)
        i += 1
    return blocks
