"""Module: @role: 設定辞書およびYAML/TOML/INI風テキストからの書き込みパス制御ポリシー構文解析を担当する。"""
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .write_path_policy import (
    WritePathMode,
    normalize_write_path_mode,
    strip_matching_quotes,
)
from .write_path_policy_constants import (
    ALLOWED_WRITE_PATHS_KEYS,
    DENIED_WRITE_PATHS_KEYS,
    WRITE_PATH_ENABLED_KEYS,
    WRITE_PATH_MODE_KEYS,
)

def extract_paths_from_val(val: object) -> List[str]:
    paths: List[str] = []
    if isinstance(val, str):
        for line in val.splitlines():
            cleaned = strip_matching_quotes(line)
            if cleaned and not cleaned.startswith(("#", "//", ";")):
                paths.append(cleaned)
    elif isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                cleaned = strip_matching_quotes(item)
                if cleaned:
                    paths.append(cleaned)
    return paths

def split_inline_list_items(inner: str) -> List[str]:
    # なぜ必要か: クォート内のカンマや空白を保持したまま配列要素と行末コメントを分離するため
    items: List[str] = []
    buf: List[str] = []
    quote_char: Optional[str] = None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote_char:
            if ch == quote_char:
                quote_char = None
            else:
                buf.append(ch)
        else:
            if ch in ('"', "'"):
                quote_char = ch
            elif ch == "#":
                while i < len(inner) and inner[i] != "\n":
                    i += 1
                continue
            elif ch in (",", "\n"):
                token = "".join(buf).strip()
                if token:
                    items.append(token)
                buf.clear()
            else:
                buf.append(ch)
        i += 1
    token = "".join(buf).strip()
    if token:
        items.append(token)
    return items

def _strip_inline_comment(
    text: str,
    *,
    allow_semicolon: bool = True,
    require_hash_boundary: bool = False,
) -> str:
    quote_char: Optional[str] = None
    just_closed_quote = False
    for idx, ch in enumerate(text):
        if quote_char:
            if ch == quote_char:
                quote_char = None
                just_closed_quote = True
            continue
        if ch in ('"', "'"):
            # なぜ必要か: 要素先頭や空白直後のクォートのみ文字列囲みとみなすため
            if idx == 0 or text[idx - 1].isspace():
                quote_char = ch
                just_closed_quote = False
                continue
        if ch == "#":
            if not require_hash_boundary or idx == 0 or text[idx - 1].isspace() or just_closed_quote:
                return text[:idx].strip()
        elif allow_semicolon and ch == ";":
            return text[:idx].strip()
        just_closed_quote = False
    return text.strip()

def _extract_bracket_list_inner(lines: List[str], start_idx: int, initial_rhs: str) -> str:
    # なぜ必要か: クォート内のGlob文字クラス([...])やコメント内の]で配列終端を誤認しないため
    def _find_closing_bracket(text: str) -> int:
        quote_char: Optional[str] = None
        depth = 0
        i = 0
        while i < len(text):
            ch = text[i]
            if quote_char:
                if ch == quote_char:
                    quote_char = None
            else:
                if ch in ('"', "'"):
                    quote_char = ch
                elif ch == "#":
                    while i < len(text) and text[i] != "\n":
                        i += 1
                    continue
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return -1

    collected = [initial_rhs]
    end_idx = start_idx
    while end_idx < len(lines):
        joined = "\n".join(collected)
        close_pos = _find_closing_bracket(joined)
        if close_pos != -1:
            return joined[1:close_pos]
        end_idx += 1
        if end_idx >= len(lines):
            break
        collected.append(lines[end_idx])
    return "\n".join(collected)[1:]

def _parse_path_list_from_text(
    content: str,
    keys: Sequence[str],
) -> Tuple[Optional[List[str]], bool]:
    keys_alt = "|".join(re.escape(k) for k in keys)
    key_re = re.compile(rf"^([ \t]*)(?:{keys_alt})[ \t]*([:=])[ \t]*(.*)$")
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        m = key_re.match(line)
        if not m:
            continue
        base_indent = len(m.group(1).expandtabs(2))
        rhs = m.group(3).strip()

        if rhs.startswith("["):
            inner = _extract_bracket_list_inner(lines, idx, rhs)
            return [p for p in (strip_matching_quotes(x) for x in split_inline_list_items(inner)) if p], False

        rhs_no_comment = _strip_inline_comment(rhs)
        if rhs_no_comment.lower() in ("false", "null", "none"):
            return [], rhs_no_comment.lower() == "false"

        if not rhs_no_comment:
            yaml_items: List[str] = []
            j = idx + 1
            while j < len(lines):
                raw_next = lines[j]
                stripped_next = raw_next.strip()
                if not stripped_next or stripped_next.startswith("#"):
                    j += 1
                    continue
                next_indent = len(raw_next[: len(raw_next) - len(raw_next.lstrip(" \t"))].expandtabs(2))
                if next_indent < base_indent:
                    break
                if stripped_next.startswith("- ") or stripped_next == "-":
                    item_raw = _strip_inline_comment(
                        stripped_next[1:].strip(),
                        allow_semicolon=False,
                        require_hash_boundary=True,
                    )
                    if item_raw:
                        cleaned = strip_matching_quotes(item_raw)
                        if cleaned:
                            yaml_items.append(cleaned)
                    j += 1
                    continue
                break
            return yaml_items, False

        return [p for p in (strip_matching_quotes(x) for x in split_inline_list_items(rhs_no_comment)) if p], False
    return None, False

