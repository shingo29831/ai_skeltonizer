# src/py_skeletonizer/config.py
"""
Role: フルコード保存対象、関数の保護リスト、およびバンドル出力に関する設定情報を保持・検証する。
"""
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple, Union

from .write_path_policy import (
    WritePathMode,
    WritePathPolicy,
    is_write_path_allowed,
    resolve_allowed_write_paths,
    strip_matching_quotes as _strip_matching_quotes,
)
from .write_path_policy_loader import (
    ALLOWED_WRITE_PATHS_KEYS as _ALLOWED_WRITE_PATHS_KEYS,
    ALLOWED_WRITE_PATH_TXT_FILES,
    DENIED_WRITE_PATH_TXT_FILES,
    META_CANDIDATE_CONFIG_FILES,
    WRITE_PATHS_SAMPLE_FILENAME,
    _collect_meta_dirs,
    ensure_write_paths_sample_file,
    extract_paths_from_val as _extract_paths_from_val,
    iter_meta_config_files as _iter_meta_config_files,
    load_allowed_write_paths_from_meta,
    load_write_path_policy_from_meta,
    parse_allowed_write_paths_from_text as _parse_allowed_write_paths_from_text,
    split_inline_list_items as _split_inline_list_items,
    tomllib as _tomllib,
)

