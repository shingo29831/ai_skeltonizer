"""Module: @role: ソースコードから特定ノード（関数・クラス）の構文単位での抽出およびMarkdownスニペット整形を担当する。"""
import difflib
from pathlib import Path
from typing import List, Tuple, Optional

from .patch_applier import _find_block_range, _extract_blocks
from .token_counter import estimate_tokens

LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "c",
    ".h": "c",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".sh": "bash",
    ".sql": "sql",
}

def detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    return LANGUAGE_EXTENSIONS.get(ext, "")

def parse_target_spec(spec: str) -> Tuple[str, List[str]]:
    cleaned_spec = spec.strip()
    if not cleaned_spec:
        raise ValueError("対象の指定が空です。")

    start_search_idx = 0
    if len(cleaned_spec) >= 2 and cleaned_spec[1] == ":" and cleaned_spec[0].isalpha():
        start_search_idx = 2

    colon_idx = cleaned_spec.find(":", start_search_idx)
    if colon_idx == -1:
        return cleaned_spec, []

    path_part = cleaned_spec[:colon_idx].strip()
    nodes_part = cleaned_spec[colon_idx + 1:].strip()
    nodes = [n.strip() for n in nodes_part.split(",") if n.strip()]
    return path_part, nodes

def extract_node_code(lines: List[str], node_name: str) -> Optional[List[str]]:
    for block_type in ("class", "def", "function"):
        start_idx, end_idx = _find_block_range(lines, node_name, block_type)
        if start_idx != -1 and end_idx != -1:
            return lines[start_idx:end_idx]

    # なぜ必要か: AIが要約に従って「ClassName.method_name」形式で指定した場合にクラス内から抽出しKeyErrorを防止
    if "." in node_name:
        class_name, method_name = node_name.split(".", 1)
        c_start, c_end = _find_block_range(lines, class_name, "class")
        if c_start != -1 and c_end != -1:
            class_lines = lines[c_start:c_end]
            for block_type in ("def", "function"):
                m_start, m_end = _find_block_range(class_lines, method_name, block_type)
                if m_start != -1 and m_end != -1:
                    return class_lines[m_start:m_end]
        # なぜ必要か: クラス名が不一致でも同名メソッドが存在すればフォールバック取得して作業中断を防止
        for block_type in ("def", "function"):
            start_idx, end_idx = _find_block_range(lines, method_name, block_type)
            if start_idx != -1 and end_idx != -1:
                return lines[start_idx:end_idx]

    return None
def extract_and_format_snippets(
    specs: List[str],
    project_root: Path,
    max_chars: int = 100_000
) -> Tuple[str, int, int]:
    if not specs:
        raise ValueError("コピー対象のファイルまたは要素が指定されていません。")

    snippet_blocks: List[str] = []
    total_items = 0

    for spec in specs:
        path_str, nodes = parse_target_spec(spec)
        raw_path = Path(path_str)
        target_path = raw_path if raw_path.is_absolute() else (project_root / raw_path).resolve()

        if not target_path.exists() or not target_path.is_file():
            raise FileNotFoundError(f"ファイルが見つかりません: {target_path}")

        try:
            rel_display_path = target_path.relative_to(project_root).as_posix()
        except ValueError:
            rel_display_path = target_path.as_posix()

        lang = detect_language(target_path)
        content = target_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        # ワイルドカード指定 (*): 定義されている関数・クラスの一覧リストを出力
        if nodes == ["*"]:
            blocks = _extract_blocks(lines)
            if not blocks:
                snippet_blocks.append(f"ファイルパス: {rel_display_path} (関数・クラス一覧: 0件)\n(定義されている関数・クラスはありません)")
            else:
                items_text = []
                for b_type, b_name, s_start, s_end in blocks:
                    type_name = "class" if b_type == "class" else "function"
                    items_text.append(f"  - [{type_name}] {b_name} (L{s_start + 1}-L{s_end})")
                summary_list = "\n".join(items_text)
                header = f"ファイルパス: {rel_display_path} (定義ノード一覧: {len(blocks)}件)"
                snippet_blocks.append(f"{header}\n```text\n{summary_list}\n```")
            total_items += len(blocks) if blocks else 1
            continue

        if not nodes:
            total_items += 1
            header = f"ファイルパス: {rel_display_path}"
            code_block = f"```{lang}\n{content.strip()}\n```" if content.strip() else f"```{lang}\n```"
            snippet_blocks.append(f"{header}\n{code_block}")
        else:
            extracted_parts: List[str] = []
            for node_name in nodes:
                node_lines = extract_node_code(lines, node_name)
                # タイポ時のサジェスト生成: 類似した関数・クラス名を提案して開発者の再入力コストを削減
                if node_lines is None:
                    available_blocks = _extract_blocks(lines)
                    available_names = [b[1] for b in available_blocks]
                    close_matches = difflib.get_close_matches(node_name, available_names, n=3, cutoff=0.5)
                    if close_matches:
                        suggestion = f"もしかして: {', '.join(repr(m) for m in close_matches)} ですか？"
                    elif available_names:
                        suggestion = f"(利用可能なノード: {', '.join(available_names)})"
                    else:
                        suggestion = "(定義されている関数・クラスはありません)"
                    raise KeyError(f"ファイル '{rel_display_path}' 内に関数またはクラス '{node_name}' が見つかりませんでした。{suggestion}")
                extracted_parts.append("\n".join(node_lines).rstrip())
                total_items += 1

            nodes_label = ", ".join(nodes)
            header = f"ファイルパス: {rel_display_path} ({nodes_label})"
            merged_code = "\n\n".join(extracted_parts)
            code_block = f"```{lang}\n{merged_code}\n```"
            snippet_blocks.append(f"{header}\n{code_block}")

    formatted_result = "\n\n".join(snippet_blocks)
    char_count = len(formatted_result)

    if char_count > max_chars:
        raise ValueError(
            f"抽出されたコードの総文字数 ({char_count:,} 文字) が上限 ({max_chars:,} 文字) を超えています。"
            "--max-chars オプションで上限を引き上げるか、対象を絞り込んでください。"
        )

    estimated_tokens = estimate_tokens(formatted_result)
    return formatted_result, estimated_tokens, total_items
