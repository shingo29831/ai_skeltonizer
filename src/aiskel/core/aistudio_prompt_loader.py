"""Module: @role: 組込・プロジェクト別プロンプト定義ファイルの読込、セクション解析、およびテンプレート置換を担当する。"""
import importlib.resources as importlib_resources
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_PROMPT_FILE_NAME = "prompts.txt"

_REQUIRED_BUILTIN_KEYS: Tuple[str, ...] = (
    "default",
    "patch_review",
    "review_context_rules",
    "review_patch_format_rules",
    "discovery",
    "file_split",
    "dir_organize",
    "config_infer",
)

_UNIFIED_PROMPT_FILENAMES: Tuple[str, ...] = (
    "PROMPTS.txt",
    "PROMPTS.md",
    "ai_meta/PROMPTS.txt",
    "ai_meta/PROMPTS.md",
    ".ai_meta/PROMPTS.txt",
    ".ai_meta/PROMPTS.md",
)

_DEFAULT_PROMPT_FILENAMES: Tuple[str, ...] = (
    "PROMPT.txt",
    "ai_meta/PROMPT.txt",
    ".ai_meta/PROMPT.txt",
    "ai_meta/prompts/default.txt",
    ".ai_meta/prompts/default.txt",
)

_REVIEW_PROMPT_FILENAMES: Tuple[str, ...] = (
    "REVIEW_PROMPT.txt",
    "ai_meta/REVIEW_PROMPT.txt",
    ".ai_meta/REVIEW_PROMPT.txt",
    "ai_meta/prompts/patch_review.txt",
    ".ai_meta/prompts/patch_review.txt",
)

_DISCOVERY_PROMPT_FILENAMES: Tuple[str, ...] = (
    "DISCOVERY_PROMPT.txt",
    "ai_meta/DISCOVERY_PROMPT.txt",
    ".ai_meta/DISCOVERY_PROMPT.txt",
    "ai_meta/prompts/discovery.txt",
    ".ai_meta/prompts/discovery.txt",
)

_PROMPT_SECTION_HEADER_RE = re.compile(
    r"^[ \t]*===[ \t]*\[PROMPT:[ \t]*([a-zA-Z0-9_]+)[ \t]*\][^\n]*===[ \t]*$"
)

_TOKEN_UNESCAPE_MAP: Tuple[Tuple[str, str], ...] = (
    ("{{<<<<}}", "<" * 4),
    ("{{====}}", "=" * 4),
    ("{{>>>>}}", ">" * 4),
    ("{{FILE_HEADER}}", "ファイル" + "パス:"),
    ("{{DELETE_HEADER}}", "削" + "除:"),
    ("{{MOVE_HEADER}}", "移" + "動:"),
    ("{{COMMIT_OPEN}}", "<" + "commit>"),
    ("{{COMMIT_CLOSE}}", "</" + "commit>"),
)

def _decode_prompt_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n").strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").strip()

def _read_first_non_empty_file(candidates: Tuple[Path, ...]) -> Optional[str]:
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        text = _decode_prompt_bytes(raw)
        if text:
            return text
    return None

def _unescape_prompt_tokens(text: str) -> str:
    resolved = text
    for token, literal in _TOKEN_UNESCAPE_MAP:
        if token in resolved:
            resolved = resolved.replace(token, literal)
    return resolved

def _render_template(template: str, replacements: Dict[str, str]) -> str:
    if not replacements:
        return template
    # 置換後の値に含まれる別プレースホルダへの再代入を防ぐため1パスで置換する
    pattern = re.compile("|".join(re.escape(key) for key in replacements))
    return pattern.sub(lambda match: replacements[match.group(0)], template)

def _parse_prompt_sections(raw_text: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_key: Optional[str] = None
    buffer: List[str] = []

    for line in raw_text.splitlines():
        header_match = _PROMPT_SECTION_HEADER_RE.match(line)
        if header_match:
            if current_key is not None:
                body = _unescape_prompt_tokens("\n".join(buffer).strip())
                if body:
                    sections[current_key] = body
            current_key = header_match.group(1).strip().lower()
            buffer = []
            continue
        if current_key is not None:
            buffer.append(line)

    if current_key is not None:
        body = _unescape_prompt_tokens("\n".join(buffer).strip())
        if body:
            sections[current_key] = body
    return sections

def _read_prompt_file_bytes(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = _decode_prompt_bytes(raw)
    return text if text else None

def _read_builtin_prompt_file_text() -> str:
    try:
        resource = importlib_resources.files("aiskel").joinpath(_PROMPT_FILE_NAME)
        raw = resource.read_bytes()
        text = _decode_prompt_bytes(raw)
        if text:
            return text
    except (ModuleNotFoundError, FileNotFoundError, OSError, TypeError, AttributeError):
        pass

    fallback_path = Path(__file__).resolve().parents[1] / _PROMPT_FILE_NAME
    fallback_text = _read_prompt_file_bytes(fallback_path)
    if fallback_text:
        return fallback_text
    raise FileNotFoundError(f"組込プロンプト定義ファイルが見つかりません: {_PROMPT_FILE_NAME}")

def _load_builtin_sections() -> Dict[str, str]:
    raw_text = _read_builtin_prompt_file_text()
    sections = _parse_prompt_sections(raw_text)
    missing = [key for key in _REQUIRED_BUILTIN_KEYS if not sections.get(key)]
    if missing:
        raise RuntimeError(f"組込プロンプト定義ファイルに必須セクションが不足しています: {', '.join(missing)}")
    return sections

def _load_project_sections(project_root: Optional[Path]) -> Dict[str, str]:
    if project_root is None:
        return {}
    resolved = project_root.resolve()
    merged: Dict[str, str] = {}
    for rel_path in _UNIFIED_PROMPT_FILENAMES + _DEFAULT_PROMPT_FILENAMES:
        raw_text = _read_prompt_file_bytes(resolved / rel_path)
        if not raw_text:
            continue
        sections = _parse_prompt_sections(raw_text)
        if sections:
            for key, val in sections.items():
                merged.setdefault(key, val)
    return merged

def _read_first_plain_or_section(
    project_root: Path,
    rel_paths: Tuple[str, ...],
    section_key: str,
) -> Optional[str]:
    resolved = project_root.resolve()
    for rel_path in rel_paths:
        raw_text = _read_prompt_file_bytes(resolved / rel_path)
        if not raw_text:
            continue
        sections = _parse_prompt_sections(raw_text)
        if sections:
            value = sections.get(section_key)
            if value:
                return value
            continue
        return _unescape_prompt_tokens(raw_text)
    return None

def get_meta_prompt_relative_paths() -> Tuple[str, ...]:
    """ai_meta/.ai_meta 配下に配置され得るプロンプト定義ファイルの相対パス一覧を返す。"""
    rel_paths: List[str] = []
    seen: Set[str] = set()
    for raw_path in _UNIFIED_PROMPT_FILENAMES + _DEFAULT_PROMPT_FILENAMES + _REVIEW_PROMPT_FILENAMES + _DISCOVERY_PROMPT_FILENAMES:
        rel: Optional[str] = None
        for prefix in ("ai_meta/", ".ai_meta/"):
            if raw_path.startswith(prefix):
                rel = raw_path[len(prefix):]
                break
        if rel and rel not in seen:
            seen.add(rel)
            rel_paths.append(rel)
    return tuple(rel_paths)
