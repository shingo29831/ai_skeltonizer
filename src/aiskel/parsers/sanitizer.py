"""Module: @role: コメントやdocstringから装飾記号・履歴・ライセンス条項を排除し、責務説明文を正規化する。"""

import re
from typing import List, Optional

# なぜ必要か: 区切り線（===, ---）や記号行の誤検知を防止しサマリーとして抽出されるのを防ぐ
DECORATION_LINE_PATTERN = re.compile(r"^(?:[=\-#*~_/\\]{3,}|\s*[=\-]{3,}.*?[=\-]{3,}\s*)$")

SINGLE_WORD_SECTION_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+:?$")

# なぜ必要か: ライセンス条項や出所表記が役割説明として誤抽出されるのを防止
COPYRIGHT_PATTERN = re.compile(
    r"(?:copyright|\(c\)|all\s+rights?\s+reserved|licensed?\s+under|this\s+software\s+is\s+released\s+under|mit\s+license|apache\s+license|bsd\s+license)",
    re.IGNORECASE,
)

COPYRIGHT_KEYWORDS = [
    "copyright",
    "(c)",
    "all rights reserved",
    "licensed under",
    "this software is released under",
    "license",
    "cc by",
    "http://",
    "https://",
    "mostly copy-paste from",
    "copied from",
    "modified from",
    "taken from",
    "mit license",
    "apache license",
    "bsd license",
    "gnu",
    "author:",
    "authors:",
]

HISTORY_PATTERNS = [
    re.compile(r"^[【\[](?:修正|追加|変更|削除|更新)[】\]]"),
    re.compile(r"^---\s*ここまで追加"),
    re.compile(r"^Experimental", re.IGNORECASE),
    re.compile(r"^(?:TODO|FIXME|NOTE|XXX|BUG|HACK)(?:\([^)]*\))?\s*:", re.IGNORECASE),
]

SECTION_TRUNCATE_PATTERN = re.compile(
    r"^(?:Args|Arguments|Parameters|Params|Keyword\s+Args|Returns?|Yields?|Raises?|Usage|Examples?|Notes?|References?)\s*:",
    re.IGNORECASE,
)

ARG_TYPE_DEF_PATTERNS = [
    re.compile(r"^[A-Za-z0-9_]+\s*:\s*\[.*\]"),
    re.compile(r"^[A-Za-z0-9_]+\s*\([^)]*\)\s*:"),
]

# なぜ必要か: ファイル先頭のパスコメント行がモジュールの役割説明に誤混入するのを防止
FILEPATH_COMMENT_PATTERN = re.compile(r"^(?:file\s*:\s*)?[a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9]+$")


def clean_comment_line(raw_line: str) -> str:
    line = raw_line.strip()
    if line.startswith("//"):
        line = line[2:].strip()
    elif line.startswith("#"):
        line = line[1:].strip()
    elif line.startswith("/*"):
        line = line[2:].strip()
    elif line.startswith("*"):
        line = line[1:].strip()
    if line.endswith("*/"):
        line = line[:-2].strip()
    return line.strip()


def _is_decoration_line(line: str) -> bool:
    if not line:
        return False
    if re.match(r"^[=\-#*~_/\\]{3,}$", line):
        return True
    if re.match(r"^[=\-]{3,}.*?[=\-]{3,}", line):
        return True
    return bool(DECORATION_LINE_PATTERN.match(line))


def _is_single_word_section(line: str) -> bool:
    return bool(SINGLE_WORD_SECTION_PATTERN.match(line))


def _is_copyright_or_license(line: str) -> bool:
    if line.startswith("#!"):
        return True
    lower = line.lower()
    if COPYRIGHT_PATTERN.search(lower):
        return True
    return any(kw in lower for kw in COPYRIGHT_KEYWORDS)


def _is_history_or_memo(line: str) -> bool:
    return any(pattern.search(line) for pattern in HISTORY_PATTERNS)


