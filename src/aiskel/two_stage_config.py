"""Module: @role: 2段階コンテキスト探索（1度目軽量インデックス＋2度目スコープ限定）の設定モデルと設定ファイル読込を担当する。"""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .write_path_policy import strip_matching_quotes as _strip_matching_quotes
from .write_path_policy_loader import iter_meta_config_files as _iter_meta_config_files, tomllib as _tomllib

DEFAULT_TWO_STAGE_MIN_TOKENS = 4000
DEFAULT_INDEX_INCLUDE_FILES = True
DEFAULT_SCOPE_INCLUDE_DEPS = True

_ENABLED_KEYS = ("two_stage_enabled", "two_stage", "enable_two_stage")
_MIN_TOKEN_KEYS = ("two_stage_min_tokens", "discovery_min_tokens")
_INC_FILES_KEYS = ("index_include_files", "discovery_include_files")
_INC_DEPS_KEYS = ("scope_include_deps", "discovery_include_deps")

@dataclass
class TwoStageConfig:
    """2段階コンテキスト探索の有効状態・閾値・インデックス/依存同梱設定を保持する。"""

    enabled: bool = True
    min_tokens: int = DEFAULT_TWO_STAGE_MIN_TOKENS
    index_include_files: bool = DEFAULT_INDEX_INCLUDE_FILES
    scope_include_deps: bool = DEFAULT_SCOPE_INCLUDE_DEPS

def _parse_bool_setting(val: object) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return None

def _apply_mapping_settings(
    data: dict,
    cfg: TwoStageConfig,
    found: dict,
) -> None:
    if not found["enabled"]:
        for k in _ENABLED_KEYS:
            if k in data and (parsed_b := _parse_bool_setting(data[k])) is not None:
                cfg.enabled = parsed_b
                found["enabled"] = True
                break
    if not found["min_tokens"]:
        for k in _MIN_TOKEN_KEYS:
            if k in data:
                val = data[k]
                if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                    cfg.min_tokens = val
                    found["min_tokens"] = True
                    break
    if not found["inc_files"]:
        for k in _INC_FILES_KEYS:
            if k in data and (parsed_b := _parse_bool_setting(data[k])) is not None:
                cfg.index_include_files = parsed_b
                found["inc_files"] = True
                break
    if not found["inc_deps"]:
        for k in _INC_DEPS_KEYS:
            if k in data and (parsed_b := _parse_bool_setting(data[k])) is not None:
                cfg.scope_include_deps = parsed_b
                found["inc_deps"] = True
                break

def load_two_stage_config_from_meta(
    project_root: Path, output_dir: Optional[Path] = None
) -> TwoStageConfig:
    """ai_meta等の設定ファイルから2段階コンテキスト探索設定を取得する"""
    cfg = TwoStageConfig()
    # 高優先度ファイルの設定値が後続ファイルで上書きされないよう項目別に確定管理する
    found = {"enabled": False, "min_tokens": False, "inc_files": False, "inc_deps": False}

    for target in _iter_meta_config_files(project_root, output_dir):
        if all(found.values()):
            break
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
                    _apply_mapping_settings(data, cfg, found)
                continue
            except Exception:
                if target.suffix.lower() == ".json":
                    continue

        if target.suffix.lower() == ".toml" and _tomllib is not None:
            try:
                data = _tomllib.loads(content)
                if isinstance(data, dict):
                    _apply_mapping_settings(data, cfg, found)
                continue
            except Exception:
                continue

        if not found["enabled"]:
            for k in _ENABLED_KEYS:
                m = re.search(rf"^[ \t]*{re.escape(k)}[ \t]*[:=][ \t]*([^\s#;]+)", content, re.MULTILINE | re.IGNORECASE)
                if m and (parsed_b := _parse_bool_setting(_strip_matching_quotes(m.group(1)))) is not None:
                    cfg.enabled = parsed_b
                    found["enabled"] = True
                    break
        if not found["min_tokens"]:
            for k in _MIN_TOKEN_KEYS:
                m = re.search(rf"^[ \t]*{re.escape(k)}[ \t]*[:=][ \t]*([\"']?)(\d+)\1[ \t]*(?:[#;].*)?$", content, re.MULTILINE | re.IGNORECASE)
                if m:
                    cfg.min_tokens = int(m.group(2))
                    found["min_tokens"] = True
                    break
        if not found["inc_files"]:
            for k in _INC_FILES_KEYS:
                m = re.search(rf"^[ \t]*{re.escape(k)}[ \t]*[:=][ \t]*([^\s#;]+)", content, re.MULTILINE | re.IGNORECASE)
                if m and (parsed_b := _parse_bool_setting(_strip_matching_quotes(m.group(1)))) is not None:
                    cfg.index_include_files = parsed_b
                    found["inc_files"] = True
                    break
        if not found["inc_deps"]:
            for k in _INC_DEPS_KEYS:
                m = re.search(rf"^[ \t]*{re.escape(k)}[ \t]*[:=][ \t]*([^\s#;]+)", content, re.MULTILINE | re.IGNORECASE)
                if m and (parsed_b := _parse_bool_setting(_strip_matching_quotes(m.group(1)))) is not None:
                    cfg.scope_include_deps = parsed_b
                    found["inc_deps"] = True
                    break

    return cfg
