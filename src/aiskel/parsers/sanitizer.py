"""
Role: コメントやdocstringから装飾線、著作権、履歴、引数定義などのノイズを除去し、
      意味のある自然文サマリー（役割説明）を抽出・サニタイズする共通モジュール。
"""
import re
from typing import List, Optional

# 装飾線パターン（3個以上連続する記号、前後の装飾線など）
DECORATION_LINE_PATTERN = re.compile(r"^(?:[=\-#*~_]{3,}|\s*[=\-]{3,}.*?[=\-]{3,}\s*)$")

# 単語1つのみのセクション見出し（例: Workers, Main, Helpers:）
SINGLE_WORD_SECTION_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+:?$")

# 著作権・ライセンス・出所表記キーワード
COPYRIGHT_KEYWORDS = [
    "copyright",
    "(c)",
    "all rights reserved",
    "licensed under",
    "license",
    "cc by",
    "http://",
    "https://",
    "mostly copy-paste from",
    "copied from",
    "modified from",
    "taken from",
]

# 変更履歴・作業メモパターン
HISTORY_PATTERNS = [
    re.compile(r"^[【\[](?:修正|追加|変更|削除|更新)[】\]]"),
    re.compile(r"^---\s*ここまで追加"),
    re.compile(r"^Experimental", re.IGNORECASE),
    re.compile(r"^(?:TODO|FIXME|NOTE|XXX|BUG|HACK)(?:\([^)]*\))?\s*:", re.IGNORECASE),
]

# 切り捨てセクションヘッダー（以降の行を即座に破棄）
SECTION_TRUNCATE_PATTERN = re.compile(
    r"^(?:Args|Arguments|Parameters|Params|Keyword\s+Args|Returns?|Yields?|Raises?|Usage|Examples?|Notes?|References?)\s*:",
    re.IGNORECASE,
)

# 引数・型定義行（例: table : [H, W], int）
ARG_TYPE_DEF_PATTERNS = [
    re.compile(r"^[A-Za-z0-9_]+\s*:\s*\[.*\]"),
    re.compile(r"^[A-Za-z0-9_]+\s*\([^)]*\)\s*:"),
]


def clean_comment_line(raw_line: str) -> str:
    """行頭・行末のコメント構文記号（#, //, /*, *, */）を除去してトリムする"""
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
    if re.match(r"^[=\-#*~_]{3,}", line):
        return True
    if re.match(r"^[=\-]{3,}.*?[=\-]{3,}", line):
        return True
    return False


def _is_single_word_section(line: str) -> bool:
    # ASCII英数字の1単語のみ（セクション区切り見出し）を除外
    return bool(SINGLE_WORD_SECTION_PATTERN.match(line))


def _is_copyright_or_license(line: str) -> bool:
    if line.startswith("#!"):
        return True
    lower = line.lower()
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
    """
    推奨アルゴリズムに従い、行リストからノイズをスキップして最初の有効なサマリー自然文1行を抽出する。
    """
    for raw_line in lines:
        line = clean_comment_line(raw_line)
        if not line:
            continue

        # 引数・戻り値セクション以降は即座に探索終了（切り捨て）
        if _is_section_truncate_trigger(line):
            break

        # 各種ノイズパターンの除外
        if _is_decoration_line(line) or _is_single_word_section(line):
            continue
        if _is_copyright_or_license(line):
            continue
        if _is_history_or_memo(line):
            continue
        if _is_type_def_line(line):
            continue
        if _is_trivial_docstring(line, entity_name):
            continue

        # Role: / AI: / Rule: のタグ表記をフォーマットして採用
        if re.match(r"^(?:Role|AI|Rule)\s*:", line, re.IGNORECASE):
            tag = line.split(":", 1)[0].strip()
            rest = line.split(":", 1)[1].strip()
            if not rest or _is_trivial_docstring(rest, entity_name):
                continue
            return f"**[{tag}]** {rest}"

        return line

    return None


def extract_clean_role_description(
    docstring: Optional[str] = None,
    leading_comments: Optional[List[str]] = None,
    entity_name: str = "",
) -> str:
    """
    docstringおよび直前コメントからサニタイズされた役割サマリー（1文）を抽出・統合する。
    """
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
                tag = cleaned.split(":", 1)[0].strip()
                rest = cleaned.split(":", 1)[1].strip()
                if rest and not _is_trivial_docstring(rest, entity_name):
                    explicit_role_comment = f"**[{tag}]** {rest}"
                    break
        if not summary:
            fallback_comment_summary = extract_summary_line(leading_comments, entity_name)

    if explicit_role_comment:
        if summary and summary != explicit_role_comment:
            return f"{explicit_role_comment} / {summary}"
        return explicit_role_comment

    if summary:
        return summary

    if fallback_comment_summary:
        return fallback_comment_summary

    return ""
