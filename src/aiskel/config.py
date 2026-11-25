# src/py_skeletonizer/config.py
"""
Role: フルコード保存対象、関数の保護リスト、およびバンドル出力に関する設定情報を保持・検証する。
"""
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Set, Tuple, Union

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None  # type: ignore[assignment]

DEFAULT_PATCH_HANDOFF_MAX_TABS = 1
_ALLOWED_WRITE_PATHS_KEYS = (
    "allowed_write_paths",
    "writable_paths",
    "write_allow_paths",
    "allow_write_paths",
    "write_paths",
)
_PATCH_HANDOFF_MAX_TABS_KEYS = (
    "patch_handoff_max_tabs",
    "handoff_max_tabs",
    "auto_handoff_max_tabs",
    "auto_tab_limit",
    "max_auto_tabs",
    "patch_auto_tab_limit",
    "max_handoff_tabs",
)
_META_CANDIDATE_CONFIG_FILES = (
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


@dataclass
class SkeletonConfig:
    full_code_paths: Set[Path] = field(default_factory=set)
    keep_functions: Set[str] = field(default_factory=set)
    only_nodes: Set[str] = field(default_factory=set)
    create_bundle: bool = True
    bundle_format: str = "txt"  # "txt", "xml" または "markdown"
    policy_path: Optional[Path] = None
    bundle_filename: Optional[str] = None

    def is_full_code_path(self, target_path: Path) -> bool:
        for full_path in self.full_code_paths:
            if target_path == full_path or full_path in target_path.parents:
                return True
        return False


def _extract_commands_from_val(val: Union[str, list]) -> List[str]:
    cmds: List[str] = []
    if isinstance(val, str):
        lines = val.splitlines()
    elif isinstance(val, list):
        lines = []
        for item in val:
            if isinstance(item, str):
                lines.extend(item.splitlines())
    else:
        return cmds
    for line in lines:
        s = line.strip()
        if s and not s.startswith(("#", "//", ";")):
            cmds.append(s)
    return cmds


def _parse_command_file(file_path: Path) -> Optional[List[str]]:
    # なぜ必要か: 多様な設定形式(JSON/テキスト/スクリプト)から実行コマンドを確実に抽出するため
    try:
        content = file_path.read_text(encoding="utf-8").strip()
    except Exception:
        return None

    if not content:
        return None

    if file_path.suffix.lower() == ".json" or content.startswith("{"):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                priority_keys = (
                    "test_command", "test_cmd", "command", "cmd",
                    "test", "run", "exec", "post_apply", "validation_command", "testCommand"
                )
                for k in priority_keys:
                    if k in data:
                        cmds = _extract_commands_from_val(data[k])
                        if cmds:
                            return cmds
                if "scripts" in data and isinstance(data["scripts"], dict):
                    for k in ("test", "validation", "run"):
                        if k in data["scripts"]:
                            cmds = _extract_commands_from_val(data["scripts"][k])
                            if cmds:
                                return cmds
        except Exception:
            pass

    if file_path.suffix.lower() == ".txt":
        lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith(("#", "//", ";"))]
        if lines:
            return lines

    key_pattern = re.compile(
        r'^[ \t]*(?:test_command|test_cmd|command|cmd|test|run|exec|post_apply|testCommand)[ \t]*[:=][ \t]*["\']?(.*?)["\']?[ \t]*$',
        re.MULTILINE | re.IGNORECASE
    )
    m = key_pattern.search(content)
    if m:
        cmds = _extract_commands_from_val(m.group(1).strip())
        if cmds:
            return cmds

    lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith(("#", "//", ";"))]
    if lines and not lines[0].startswith(("[", "{")):
        return lines

    return None


