# src/aiskel/core/patch_applier.py
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

def _extract_blocks(lines: List[str]) -> List[Tuple[str, str, int, int]]:
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = BLOCK_PATTERN.match(line)
        if match:
            b_type = match.group(1) or 'function'
            b_name = match.group(2) or match.group(3)
            s_start, s_end = _find_block_range(lines[i:], b_name, b_type)
            if s_start != -1:
                s_start += i
                s_end += i
                blocks.append((b_type, b_name, s_start, s_end))
                i = max(s_end - 1, i)
        i += 1
    return blocks

def _find_and_replace(content: str, search_lines: List[str], replace_lines: List[str], force_replace: bool = False) -> Tuple[Optional[str], str, bool]:
    """
    完全一致、または柔軟なマッチングで置換を行う
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
    
    # 1. 改行コードを統一して完全一致検索
    content_normalized = content.replace("\r\n", "\n")
    search_text = "\n".join(core_search)
    replace_text = "\n".join(replace_lines)
    
    if not force_replace and replace_text.strip() and replace_text in content_normalized:
        return content_normalized, "", True

    if search_text in content_normalized:
        return content_normalized.replace(search_text, replace_text), "", False

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
            return result, "", False

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
                return result, "", False

    # 4. 最初と最後の行によるブロックマッチング
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
                return result, "", False

    # 5. 行類似度マッチング (Fuzzy Matching): AIの微小なコメント差分や末尾カンマ表記揺れによる置換失敗を防止
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
            new_lines = content_lines[:best_start_idx] + replace_lines + content_lines[best_end_idx + 1:]
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

def _find_block_range(lines: List[str], block_name: str, block_type: str) -> Tuple[int, int]:
    """
    行リストから指定された関数またはクラスの定義範囲（開始行、終了行）を返す。
    """
    if block_type in ('def', 'function'):
        pattern = re.compile(
            r'^([ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:'
            r'(?:def|function)\s+' + re.escape(block_name) + r'\s*[\(\{]|'
            r'(?:const|let|var)\s+' + re.escape(block_name) + r'\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>'
            r')'
        )
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
        
    actual_start_idx = start_idx
    while actual_start_idx > 0:
        prev_line = lines[actual_start_idx - 1]
        prev_stripped = prev_line.strip()
        if not prev_stripped:
            break
        prev_indent = len(prev_line) - len(prev_line.lstrip())
        if prev_indent == base_indent and prev_stripped.startswith('@'):
            actual_start_idx -= 1
        else:
            break
    start_idx = actual_start_idx

    has_brace = False
    brace_depth = 0
    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        open_c = line.count('{')
        close_c = line.count('}')
        if open_c > 0:
            has_brace = True
        brace_depth += (open_c - close_c)
        if has_brace and brace_depth <= 0:
            return start_idx, idx + 1

    end_idx = start_idx + 1
    while end_idx < len(lines):
        line = lines[end_idx]
        if line.strip():
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                if line.strip().startswith('}'):
                    end_idx += 1
                break
        end_idx += 1
        
    return start_idx, end_idx

def _replace_blocks_in_lines(target_lines: List[str], source_lines: List[str], file_path: Path, project_root: Path) -> Tuple[int, int, List[str]]:
    success = 0
    fail = 0
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

        if t_start != -1:
            result_lines = result_lines[:t_start] + new_block_lines + result_lines[t_end:]
            print(f"✔ {type_label}置換成功: {block_name} ({disp_path})")
            success += 1
        else:
            print(f"✖ {type_label}置換失敗: 対象ファイルに{type_label} '{block_name}' が見つかりません。")
            fail += 1
        
    return success, fail, result_lines

def generate_diff_display(file_path: Path, original: str, updated: str, project_root: Path) -> str:
    """変更前後の文字列からカラー付き Unified Diff を生成する"""
    try:
        rel_path = file_path.relative_to(project_root).as_posix()
    except ValueError:
        rel_path = file_path.as_posix()
    
    orig_lines = original.splitlines(keepends=True)
    upd_lines = updated.splitlines(keepends=True)
    diff = list(difflib.unified_diff(orig_lines, upd_lines, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
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
    
    file_pattern = re.compile(r'(?:ファイルパス|File|ファイル):\s*`?([a-zA-Z0-9_/\.\-:\\]+)`?', re.IGNORECASE)
    current_file = target_file
    lines = patch_text.splitlines()
    i = 0
    
    file_contents: Dict[Path, List[str]] = {}
    original_contents: Dict[Path, str] = {}
    
    while i < len(lines):
        line = lines[i]
        file_match = file_pattern.search(line)
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
                    s, f, new_lines = _replace_blocks_in_lines(file_contents[current_file], block_lines, current_file, project_root)
                    success_count += s
                    fail_count += f
                    if s > 0:
                        modified_files.add(current_file)
                    file_contents[current_file] = new_lines
        i += 1

    # トランザクション・Dry-run判定: 失敗時はディスク書き込みを破棄
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
    dry_run が True の場合はディスクへの書き込みを行わず Unified Diff を表示する。
    """
    if "<<<<" not in patch_text or "====" not in patch_text or ">>>>" not in patch_text:
        if revert:
            print("✖ エラー: リバート処理には置換ブロック(<<<< ==== >>>>)が必須です。")
            return 0, 1, 0, set()
        print("ℹ 置換ブロック(<<<<)が見つからないため、関数・クラス単位の自動置換を試みます...")
        return _apply_block_replacement(patch_text, project_root, target_file, dry_run=dry_run)

    success_count = 0
    fail_count = 0
    skipped_count = 0
    modified_files: Set[Path] = set()

    # 部分置換エラー時のプロジェクト破損防止のため、全変更をインメモリ仮想バッファでシミュレーションする
    file_contents: Dict[Path, str] = {}
    original_contents: Dict[Path, str] = {}

    file_pattern = re.compile(r'(?:ファイルパス|File|ファイル):\s*`?([a-zA-Z0-9_/\.\-:\\]+)`?', re.IGNORECASE)
    current_file: Path | None = target_file
    lines = patch_text.splitlines()
    i = 0
    
    while i < len(lines):
        line = lines[i]
        file_match = file_pattern.search(line)
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
            content = file_contents[current_file]

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

                    is_partial = False
                    if blocks:
                        if existing_has_imports and not replace_has_imports:
                            is_partial = True
                        elif not replace_has_imports:
                            existing_blocks = _extract_blocks(content_lines)
                            if len(blocks) < len(existing_blocks):
                                is_partial = True

                    if is_partial:
                        if len(blocks) == 1 and len(replace_lines) <= (blocks[0][3] - blocks[0][2] + 5):
                            b_type, b_name, _, _ = blocks[0]
                            t_start, t_end = _find_block_range(content_lines, b_name, b_type)
                            if t_start != -1:
                                target_block_text = "\n".join(content_lines[t_start:t_end]).strip()
                                if not force_replace and target_block_text == replace_text:
                                    print(f"⏭ 置換スキップ (適用済み): {disp_path} ({b_name})")
                                    skipped_count += 1
                                else:
                                    new_lines = content_lines[:t_start] + replace_lines + content_lines[t_end:]
                                    new_content = "\n".join(new_lines)
                                    if not new_content.endswith("\n"):
                                        new_content += "\n"
                                    file_contents[current_file] = new_content
                                    type_label = "クラス" if b_type == "class" else "関数"
                                    print(f"✔ {type_label}部分置換成功: {b_name} ({disp_path})")
                                    success_count += 1
                                    modified_files.add(current_file)
                            else:
                                type_label = "クラス" if b_type == "class" else "関数"
                                print(f"✖ 置換失敗: 対象ファイルに{type_label} '{b_name}' が見つかりません ({disp_path})")
                                fail_count += 1
                        else:
                            s, f, new_lines = _replace_blocks_in_lines(content_lines, replace_lines, current_file, project_root)
                            if s > 0:
                                new_content = "\n".join(new_lines)
                                if not new_content.endswith("\n"):
                                    new_content += "\n"
                                file_contents[current_file] = new_content
                                success_count += s
                                fail_count += f
                                modified_files.add(current_file)
                            else:
                                fail_count += f
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
                    new_content, error_msg, is_skipped = _find_and_replace(content, search_lines, replace_lines, force_replace)
                    if is_skipped:
                        action_label = "リバート" if revert else "置換"
                        print(f"⏭ {action_label}スキップ (適用済み): {disp_path}")
                        skipped_count += 1
                    elif new_content is not None:
                        file_contents[current_file] = new_content
                        action_label = "リバート" if revert else "適用"
                        print(f"✔ {action_label}成功: {disp_path}")
                        success_count += 1
                        modified_files.add(current_file)
                    else:
                        action_label = "リバート" if revert else "適用"
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

    # トランザクション判定: 1箇所でも失敗した場合はディスクへの書き込みを一切行わず中止
    if fail_count > 0:
        print(f"\n❌ 置換エラーが発生したため、トランザクションを中断しました。ディスクへの書き込みは一切行われていません（All-or-Nothing）。")
        return success_count, fail_count, skipped_count, set()

    # Dry-runプレビュー出力
    if dry_run:
        print("\n🔍 [DRY-RUN] 差分プレビュー（ディスクは変更されません）:")
        for path in modified_files:
            orig = original_contents.get(path, "")
            diff_text = generate_diff_display(path, orig, file_contents[path], project_root)
            if diff_text:
                print(diff_text)
        return success_count, fail_count, skipped_count, modified_files

    # トランザクション確定: 全ての変更をディスクに一括反映
    for path in modified_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_contents[path], encoding="utf-8")

    return success_count, fail_count, skipped_count, modified_files
