from pathlib import Path
from typing import List, Tuple, Optional

from .patch_applier import _find_block_range
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
    """ファイル拡張子からMarkdownコードブロック用言語識別子を特定する"""
    ext = path.suffix.lower()
    return LANGUAGE_EXTENSIONS.get(ext, "")

def parse_target_spec(spec: str) -> Tuple[str, List[str]]:
    """
    指定文字列を (ファイルパス文字列, ノード名リスト) に分解する。
    Windowsのドライブレター (例: C:\\path\\to\\file.py:func) を安全に扱う。
    """
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
    """
    指定された行リストから指定された関数またはクラスのコード行を抽出する。
    """
    for block_type in ("class", "def", "function"):
        start_idx, end_idx = _find_block_range(lines, node_name, block_type)
        if start_idx != -1 and end_idx != -1:
            return lines[start_idx:end_idx]
    return None

def extract_and_format_snippets(
    specs: List[str],
    project_root: Path,
    max_chars: int = 100_000
) -> Tuple[str, int, int]:
    """
    指定された複数ターゲットのコードを抽出し、AI入力用に整形して返す。
    
    戻り値: (整形済みテキスト, トークン推定値, 抽出したノード/ファイル総数)
    文字数が max_chars を超える場合は ValueError を送出する。
    """
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

        if not nodes:
            total_items += 1
            header = f"ファイルパス: {rel_display_path}"
            code_block = f"```{lang}\n{content.strip()}\n```" if content.strip() else f"```{lang}\n```"
            snippet_blocks.append(f"{header}\n{code_block}")
        else:
            extracted_parts: List[str] = []
            for node_name in nodes:
                node_lines = extract_node_code(lines, node_name)
                if node_lines is None:
                    raise KeyError(f"ファイル '{rel_display_path}' 内に関数またはクラス '{node_name}' が見つかりませんでした。")
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
            "クリップボードおよびエディタのフリーズを防ぐため処理を中断しました。"
            "対象を絞り込むか、--max-chars オプションで上限を引き上げてください。"
        )

    estimated_tokens = estimate_tokens(formatted_result)
    return formatted_result, estimated_tokens, total_items