def ensure_default_config_file(output_dir: Path, project_root: Path) -> Optional[Path]:
    """ai_meta内に設定ファイルが存在しない場合、デフォルトのconfig.jsonを安全に自動生成する"""
    # なぜ必要か: 既に何らかの設定ファイルが存在する場合は上書き破壊を防止するため
    existing_cmd = load_test_command_from_meta(project_root, output_dir)
    config_path = output_dir / "config.json"
    if existing_cmd is not None or config_path.exists():
        return None

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        config_data = {
            "bundle_filename": "architecture_bundle.txt",
            "test_command": [],
            "exclude_paths": [],
            "allowed_write_paths": [],
            "patch_handoff_max_tabs": DEFAULT_PATCH_HANDOFF_MAX_TABS,
            "description": "aiskel apply (-e / -T) 実行後に自動実行されるテスト・検証コマンドを記述してください (配列または改行で複数指定可)",
        }
        config_path.write_text(json.dumps(config_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return config_path
    except Exception as e:
        print(f"⚠ 設定ファイルの自動生成に失敗しました: {e}", file=sys.stderr)
        return None


def load_test_command_from_meta(project_root: Path, output_dir: Optional[Path] = None) -> Optional[List[str]]:
    """ai_metaディレクトリ等の設定ファイルから実行コマンドを取得する"""
    meta_dirs = []
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

    candidate_files = (
        "config.json",
        "settings.json",
        "aiskel.json",
        "aiskel_config.json",
        ".aiskelrc.json",
        ".aiskelrc",
        "test_command.txt",
        "test_cmd.txt",
        "command.txt",
        "test.txt",
        "run.txt",
        "config.toml",
        "settings.toml",
        "config.yaml",
        "config.yml",
        "settings.yaml",
        "settings.yml",
        "config.ini",
        "settings.ini",
    )

    for d in checked_dirs:
        for fname in candidate_files:
            target = d / fname
            if target.is_file():
                cmd = _parse_command_file(target)
                if cmd:
                    return cmd

    for d in checked_dirs:
        for script_name in ("test.sh", "test.bat", "test.cmd", "run.sh"):
            target = d / script_name
            if target.is_file():
                # なぜ必要か: WindowsバッチとPOSIXシェルスクリプトの実行互換性を保持するため
                if sys.platform == "win32" and target.suffix in (".bat", ".cmd"):
                    return [f'"{target}"']
                return [f'bash "{target}"' if target.suffix == ".sh" else f'"{target}"']

    return None


def load_bundle_filename_from_meta(project_root: Path, output_dir: Optional[Path] = None) -> Optional[str]:
    """ai_metaディレクトリ等の設定ファイルからバンドル出力ファイル名を取得する"""
    meta_dirs = []
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

    candidate_files = (
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
    priority_keys = ("bundle_filename", "bundle_name", "bundle_file", "architecture_bundle_filename")

    for d in checked_dirs:
        for fname in candidate_files:
            target = d / fname
            if not target.is_file():
                continue
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
                        for k in priority_keys:
                            if k in data and isinstance(data[k], str) and data[k].strip():
                                return data[k].strip()
                except Exception:
                    pass

            pattern = re.compile(
                r'^[ \t]*(?:bundle_filename|bundle_name|bundle_file|architecture_bundle_filename)[ \t]*[:=][ \t]*["\']?(.*?)["\']?[ \t]*$',
                re.MULTILINE | re.IGNORECASE,
            )
            m = pattern.search(content)
            if m and m.group(1).strip():
                return m.group(1).strip()

    return None


def load_bloat_config_from_meta(
    project_root: Path, output_dir: Optional[Path] = None
) -> Tuple[Optional[int], Optional[int], Set[str]]:
    """ai_meta等の設定ファイルから肥大化判定の閾値(lines, chars)および除外パスを取得する"""
    meta_dirs = []
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

    candidate_files = (
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

    max_lines: Optional[int] = None
    max_chars: Optional[int] = None
    exclude_paths: Set[str] = set()

    for d in checked_dirs:
        for fname in candidate_files:
            target = d / fname
            if not target.is_file():
                continue
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
                        for l_key in ("bloat_max_lines", "max_lines", "bloat_lines"):
                            if l_key in data and isinstance(data[l_key], int):
                                max_lines = data[l_key]
                                break
                        for c_key in ("bloat_max_chars", "max_chars", "bloat_chars"):
                            if c_key in data and isinstance(data[c_key], int):
                                max_chars = data[c_key]
                                break
                        for ex_key in ("exclude_paths", "ignore_paths", "bloat_exclude", "exclude", "ignore"):
                            if ex_key in data:
                                val = data[ex_key]
                                if isinstance(val, list):
                                    for item in val:
                                        if isinstance(item, str) and item.strip():
                                            exclude_paths.add(item.strip())
                                elif isinstance(val, str) and val.strip():
                                    exclude_paths.add(val.strip())
                except Exception:
                    pass

    return max_lines, max_chars, exclude_paths


def _iter_meta_config_files(
    project_root: Path, output_dir: Optional[Path] = None
) -> Iterator[Path]:
    meta_dirs = []
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

    for d in checked_dirs:
        for fname in _META_CANDIDATE_CONFIG_FILES:
            target = d / fname
            if target.is_file():
                yield target


def load_patch_handoff_max_tabs_from_meta(
    project_root: Path, output_dir: Optional[Path] = None
) -> Optional[int]:
    """ai_meta等の設定ファイルからパッチ検知時の新規タブ自動作成上限回数を取得する"""
    for target in _iter_meta_config_files(project_root, output_dir):
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
                    for k in _PATCH_HANDOFF_MAX_TABS_KEYS:
                        if k in data:
                            val = data[k]
                            if isinstance(val, int) and not isinstance(val, bool):
                                return max(0, val)
                continue
            except Exception:
                # 明示的な.jsonファイルが構文不正の場合にテキスト正規表現へ誤フォールバックしない
                if target.suffix.lower() == ".json":
                    continue

        for k in _PATCH_HANDOFF_MAX_TABS_KEYS:
            pattern = re.compile(
                rf"^[ \t]*{re.escape(k)}[ \t]*[:=][ \t]*([\"']?)(-?\d+)\1[ \t]*(?:[#;].*)?$",
                re.MULTILINE | re.IGNORECASE,
            )
            m = pattern.search(content)
            if m:
                try:
                    return max(0, int(m.group(2)))
                except ValueError:
                    pass

    return None

def _strip_matching_quotes(text: str) -> str:
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1].strip()
    return s

def _extract_paths_from_val(val: object) -> List[str]:
    paths: List[str] = []
    if isinstance(val, str):
        for line in val.splitlines():
            cleaned = _strip_matching_quotes(line)
            if cleaned and not cleaned.startswith(("#", "//", ";")):
                paths.append(cleaned)
    elif isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                cleaned = _strip_matching_quotes(item)
                if cleaned:
                    paths.append(cleaned)
    return paths

def _split_inline_list_items(inner: str) -> List[str]:
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

def _parse_allowed_write_paths_from_text(content: str) -> Optional[List[str]]:
    keys_alt = "|".join(re.escape(k) for k in _ALLOWED_WRITE_PATHS_KEYS)
    key_re = re.compile(rf"^([ \t]*)(?:{keys_alt})[ \t]*([:=])[ \t]*(.*)$")
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        m = key_re.match(line)
        if not m:
            continue
        base_indent = len(m.group(1).expandtabs(2))
        rhs = m.group(3).strip()

        if rhs.startswith("["):
            collected = [rhs]
            end_idx = idx
            while "]" not in collected[-1] and end_idx + 1 < len(lines):
                end_idx += 1
                collected.append(lines[end_idx])
            joined = "\n".join(collected)
            close_pos = joined.find("]")
            inner = joined[1:close_pos] if close_pos != -1 else joined[1:]
            return [p for p in (_strip_matching_quotes(x) for x in _split_inline_list_items(inner)) if p]

        rhs_no_comment = rhs.split("#", 1)[0].split(";", 1)[0].strip() if not rhs.startswith(('"', "'")) else rhs
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
                        cleaned = _strip_matching_quotes(item_raw)
                        if cleaned:
                            yaml_items.append(cleaned)
                    j += 1
                    continue
                break
            return yaml_items

        return [p for p in (_strip_matching_quotes(x) for x in _split_inline_list_items(rhs_no_comment)) if p]
    return None

def resolve_allowed_write_paths(
    raw_paths: Sequence[Union[str, Path]],
    project_root: Path,
) -> List[Path]:
    """設定された書き込み許可パス群をproject_root基準の解決済みPathリストへ正規化する。"""
    base_dir = project_root.resolve()
    resolved_list: List[Path] = []
    seen: Set[Path] = set()
    for raw in raw_paths:
        if isinstance(raw, Path):
            p = raw
        else:
            cleaned = _strip_matching_quotes(str(raw))
            if not cleaned:
                continue
            p = Path(cleaned)
        resolved = p.resolve() if p.is_absolute() else (base_dir / p).resolve()
        if resolved not in seen:
            seen.add(resolved)
            resolved_list.append(resolved)
    return resolved_list

def is_write_path_allowed(target_path: Path, allowed_paths: Sequence[Path]) -> bool:
    """対象ファイルパスが書き込み許可パス群のいずれかに該当・包含されるか判定する。"""
    if not allowed_paths:
        return True
    resolved_target = target_path.resolve()
    for allowed in allowed_paths:
        resolved_allowed = allowed.resolve()
        if resolved_target == resolved_allowed:
            return True
        # なぜ必要か: 実在する単一ファイルと同名プレフィックス配下への誤許可を防ぐため
        if resolved_allowed.is_file():
            continue
        if resolved_allowed in resolved_target.parents:
            return True
    return False

def load_allowed_write_paths_from_meta(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> List[Path]:
    """ai_meta等の設定ファイルから書き込み許可対象のファイル・ディレクトリパス群を取得する。"""
    for target in _iter_meta_config_files(project_root, output_dir):
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
                    for k in _ALLOWED_WRITE_PATHS_KEYS:
                        if k in data:
                            return resolve_allowed_write_paths(
                                _extract_paths_from_val(data[k]), project_root
                            )
                continue
            except Exception:
                # 明示的な.jsonファイルが構文不正の場合にテキスト正規表現へ誤フォールバックしない
                if target.suffix.lower() == ".json":
                    continue

        if target.suffix.lower() == ".toml" and tomllib is not None:
            try:
                data = tomllib.loads(content)
                if isinstance(data, dict):
                    for k in _ALLOWED_WRITE_PATHS_KEYS:
                        if k in data:
                            return resolve_allowed_write_paths(
                                _extract_paths_from_val(data[k]), project_root
                            )
                continue
            except Exception:
                continue

        parsed = _parse_allowed_write_paths_from_text(content)
        if parsed is not None:
            return resolve_allowed_write_paths(parsed, project_root)

    meta_dirs: List[Path] = []
    if output_dir:
        meta_dirs.append(output_dir)
    meta_dirs.extend([project_root / "ai_meta", project_root / ".ai_meta"])
    checked_dirs: List[Path] = []
    for d in meta_dirs:
        try:
            resolved = d.resolve()
            if resolved not in checked_dirs and resolved.exists() and resolved.is_dir():
                checked_dirs.append(resolved)
        except OSError:
            continue

    for d in checked_dirs:
        for fname in ("allowed_write_paths.txt", "writable_paths.txt", "allow_write_paths.txt"):
            target = d / fname
            if not target.is_file():
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except Exception:
                continue
            return resolve_allowed_write_paths(_extract_paths_from_val(text), project_root)

    return []
