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