_META_CANDIDATE_CONFIG_FILES = META_CANDIDATE_CONFIG_FILES
PENDING_COMMIT_FILENAME = "pending_commit.json"
DEFAULT_PATCH_HANDOFF_MAX_TABS = 1
DEFAULT_ORGANIZE_MAX_FILES = 10
_ORGANIZE_MAX_FILES_KEYS = (
    "organize_max_files",
    "hierarchy_max_files",
    "dir_max_files",
    "max_files_per_dir",
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
_STATIC_ANALYSIS_COMMAND_KEYS = (
    "static_analysis_command",
    "static_analysis_cmd",
    "static_analysis",
    "lint_command",
    "lint_cmd",
    "syntax_check_command",
    "syntax_check_cmd",
    "typecheck_command",
    "typecheck_cmd",
    "staticAnalysisCommand",
)
_STATIC_ANALYSIS_TXT_FILES = (
    "static_analysis_command.txt",
    "static_analysis_cmd.txt",
    "static_analysis.txt",
    "lint_command.txt",
    "lint_cmd.txt",
    "syntax_check_command.txt",
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

    # 構造化設定の他項目(static_analysis_command等)をテストコマンドへ誤認しないため専用テキストのみ行フォールバック
    if file_path.suffix.lower() not in (".json", ".toml", ".yaml", ".yml", ".ini") and not file_path.name.startswith(".aiskelrc"):
        lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith(("#", "//", ";"))]
        if lines and not lines[0].startswith(("[", "{")):
            return lines

    return None

def _strip_unquoted_comment(text: str) -> str:
    quote: Optional[str] = None
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote == '"':
            escaped = True
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch in ("#", ";") and (i == 0 or text[i - 1].isspace()):
            return text[:i].strip()
    return text.strip()

def _extract_commands_from_mapping(data: dict) -> Optional[List[str]]:
    for k in _STATIC_ANALYSIS_COMMAND_KEYS:
        if k in data:
            cmds = _extract_commands_from_val(data[k])
            if cmds:
                return cmds
    if "scripts" in data and isinstance(data["scripts"], dict):
        for k in ("static_analysis", "lint", "typecheck", "syntax_check"):
            if k in data["scripts"]:
                cmds = _extract_commands_from_val(data["scripts"][k])
                if cmds:
                    return cmds
    return None

def _extract_commands_from_structured_text(content: str, keys: Tuple[str, ...]) -> Optional[List[str]]:
    lines = content.splitlines()
    empty_literals = {"[]", "()", "null", "none", "~", '""', "''"}
    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", "//", ";")):
            continue
        for key in keys:
            m = re.match(rf"^{re.escape(key)}[ \t]*[:=][ \t]*(.*)$", stripped, re.IGNORECASE)
            if not m:
                continue
            rhs = _strip_unquoted_comment(m.group(1))
            if rhs.lower() in empty_literals:
                continue
            if rhs.startswith("["):
                if rhs.endswith("]"):
                    items = [_strip_matching_quotes(it) for it in _split_inline_list_items(rhs[1:-1])]
                    cmds = [it.strip() for it in items if it.strip()]
                    if cmds:
                        return cmds
                    continue
                collected: List[str] = []
                first_body = rhs[1:].strip()
                if first_body:
                    for it in _split_inline_list_items(first_body):
                        cleaned = _strip_matching_quotes(it).strip()
                        if cleaned:
                            collected.append(cleaned)
                for sub_raw in lines[idx + 1:]:
                    sub = _strip_unquoted_comment(sub_raw.strip())
                    if not sub:
                        continue
                    end_pos = sub.find("]")
                    body = sub[:end_pos] if end_pos != -1 else sub
                    for it in _split_inline_list_items(body):
                        cleaned = _strip_matching_quotes(it).strip()
                        if cleaned:
                            collected.append(cleaned)
                    if end_pos != -1:
                        break
                if collected:
                    return collected
                continue
            if not rhs:
                base_indent = len(raw_line) - len(raw_line.lstrip(" \t"))
                yaml_items: List[str] = []
                for sub_raw in lines[idx + 1:]:
                    sub_stripped = sub_raw.strip()
                    if not sub_stripped or sub_stripped.startswith("#"):
                        continue
                    sub_indent = len(sub_raw) - len(sub_raw.lstrip(" \t"))
                    if sub_indent <= base_indent and not sub_stripped.startswith("-"):
                        break
                    if sub_stripped.startswith("-"):
                        raw_item = _strip_unquoted_comment(sub_stripped[1:].strip())
                        item_val = _strip_matching_quotes(raw_item).strip()
                        if item_val:
                            yaml_items.append(item_val)
                    else:
                        break
                if yaml_items:
                    return yaml_items
                continue
            unquoted = _strip_matching_quotes(rhs).strip()
            if unquoted and unquoted.lower() not in empty_literals:
                return [unquoted]
    return None

def load_static_analysis_command_from_meta(
    project_root: Path, output_dir: Optional[Path] = None
) -> Optional[List[str]]:
    """ai_meta等の設定ファイルから静的コード解析用コマンドを取得する"""
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
                    cmds = _extract_commands_from_mapping(data)
                    if cmds:
                        return cmds
                continue
            except Exception:
                if target.suffix.lower() == ".json":
                    continue

        if target.suffix.lower() == ".toml" and _tomllib is not None:
            try:
                data = _tomllib.loads(content)
                if isinstance(data, dict):
                    cmds = _extract_commands_from_mapping(data)
                    if cmds:
                        return cmds
                continue
            except Exception:
                continue

        parsed = _extract_commands_from_structured_text(content, _STATIC_ANALYSIS_COMMAND_KEYS)
        if parsed:
            return parsed

    for d in _collect_meta_dirs(project_root, output_dir):
        for fname in _STATIC_ANALYSIS_TXT_FILES:
            target = d / fname
            if not target.is_file():
                continue
            try:
                content = target.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            lines = [
                l.strip()
                for l in content.splitlines()
                if l.strip() and not l.strip().startswith(("#", "//", ";"))
            ]
            if lines:
                return lines

    return None

def ensure_default_config_file(output_dir: Path, project_root: Path) -> Optional[Path]:
    """ai_meta内に設定ファイルが存在しない場合、デフォルトのconfig.jsonを安全に自動生成する"""
    # なぜ必要か: 既に何らかの設定ファイルが存在する場合は上書き破壊を防止するため
    existing_cmd = load_test_command_from_meta(project_root, output_dir)
    existing_static_cmd = load_static_analysis_command_from_meta(project_root, output_dir)
    config_path = output_dir / "config.json"
    if (
        existing_cmd is not None
        or existing_static_cmd is not None
        or config_path.exists()
        or any(_iter_meta_config_files(project_root, output_dir))
    ):
        return None

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        config_data = {
            "bundle_filename": "architecture_bundle.txt",
            "test_command": [],
            "static_analysis_command": [],
            "exclude_paths": [],
            "write_paths_mode": "auto",
            "allowed_write_paths": [],
            "denied_write_paths": [],
            "patch_handoff_max_tabs": DEFAULT_PATCH_HANDOFF_MAX_TABS,
            "description": (
                "test_command には aiskel apply (-e / -T) 実行後に自動実行されるテスト・検証コマンド、"
                "static_analysis_command には aiskel a 適用後に自動実行される静的コード解析(構文・型・Lintチェック等)コマンドを記述してください "
                "(配列または改行で複数指定可)。static_analysis_command が失敗した場合は変更を自動で元に戻しエラー内容をプロンプトへ追記します。"
                f"書き込み可能パスの指定方法は同フォルダの {WRITE_PATHS_SAMPLE_FILENAME} を参照してください"
            ),
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

def load_organize_config_from_meta(
    project_root: Path, output_dir: Optional[Path] = None
) -> Tuple[Optional[int], Set[str]]:
    """ai_meta等の設定ファイルから階層整理の対象閾値(同一階層ファイル数)および除外パスを取得する"""
    _, _, exclude_paths = load_bloat_config_from_meta(project_root, output_dir)
    max_files: Optional[int] = None

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
                    for k in _ORGANIZE_MAX_FILES_KEYS:
                        if k in data:
                            val = data[k]
                            if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                                max_files = val
                                break
                continue
            except Exception:
                if target.suffix.lower() == ".json":
                    continue

        for k in _ORGANIZE_MAX_FILES_KEYS:
            pattern = re.compile(
                rf"^[ \t]*{re.escape(k)}[ \t]*[:=][ \t]*([\"']?)(\d+)\1[ \t]*(?:[#;].*)?$",
                re.MULTILINE | re.IGNORECASE,
            )
            m = pattern.search(content)
            if m:
                try:
                    parsed = int(m.group(2))
                    if parsed > 0:
                        max_files = parsed
                        break
                except ValueError:
                    pass

    return max_files, exclude_paths

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
