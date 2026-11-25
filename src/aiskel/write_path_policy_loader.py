"""Module: @role: ai_meta設定ファイル群からの書き込みパス制御ポリシー読込・サンプル生成および公開ファサードを提供する。"""
import json
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from .write_path_policy import WritePathMode, WritePathPolicy
from .write_path_policy_constants import (
    ALLOWED_WRITE_PATHS_KEYS,
    ALLOWED_WRITE_PATH_TXT_FILES,
    DEFAULT_WRITE_PATHS_SAMPLE_CONTENT,
    DENIED_WRITE_PATHS_KEYS,
    DENIED_WRITE_PATH_TXT_FILES,
    META_CANDIDATE_CONFIG_FILES,
    WRITE_PATHS_SAMPLE_FILENAME,
    WRITE_PATH_ENABLED_KEYS,
    WRITE_PATH_MODE_KEYS,
)
from .write_path_policy_parser import (
    _extract_bracket_list_inner,
    _parse_path_list_from_text,
    _parse_write_policy_from_dict,
    _parse_write_policy_from_text,
    _strip_inline_comment,
    extract_paths_from_val,
    parse_allowed_write_paths_from_text,
    split_inline_list_items,
)

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None  # type: ignore[assignment]


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
