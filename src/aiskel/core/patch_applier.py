# src/aiskel/core/patch_applier.py
import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict

def _find_and_replace(content: str, search_lines: List[str], replace_lines: List[str]) -> Tuple[Optional[str], str]:
    """
    完全一致、または柔軟なマッチングで置換を行う
    戻り値: (置換後の文字列, エラーメッセージ)
    """
    if not search_lines:
        return None, "検索ブロックが空です。"

    # 検索ブロックの前後の空行を取り除く
    s_start = 0
    while s_start < len(search_lines) and not search_lines[s_start].strip():
        s_start += 1
    s_end = len(search_lines)
    while s_end > s_start and not search_lines[s_end-1].strip():
        s_end -= 1
        
    if s_start >= s_end:
        return None, "検索ブロックに有効なテキストが含まれていません。"
        
    core_search = search_lines[s_start:s_end]
    
    # 1. 改行コードを統一して完全一致検索
    content_normalized = content.replace("\r\n", "\n")
    search_text = "\n".join(core_search)
    replace_text = "\n".join(replace_lines)
    
    if search_text in content_normalized:
        return content_normalized.replace(search_text, replace_text), ""

    # 2. 行単位の柔軟なマッチング (インデント無視)
    content_lines = content_normalized.splitlines()
    stripped_search = [s.strip() for s in core_search]
    search_len = len(stripped_search)
    
    for i in range(len(content_lines) - search_len + 1):
        match = True
        for j in range(search_len):
            if content_lines[i+j].strip() != stripped_search[j]:
                match = False
                break
        if match:
            new_lines = content_lines[:i] + replace_lines + content_lines[i+search_len:]
            result = "\n".join(new_lines)
            if content.endswith("\n") and not result.endswith("\n"):
                result += "\n"
            return result, ""

    # 3. 途中の空行も完全に無視したマッチング
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
                new_lines = content_lines[:start_idx] + replace_lines + content_lines[end_idx+1:]
                result = "\n".join(new_lines)
                if content.endswith("\n") and not result.endswith("\n"):
                    result += "\n"
                return result, ""

    # 4. 最初と最後の行によるブロックマッチング (究極のフォールバック)
    if ne_search_len >= 2:
        first_line = non_empty_search[0]
        last_line = non_empty_search[-1]
        
        first_matches = [idx for idx, line in non_empty_content if line == first_line]
        last_matches = [idx for idx, line in non_empty_content if line == last_line]
        
        if len(first_matches) == 1 and len(last_matches) == 1:
            start_idx = first_matches[0]
            end_idx = last_matches[0]
            if start_idx < end_idx:
                new_lines = content_lines[:start_idx] + replace_lines + content_lines[end_idx+1:]
                result = "\n".join(new_lines)
                if content.endswith("\n") and not result.endswith("\n"):
                    result += "\n"
                return result, ""
        elif len(first_matches) == 0:
            return None, f"検索ブロックの最初の行が見つかりません: '{first_line}'"
        elif len(last_matches) == 0:
            return None, f"検索ブロックの最後の行が見つかりません: '{last_line}'"

    # どこまで一致したかを調べる（デバッグ用）
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
        return None, f"途中まで一致しましたが、以下の行がファイル内の記述と異なります:\n    '{best_match_line}'"

    return None, "検索テキストがファイル内に全く見つかりませんでした。"


def _find_block_range(lines: List[str], block_name: str, block_type: str) -> Tuple[int, int]:
    """
    行リストから指定された関数またはクラスの定義範囲（開始行、終了行）を返す。
    インデントレベルを解析して終端を判定する。
    """
    if block_type in ('def', 'function'):
        pattern = re.compile(r'^([ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|function)\s+' + re.escape(block_name) + r'\s*\(')
    elif block_type == 'class':
        pattern = re.compile(r'^([ \t]*)(?:export\s+)?(?:default\s+)?class\s+' + re.escape(block_name) + r'(?:[\s\(\{]|$)')
    else:
        return -1, -1
    
    start_idx = -1
    base_indent = 0
    for i, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            start_idx = i
            base_indent = len(match.group(1))
            break
            
    if start_idx == -1:
        return -1, -1
        
    end_idx = start_idx + 1
    while end_idx < len(lines):
        line = lines[end_idx]
        if line.strip():
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                # JS/TSの閉じカッコ行はブロックに含める
                if line.strip().startswith('}'):
                    end_idx += 1
                break
        end_idx += 1
        
    return start_idx, end_idx


def _replace_blocks_in_lines(target_lines: List[str], source_lines: List[str], file_path: Path, project_root: Path) -> Tuple[int, int, List[str]]:
    """
    source_lines 内の関数・クラスを抽出し、target_lines 内の同名ブロックを置換する。
    """
    success = 0
    fail = 0
    result_lines = list(target_lines)
    
    block_pattern = re.compile(r'^([ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?(def|function|class)\s+([a-zA-Z0-9_]+)')
    
    i = 0
    while i < len(source_lines):
        match = block_pattern.match(source_lines[i])
        if match:
            block_type = match.group(2)
            block_name = match.group(3)
            
            s_start, s_end = _find_block_range(source_lines[i:], block_name, block_type)
            if s_start != -1:
                s_start += i
                s_end += i
                new_block_lines = source_lines[s_start:s_end]
                
                t_start, t_end = _find_block_range(result_lines, block_name, block_type)
                if t_start != -1:
                    result_lines = result_lines[:t_start] + new_block_lines + result_lines[t_end:]
                    type_label = "クラス" if block_type == "class" else "関数"
                    print(f"✅ {type_label}置換成功: {block_name} ({file_path.relative_to(project_root)})")
                    success += 1
                else:
                    type_label = "クラス" if block_type == "class" else "関数"
                    print(f"❌ {type_label}置換失敗: 対象ファイルに{type_label} '{block_name}' が見つかりません。")
                    fail += 1
                
                i = s_end - 1
        i += 1
        
    return success, fail, result_lines


