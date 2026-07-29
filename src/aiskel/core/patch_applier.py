import re
from pathlib import Path
from typing import List, Tuple

def apply_patch(patch_text: str, project_root: Path, target_file: Path | None = None) -> Tuple[int, int]:
    """
    パッチテキストを解析し、<<<< ==== >>>> ブロックに従ってファイルを書き換える。
    戻り値: (成功した置換数, 失敗した置換数)
    """
    success_count = 0
    fail_count = 0

    # ファイルパスを抽出する正規表現 (例: "ファイルパス: src/main.py" または "File: src/main.py")
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

            search_text = "\n".join(search_lines) + "\n"
            replace_text = "\n".join(replace_lines) + "\n"

            if current_file.exists():
                content = current_file.read_text(encoding="utf-8")
                if search_text in content:
                    new_content = content.replace(search_text, replace_text)
                    current_file.write_text(new_content, encoding="utf-8")
                    print(f"✅ 適用成功: {current_file.relative_to(project_root)}")
                    success_count += 1
                else:
                    print(f"❌ 適用失敗: {current_file.relative_to(project_root)}")
                    print("  -> 検索テキストがファイル内に見つかりませんでした。AIの出力が古いか、インデントがずれている可能性があります。")
                    fail_count += 1
            else:
                print(f"❌ 適用失敗: ファイルが存在しません -> {current_file}")
                fail_count += 1
                
        i += 1

    return success_count, fail_count