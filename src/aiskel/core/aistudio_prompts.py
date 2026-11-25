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