def _apply_block_replacement(patch_text: str, project_root: Path, target_file: Path | None) -> Tuple[int, int]:
    """
    パッチテキスト内の関数・クラス定義を抽出し、対象ファイルの同名ブロックを丸ごと置換するフォールバック処理。
    """
    success_count = 0
    fail_count = 0
    
    file_pattern = re.compile(r'(?:ファイルパス|File|ファイル):\s*`?([a-zA-Z0-9_/\.\-]+)`?', re.IGNORECASE)
    current_file = target_file
    lines = patch_text.splitlines()
    i = 0
    
    file_contents: Dict[Path, List[str]] = {}
    
    while i < len(lines):
        line = lines[i]
        
        if target_file is None:
            file_match = file_pattern.search(line)
            if file_match:
                rel_path = file_match.group(1)
                resolved_path = (project_root / rel_path).resolve()
                if resolved_path.exists():
                    current_file = resolved_path
                    if current_file not in file_contents:
                        file_contents[current_file] = current_file.read_text(encoding="utf-8").splitlines()
        
        # Markdownのコードブロックを検知
        if line.strip().startswith("```"):
            block_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block_lines.append(lines[i])
                i += 1
                
            if current_file and current_file.exists():
                if current_file not in file_contents:
                    file_contents[current_file] = current_file.read_text(encoding="utf-8").splitlines()
                    
                s, f, new_lines = _replace_blocks_in_lines(file_contents[current_file], block_lines, current_file, project_root)
                success_count += s
                fail_count += f
                file_contents[current_file] = new_lines
                
        i += 1

    # コードブロックが一つも処理されなかった場合、テキスト全体を対象に試行
    if success_count == 0 and fail_count == 0 and current_file and current_file.exists():
        if current_file not in file_contents:
            file_contents[current_file] = current_file.read_text(encoding="utf-8").splitlines()
        s, f, new_lines = _replace_blocks_in_lines(file_contents[current_file], lines, current_file, project_root)
        success_count += s
        fail_count += f
        file_contents[current_file] = new_lines

    # 変更を保存
    for path, content_lines in file_contents.items():
        original_content = path.read_text(encoding="utf-8")
        new_content = "\n".join(content_lines)
        if original_content.endswith("\n") and not new_content.endswith("\n"):
            new_content += "\n"
        path.write_text(new_content, encoding="utf-8")
        
    return success_count, fail_count


def apply_patch(patch_text: str, project_root: Path, target_file: Path | None = None) -> Tuple[int, int]:
    """
    パッチテキストを解析し、ファイルを書き換える。
    置換ブロック(<<<<)がない場合は、関数・クラス単位の自動置換へフォールバックする。
    戻り値: (成功した置換数, 失敗した置換数)
    """
    # 置換ブロックが存在しない場合は関数・クラス単位の置換へフォールバック
    if "<<<<" not in patch_text or "====" not in patch_text or ">>>>" not in patch_text:
        print("ℹ️ 置換ブロック(<<<<)が見つからないため、関数・クラス単位の自動置換を試みます...")
        return _apply_block_replacement(patch_text, project_root, target_file)

    success_count = 0
    fail_count = 0

    # ファイルパスを抽出する正規表現
    file_pattern = re.compile(r'(?:ファイルパス|File|ファイル):\s*`?([a-zA-Z0-9_/\.\-]+)`?', re.IGNORECASE)
    
    current_file: Path | None = target_file
    lines = patch_text.splitlines()
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # ターゲットが明示指定されていない場合のみ、テキストからファイルパスを探す
        if target_file is None:
            file_match = file_pattern.search(line)
            if file_match:
                rel_path = file_match.group(1)
                current_file = (project_root / rel_path).resolve()
                i += 1
                continue

        # 置換ブロックの開始を検知
        if line.strip() == "<<<<":
            if not current_file:
                print(f"⚠️ 警告: ファイルパスが指定されていないため、ブロックをスキップします (行: {i+1})")
                fail_count += 1
                while i < len(lines) and lines[i].strip() != ">>>>":
                    i += 1
                i += 1
                continue

            search_lines = []
            replace_lines = []
            
            # <<<< から ==== までを抽出
            i += 1
            while i < len(lines) and lines[i].strip() != "====":
                search_lines.append(lines[i])
                i += 1
                
            # ==== から >>>> までを抽出
            i += 1
            while i < len(lines) and lines[i].strip() != ">>>>":
                replace_lines.append(lines[i])
                i += 1

            if current_file.exists():
                content = current_file.read_text(encoding="utf-8")
                new_content, error_msg = _find_and_replace(content, search_lines, replace_lines)
                
                if new_content is not None:
                    current_file.write_text(new_content, encoding="utf-8")
                    print(f"✅ 適用成功: {current_file.relative_to(project_root)}")
                    success_count += 1
                else:
                    print(f"❌ 適用失敗: {current_file.relative_to(project_root)}")
                    print(f"  -> {error_msg}")
                    fail_count += 1
            else:
                print(f"❌ 適用失敗: ファイルが存在しません -> {current_file}")
                fail_count += 1
                
        i += 1

    return success_count, fail_count