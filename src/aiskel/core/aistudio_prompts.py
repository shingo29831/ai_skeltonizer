"""Module: @role: AI Studio自動貼り付け用の複数パターン別プロンプト定義・読込・組み立てを担当する。"""
from pathlib import Path
from typing import Optional, Tuple, Union

from .aistudio_prompt_loader import (
    _DEFAULT_PROMPT_FILENAMES,
    _PROMPT_FILE_NAME,
    _PROMPT_SECTION_HEADER_RE,
    _REQUIRED_BUILTIN_KEYS,
    _REVIEW_PROMPT_FILENAMES,
    _TOKEN_UNESCAPE_MAP,
    _UNIFIED_PROMPT_FILENAMES,
    _decode_prompt_bytes,
    _load_builtin_sections,
    _load_project_sections,
    _parse_prompt_sections,
    _read_builtin_prompt_file_text,
    _read_first_non_empty_file,
    _read_first_plain_or_section,
    _read_prompt_file_bytes,
    _render_template,
    _unescape_prompt_tokens,
)
from .aistudio_prompt_rules import (
    PATCH_HANDOFF_STOP_SIGNAL,
    PromptPattern,
    _BUILTIN_REVIEW_BASE_RULES,
    _COMMIT_CLOSE,
    _COMMIT_OPEN,
    _CONTEXT_RULES_SECTION_RE,
    _CORE_ENGINEERING_RULES_HEAD_COMMON,
    _CORE_ENGINEERING_RULES_TAIL,
    _DEFAULT_CONTEXT_RULES,
    _DEFAULT_OUTPUT_RULES,
    _D_END,
    _D_MID,
    _D_START,
    _OUTPUT_FLOW_SECTION_RE,
    _REVIEW_CONTEXT_RULES,
    _REVIEW_PATCH_FORMAT_RULES,
    _replace_output_flow_section_for_review,
    _replace_sections_for_review,
)

BUILTIN_DEFAULT_PROMPT: str = _load_builtin_sections()["default"]


def load_default_prompt(project_root: Path) -> str:
    """対象プロジェクトの設定ファイルまたは外部プロンプト定義ファイルからデフォルトプロンプトを読み込む。"""
    resolved = project_root.resolve()
    custom = _read_first_plain_or_section(
        resolved,
        _DEFAULT_PROMPT_FILENAMES + _UNIFIED_PROMPT_FILENAMES,
        PromptPattern.DEFAULT.value,
    )
    if custom is not None:
        return custom
    return _load_builtin_sections()["default"]

def build_patch_review_prompt(
    default_prompt: str,
    project_root: Optional[Path] = None,
    review_template_override: Optional[str] = None,
) -> str:
    """デフォルトプロンプトの規約をベースに新規実装内容の欠陥指摘を依頼するレビュー専用プロンプトを組み立てる。"""
    builtin_sections = _load_builtin_sections()
    project_sections = _load_project_sections(project_root)

    review_template = (
        review_template_override
        or project_sections.get("patch_review")
        or builtin_sections["patch_review"]
    )
    review_context_rules = (
        project_sections.get("review_context_rules") or builtin_sections["review_context_rules"]
    )
    review_patch_format_rules = (
        project_sections.get("review_patch_format_rules") or builtin_sections["review_patch_format_rules"]
    )

    stripped = (default_prompt or "").strip()
    builtin_default = builtin_sections["default"].strip()
    is_builtin = (
        (not stripped)
        or (stripped == builtin_default)
        or (stripped == BUILTIN_DEFAULT_PROMPT.strip())
    )

    if is_builtin:
        base_rules, _ = _replace_sections_for_review(
            builtin_default,
            review_context_rules,
            review_patch_format_rules,
            replace_context=True,
        )
        base_section = f"=== 基本規約（設計・実装・パッチ準拠ルール） ===\n{base_rules}"
    else:
        sanitized, replaced = _replace_sections_for_review(
            stripped,
            review_context_rules,
            review_patch_format_rules,
            replace_context=False,
        )
        header = (
            "=== 基本規約（プロジェクト固有ルール：出力手順は上記「検証手順と出力方針」を優先） ==="
            if replaced
            else "=== 基本規約（プロジェクト固有ルール：出力省略制約・出力例より上記「検証手順と出力方針」とパッチ書式を優先） ==="
        )
        base_rules = sanitized
        base_section = f"{header}\n{sanitized}"

    return _render_template(
        review_template,
        {
            "{{STOP_SIGNAL}}": PATCH_HANDOFF_STOP_SIGNAL,
            "{{BASE_SECTION}}": base_section,
            "{{BASE_RULES}}": base_rules,
        },
    )

def list_prompt_patterns() -> Tuple[str, ...]:
    """利用可能なプロンプトパターン名一覧を返す。"""
    return tuple(p.value for p in PromptPattern)