def _is_section_truncate_trigger(line: str) -> bool:
    if SECTION_TRUNCATE_PATTERN.match(line):
        return True
    if line.startswith("<XML>") or line.startswith("```"):
        return True
    return False


def _is_type_def_line(line: str) -> bool:
    return any(pattern.match(line) for pattern in ARG_TYPE_DEF_PATTERNS)


def _is_filepath_comment(line: str) -> bool:
    return bool(FILEPATH_COMMENT_PATTERN.match(line.strip()))


def _is_trivial_docstring(line: str, entity_name: str) -> bool:
    if not entity_name:
        return False
    base_name = entity_name.split(".")[-1].split("::")[-1].strip()
    clean_line = line.strip(" `'\".()_").lower()
    clean_base = base_name.strip(" `'\".()_").lower()
    clean_entity = entity_name.strip(" `'\".()_").lower()

    if not clean_line:
        return True
    if clean_line in (clean_base, clean_entity):
        return True
    if clean_line in (f"def {clean_base}", f"function {clean_base}", f"class {clean_base}"):
        return True
    if clean_line in (f"role: {clean_base}", f"role:{clean_base}"):
        return True
    return False


def extract_summary_line(lines: List[str], entity_name: str = "") -> Optional[str]:
    for raw_line in lines:
        line = clean_comment_line(raw_line)
        if not line:
            continue

        if _is_section_truncate_trigger(line):
            break

        if _is_decoration_line(line) or _is_single_word_section(line):
            continue
        if _is_copyright_or_license(line):
            continue
        if _is_history_or_memo(line):
            continue
        if _is_type_def_line(line):
            continue
        if _is_filepath_comment(line):
            continue
        if _is_trivial_docstring(line, entity_name):
            continue

        # なぜ必要か: Module: @role: などの複合プレフィックスを完全に除去し二重ラベル化を防止
        m = re.match(r"^(?:(?:Module|Class|Function)\s*:?\s*)?(?:@role|Role|AI|Rule)\s*:?\s*", line, re.IGNORECASE)
        if m and m.end() > 0:
            rest = line[m.end():].strip()
            if not rest or _is_trivial_docstring(rest, entity_name):
                continue
            return rest

        return line

    return None


def extract_clean_role_description(
    docstring: Optional[str] = None,
    leading_comments: Optional[List[str]] = None,
    entity_name: str = "",
) -> str:
    summary: Optional[str] = None
    if docstring:
        doc_lines = docstring.strip().splitlines()
        summary = extract_summary_line(doc_lines, entity_name)

    explicit_role_comment: Optional[str] = None
    fallback_comment_summary: Optional[str] = None

    if leading_comments:
        for c in leading_comments:
            cleaned = clean_comment_line(c)
            if re.match(r"^(?:Role|AI|Rule)\s*:", cleaned, re.IGNORECASE):
                rest = cleaned.split(":", 1)[1].strip()
                if rest and not _is_trivial_docstring(rest, entity_name):
                    explicit_role_comment = rest
                    break
        if not summary:
            fallback_comment_summary = extract_summary_line(leading_comments, entity_name)

    # なぜ必要か: 多重に付与された @role や Module: プレフィックスを再帰的にストリップして純粋な説明文のみを抽出
    def _strip_role_prefix(text: str) -> str:
        return re.sub(
            r"^(?:(?:\*\*\[(?:Role|AI|Rule)\]\*\*|(?:Module|Class|Function)\s*:?\s*|@role\s*:?|Role\s*:?|AI\s*:?|Rule\s*:?)\s*)+",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    if explicit_role_comment:
        clean_explicit = _strip_role_prefix(explicit_role_comment)
        if summary:
            clean_summary = _strip_role_prefix(summary)
            if clean_summary and clean_summary != clean_explicit:
                return f"{clean_explicit} / {clean_summary}"
        return clean_explicit

    if summary:
        return _strip_role_prefix(summary)

    if fallback_comment_summary:
        return fallback_comment_summary

    return ""
