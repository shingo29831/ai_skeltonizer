"""Module: @role: ai_meta設定ファイル群からの書き込みパス制御ポリシー読込およびサンプルファイル生成を担当する。"""
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from .write_path_policy import (
    WritePathMode,
    WritePathPolicy,
    normalize_write_path_mode,
    strip_matching_quotes,
)

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None  # type: ignore[assignment]

WRITE_PATHS_SAMPLE_FILENAME = "writable_paths.sample.txt"

ALLOWED_WRITE_PATHS_KEYS = (
    "allowed_write_paths",
    "writable_paths",
    "write_allow_paths",
    "allow_write_paths",
    "write_paths",
    "write_whitelist",
    "writable_whitelist",
    "whitelist_write_paths",
)

DENIED_WRITE_PATHS_KEYS = (
    "denied_write_paths",
    "disallowed_write_paths",
    "blocked_write_paths",
    "forbidden_write_paths",
    "write_deny_paths",
    "deny_write_paths",
    "readonly_paths",
    "read_only_paths",
    "write_blacklist",
    "writable_blacklist",
    "blacklist_write_paths",
)

WRITE_PATH_MODE_KEYS = (
    "write_paths_mode",
    "write_path_mode",
    "write_policy_mode",
    "writable_paths_mode",
    "write_mode",
)

WRITE_PATH_ENABLED_KEYS = (
    "write_paths_enabled",
    "enable_write_paths",
    "write_path_restriction",
)

ALLOWED_WRITE_PATH_TXT_FILES = (
    "allowed_write_paths.txt",
    "writable_paths.txt",
    "allow_write_paths.txt",
)

DENIED_WRITE_PATH_TXT_FILES = (
    "denied_write_paths.txt",
    "deny_write_paths.txt",
    "disallowed_write_paths.txt",
    "blocked_write_paths.txt",
    "readonly_paths.txt",
)

META_CANDIDATE_CONFIG_FILES = (
    "config.json",
    "settings.json",
    "aiskel.json",
    "aiskel_config.json",
    ".aiskelrc.json",
    ".aiskelrc",
    "config.toml",
    "settings.toml",
    "config.yaml",
    "config.yml",
    "settings.yaml",
    "settings.yml",
    "config.ini",
    "settings.ini",
)

DEFAULT_WRITE_PATHS_SAMPLE_CONTENT = """# aiskel 書き込み可能パス制御 設定サンプル (writable_paths.sample.txt)
#
# このファイルは参考用のサンプルです（このファイル自体は設定として読み込まれません）。
# 実際の設定は同じフォルダ内の config.json (または config.toml / config.yaml / *.txt) に記述してください。
#
# ==============================================================================
# 1. モード設定 (write_paths_mode)
# ==============================================================================
#   - "disabled"  : 書き込みパス制限を無効化（すべてのパスへ書き込み可。リストを残したまま一時停止可能）
#   - "whitelist" : ホワイトリストモード（allowed_write_paths に一致するパスのみ許可。空なら全拒否）
#   - "blacklist" : ブラックリストモード（denied_write_paths に一致するパスのみ拒否。それ以外は許可）
#   - "both"      : ホワイトリストとブラックリストを併用（許可対象のうち禁止対象を除外）
#   - "auto"      : 自動判定（デフォルト。非空のリストのみ有効化し、両方指定時はブラックリスト優先。両方空なら制限なし）
#
# ※ パス指定ルール:
#   - 相対パスはプロジェクトルート基準で解決されます
#   - ディレクトリ: "src/" または "src" -> 配下の全ファイル・サブディレクトリに一致
#   - 単一ファイル: "src/main.py" -> 指定ファイルのみに一致
#   - ワイルドカード(Glob): "*.env", "src/**/*.py", "config/*.secret.json" 等が利用可能
#
# ==============================================================================
# 2. config.json 記述例
# ==============================================================================
#
# [例A] ホワイトリスト（指定ディレクトリ・ファイルのみ書き込み許可）
# {
#   "write_paths_mode": "whitelist",
#   "allowed_write_paths": [
#     "src/",
#     "tests/",
#     "README.md"
#   ],
#   "denied_write_paths": []
# }
#
# [例B] ブラックリスト（重要ファイル・機密ファイルのみ書き込み禁止）
# {
#   "write_paths_mode": "blacklist",
#   "allowed_write_paths": [],
#   "denied_write_paths": [
#     ".env",
#     "*.secret.*",
#     "config/production.json",
#     "migrations/"
#   ]
# }
#
# [例C] ホワイトリスト + ブラックリスト併用（src/ 配下のみ許可しつつ、一部ファイルを除外）
# {
#   "write_paths_mode": "both",
#   "allowed_write_paths": [
#     "src/"
#   ],
#   "denied_write_paths": [
#     "src/generated/",
#     "src/core/secrets.py"
#   ]
# }
#
# [例D] 無効化（リスト設定を保持したまま、制限を一時的に無効化）
# {
#   "write_paths_mode": "disabled",
#   "allowed_write_paths": ["src/"],
#   "denied_write_paths": [".env"]
# }
#
# ==============================================================================
# 3. テキストファイル / YAML / TOML での記述例
# ==============================================================================
# [テキストファイル] 同フォルダに以下のファイルを配置（1行1パス、# でコメント）:
#   - ai_meta/allowed_write_paths.txt : ホワイトリスト用
#   - ai_meta/denied_write_paths.txt  : ブラックリスト用
#
# [YAML (config.yaml)]
#   write_paths_mode: whitelist
#   allowed_write_paths:
#     - src/
#     - tests/
#   denied_write_paths:
#     - "*.env"
#
# [TOML (config.toml)]
#   write_paths_mode = "blacklist"
#   denied_write_paths = [".env", "secrets/"]
"""

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