def parse_allowed_write_paths_from_text(content: str) -> Optional[List[str]]:
    parsed, _ = _parse_path_list_from_text(content, ALLOWED_WRITE_PATHS_KEYS)
    return parsed

def _parse_write_policy_from_dict(
    data: Dict[str, object],
) -> Tuple[bool, Optional[WritePathMode], List[str], List[str]]:
    found_any = False
    explicit_mode: Optional[WritePathMode] = None
    disabled_by_flag = False
    allow_raw: List[str] = []
    deny_raw: List[str] = []

    for ek in WRITE_PATH_ENABLED_KEYS:
        if ek in data and isinstance(data[ek], bool):
            found_any = True
            if data[ek] is False:
                disabled_by_flag = True
            break

    for mk in WRITE_PATH_MODE_KEYS:
        if mk in data:
            found_any = True
            explicit_mode = normalize_write_path_mode(data[mk])
            break

    for container_key in ("write_paths", "write_policy"):
        nested = data.get(container_key)
        if isinstance(nested, dict):
            found_any = True
            if isinstance(nested.get("enabled"), bool) and nested["enabled"] is False:
                disabled_by_flag = True
            if explicit_mode is None and "mode" in nested:
                explicit_mode = normalize_write_path_mode(nested["mode"])
            for ak in ("allow", "allowed", "whitelist", "allowed_write_paths"):
                if ak in nested and nested[ak] is not None:
                    allow_raw.extend(extract_paths_from_val(nested[ak]))
            for dk in ("deny", "denied", "blacklist", "denied_write_paths"):
                if dk in nested and nested[dk] is not None:
                    deny_raw.extend(extract_paths_from_val(nested[dk]))

    for ak in ALLOWED_WRITE_PATHS_KEYS:
        if ak in data:
            val = data[ak]
            if isinstance(val, dict):
                continue
            found_any = True
            if isinstance(val, bool):
                if val is False:
                    disabled_by_flag = True
            elif val is not None:
                allow_raw.extend(extract_paths_from_val(val))
            break

    for dk in DENIED_WRITE_PATHS_KEYS:
        if dk in data:
            val = data[dk]
            found_any = True
            if isinstance(val, bool):
                if val is False:
                    disabled_by_flag = True
            elif val is not None:
                deny_raw.extend(extract_paths_from_val(val))
            break

    if disabled_by_flag:
        explicit_mode = WritePathMode.DISABLED

    return found_any, explicit_mode, allow_raw, deny_raw

def _parse_write_policy_from_text(
    content: str,
) -> Tuple[bool, Optional[WritePathMode], List[str], List[str]]:
    explicit_mode: Optional[WritePathMode] = None
    disabled_by_flag = False
    found_any = False

    en_alt = "|".join(re.escape(k) for k in WRITE_PATH_ENABLED_KEYS)
    en_re = re.compile(
        rf"^[ \t]*(?:{en_alt})[ \t]*[:=][ \t]*(false|true)[ \t]*(?:[#;].*)?$",
        re.MULTILINE | re.IGNORECASE,
    )
    m_en = en_re.search(content)
    if m_en:
        found_any = True
        if m_en.group(1).lower() == "false":
            disabled_by_flag = True

    mode_alt = "|".join(re.escape(k) for k in WRITE_PATH_MODE_KEYS)
    mode_re = re.compile(rf"^[ \t]*(?:{mode_alt})[ \t]*[:=][ \t]*(.*)$", re.MULTILINE | re.IGNORECASE)
    m_mode = mode_re.search(content)
    if m_mode:
        found_any = True
        rhs = _strip_inline_comment(m_mode.group(1))
        explicit_mode = normalize_write_path_mode(rhs)

    allow_parsed, allow_false = _parse_path_list_from_text(content, ALLOWED_WRITE_PATHS_KEYS)
    deny_parsed, deny_false = _parse_path_list_from_text(content, DENIED_WRITE_PATHS_KEYS)
    if allow_parsed is not None or deny_parsed is not None:
        found_any = True
    if disabled_by_flag or (explicit_mode is None and (allow_false or deny_false)):
        explicit_mode = WritePathMode.DISABLED

    return found_any, explicit_mode, allow_parsed or [], deny_parsed or []