def load_prompt_for_pattern(project_root: Path, pattern: Union[PromptPattern, str]) -> str:
    """指定されたパターンに対応する専用プロンプト文字列を返す。"""
    raw_value = pattern.value if isinstance(pattern, PromptPattern) else str(pattern or "").strip().lower()
    if not raw_value or raw_value == PromptPattern.DEFAULT.value:
        return load_default_prompt(project_root)

    resolved = project_root.resolve()
    if raw_value == PromptPattern.PATCH_REVIEW.value:
        custom_review = _read_first_plain_or_section(
            resolved,
            _REVIEW_PROMPT_FILENAMES,
            PromptPattern.PATCH_REVIEW.value,
        )
        if custom_review is not None:
            if "{{BASE_SECTION}}" in custom_review or "{{BASE_RULES}}" in custom_review:
                return build_patch_review_prompt(
                    load_default_prompt(resolved),
                    project_root=resolved,
                    review_template_override=custom_review,
                )
            return _render_template(
                custom_review,
                {"{{STOP_SIGNAL}}": PATCH_HANDOFF_STOP_SIGNAL},
            )
        project_sections = _load_project_sections(resolved)
        unified_review = project_sections.get(PromptPattern.PATCH_REVIEW.value)
        if unified_review is not None:
            if "{{BASE_SECTION}}" in unified_review or "{{BASE_RULES}}" in unified_review:
                return build_patch_review_prompt(
                    load_default_prompt(resolved),
                    project_root=resolved,
                    review_template_override=unified_review,
                )
            return _render_template(
                unified_review,
                {"{{STOP_SIGNAL}}": PATCH_HANDOFF_STOP_SIGNAL},
            )
        return build_patch_review_prompt(load_default_prompt(resolved), project_root=resolved)

    raise ValueError(f"未対応のプロンプトパターンです: {pattern}")

def build_file_split_prompt(
    default_prompt: str,
    target_rel_path: str,
    lines: int,
    chars: int,
    max_lines: int,
    max_chars: int,
    file_index: int = 1,
    total_files: int = 1,
    project_root: Optional[Path] = None,
) -> str:
    """肥大化した単一ファイルを責務ごとに分割するための専用プロンプトを組み立てる。"""
    builtin_sections = _load_builtin_sections()
    project_sections = _load_project_sections(project_root)
    split_template = project_sections.get("file_split") or builtin_sections["file_split"]

    stripped = (default_prompt or "").strip()
    base_rules = stripped if stripped else builtin_sections["default"]
    return _render_template(
        split_template,
        {
            "{{TARGET_REL_PATH}}": target_rel_path,
            "{{LINES}}": str(lines),
            "{{CHARS}}": f"{chars:,}",
            "{{MAX_LINES}}": str(max_lines),
            "{{MAX_CHARS}}": f"{max_chars:,}",
            "{{FILE_INDEX}}": str(file_index),
            "{{TOTAL_FILES}}": str(total_files),
            "{{BASE_RULES}}": base_rules,
        },
    )
_PROMPT_FILE_NAME = "prompts.txt"


_REQUIRED_BUILTIN_KEYS = (
    "default",
    "patch_review",
    "review_context_rules",
    "review_patch_format_rules",
    "file_split",
)


_UNIFIED_PROMPT_FILENAMES = (
    "PROMPTS.txt",
    "PROMPTS.md",
    "ai_meta/PROMPTS.txt",
    "ai_meta/PROMPTS.md",
    ".ai_meta/PROMPTS.txt",
    ".ai_meta/PROMPTS.md",
)


_DEFAULT_PROMPT_FILENAMES = (
    "PROMPT.txt",
    "ai_meta/PROMPT.txt",
    ".ai_meta/PROMPT.txt",
    "ai_meta/prompts/default.txt",
    ".ai_meta/prompts/default.txt",
)


_REVIEW_PROMPT_FILENAMES = (
    "REVIEW_PROMPT.txt",
    "ai_meta/REVIEW_PROMPT.txt",
    ".ai_meta/REVIEW_PROMPT.txt",
    "ai_meta/prompts/patch_review.txt",
    ".ai_meta/prompts/patch_review.txt",
)


_PROMPT_SECTION_HEADER_RE = re.compile(
    r"^[ \t]*===[ \t]*\[PROMPT:[ \t]*([a-zA-Z0-9_]+)[ \t]*\][^\n]*===[ \t]*$"
)


_CONTEXT_RULES_SECTION_RE = re.compile(
    r"^(?P<indent>[ \t]*)1\.[ \t]*コンテキスト解釈とトークン最小化規則[^\n]*\n[\s\S]*?(?=^(?P=indent)2\.[ \t]|\Z)",
    re.MULTILINE,
)


_TOKEN_UNESCAPE_MAP: Tuple[Tuple[str, str], ...] = (
    ("{{<<<<}}", "<" * 4),
    ("{{====}}", "=" * 4),
    ("{{>>>>}}", ">" * 4),
    ("{{FILE_HEADER}}", "ファイル" + "パス:"),
    ("{{DELETE_HEADER}}", "削" + "除:"),
    ("{{COMMIT_OPEN}}", "<" + "commit>"),
    ("{{COMMIT_CLOSE}}", "</" + "commit>"),
)


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


def _replace_sections_for_review(
    prompt_text: str,
    review_context_rules: str,
    review_patch_format_rules: str,
    replace_context: bool,
) -> Tuple[str, bool]:
    updated = prompt_text.strip()
    if replace_context and _CONTEXT_RULES_SECTION_RE.search(updated):
        updated = _CONTEXT_RULES_SECTION_RE.sub(
            lambda _m: f"{review_context_rules.strip()}\n\n",
            updated,
            count=1,
        )

    if _OUTPUT_FLOW_SECTION_RE.search(updated):
        updated = _OUTPUT_FLOW_SECTION_RE.sub(
            lambda _m: f"{review_patch_format_rules.strip()}\n\n",
            updated,
            count=1,
        )
        return updated.strip(), True
    return updated.strip(), False