def _strip_inline_comment(text: str) -> str:
    quote_char: Optional[str] = None
    for idx, ch in enumerate(text):
        if quote_char:
            if ch == quote_char:
                quote_char = None
        else:
            if ch in ('"', "'"):
                quote_char = ch
            elif ch in ("#", ";"):
                return text[:idx].strip()
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

def _collect_meta_dirs(project_root: Path, output_dir: Optional[Path] = None) -> List[Path]:
    meta_dirs: List[Path] = []
    if output_dir:
        meta_dirs.append(output_dir)
    meta_dirs.extend([
        project_root / "ai_meta",
        project_root / ".ai_meta",
    ])
    checked_dirs: List[Path] = []
    for d in meta_dirs:
        try:
            resolved = d.resolve()
            if resolved not in checked_dirs and resolved.exists() and resolved.is_dir():
                checked_dirs.append(resolved)
        except OSError:
            continue
    return checked_dirs

def iter_meta_config_files(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> Iterator[Path]:
    for d in _collect_meta_dirs(project_root, output_dir):
        for fname in META_CANDIDATE_CONFIG_FILES:
            target = d / fname
            if target.is_file():
                yield target

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
                    item_raw = stripped_next[1:].strip()
                    if item_raw and not item_raw.startswith("#"):
                        if not item_raw.startswith(('"', "'")):
                            item_raw = item_raw.split(" #", 1)[0].strip()
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

def _load_txt_path_list(checked_dirs: Sequence[Path], filenames: Sequence[str]) -> List[str]:
    for d in checked_dirs:
        for fname in filenames:
            target = d / fname
            if not target.is_file():
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except Exception:
                continue
            return extract_paths_from_val(text)
    return []

def load_write_path_policy_from_meta(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> WritePathPolicy:
    """ai_meta等の設定ファイルから書き込み可能パスのポリシー(モード・許可・禁止リスト)を取得する。"""
    resolved_root = project_root.resolve()
    explicit_mode: Optional[WritePathMode] = None
    allow_raw: List[str] = []
    deny_raw: List[str] = []

    for target in iter_meta_config_files(resolved_root, output_dir):
        try:
            content = target.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not content:
            continue

        if target.suffix.lower() == ".json" or content.startswith("{"):
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    found, mode, allows, denies = _parse_write_policy_from_dict(data)
                    if found:
                        explicit_mode, allow_raw, deny_raw = mode, allows, denies
                        break
                continue
            except Exception:
                # 明示的な.jsonファイルが構文不正の場合にテキスト正規表現へ誤フォールバックしない
                if target.suffix.lower() == ".json":
                    continue

        if target.suffix.lower() == ".toml" and tomllib is not None:
            try:
                data = tomllib.loads(content)
                if isinstance(data, dict):
                    found, mode, allows, denies = _parse_write_policy_from_dict(data)
                    if found:
                        explicit_mode, allow_raw, deny_raw = mode, allows, denies
                        break
                continue
            except Exception:
                continue

        found, mode, allows, denies = _parse_write_policy_from_text(content)
        if found:
            explicit_mode, allow_raw, deny_raw = mode, allows, denies
            break

    if explicit_mode == WritePathMode.DISABLED:
        return WritePathPolicy(
            project_root=resolved_root,
            mode=WritePathMode.DISABLED,
            allow_patterns=tuple(allow_raw),
            deny_patterns=tuple(deny_raw),
        )

    checked_dirs = _collect_meta_dirs(resolved_root, output_dir)
    if not allow_raw:
        allow_raw = _load_txt_path_list(checked_dirs, ALLOWED_WRITE_PATH_TXT_FILES)
    if not deny_raw:
        deny_raw = _load_txt_path_list(checked_dirs, DENIED_WRITE_PATH_TXT_FILES)

    return WritePathPolicy(
        project_root=resolved_root,
        mode=explicit_mode or WritePathMode.AUTO,
        allow_patterns=tuple(dict.fromkeys(allow_raw)),
        deny_patterns=tuple(dict.fromkeys(deny_raw)),
    )

def load_allowed_write_paths_from_meta(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> List[Path]:
    """ai_meta等の設定ファイルから書き込み許可対象のファイル・ディレクトリパス群を取得する。"""
    policy = load_write_path_policy_from_meta(project_root, output_dir)
    if policy.mode in (WritePathMode.DISABLED, WritePathMode.BLACKLIST):
        return []
    return policy.allow_paths

def ensure_write_paths_sample_file(output_dir: Path, project_root: Path) -> Optional[Path]:
    """ai_meta内に書き込み可能パス指定方法のサンプルファイルが存在しない場合に出力する。"""
    sample_path = output_dir / WRITE_PATHS_SAMPLE_FILENAME
    if sample_path.exists():
        return None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        sample_path.write_text(DEFAULT_WRITE_PATHS_SAMPLE_CONTENT, encoding="utf-8")
        return sample_path
    except Exception as e:
        print(f"⚠ 書き込み可能パスサンプルファイルの生成に失敗しました: {e}", file=sys.stderr)
        return None
