# src/py_skeletonizer/config.py
"""
Role: フルコード保存対象、関数の保護リスト、およびバンドル出力に関する設定情報を保持・検証する。
"""
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set


@dataclass
class SkeletonConfig:
    full_code_paths: Set[Path] = field(default_factory=set)
    keep_functions: Set[str] = field(default_factory=set)
    only_nodes: Set[str] = field(default_factory=set)
    create_bundle: bool = True
    bundle_format: str = "txt"  # "txt", "xml" または "markdown"
    policy_path: Optional[Path] = None

    def is_full_code_path(self, target_path: Path) -> bool:
        for full_path in self.full_code_paths:
            if target_path == full_path or full_path in target_path.parents:
                return True
        return False


def _parse_command_file(file_path: Path) -> Optional[str]:
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
                    if k in data and isinstance(data[k], str) and data[k].strip():
                        return data[k].strip()
                if "scripts" in data and isinstance(data["scripts"], dict):
                    for k in ("test", "validation", "run"):
                        val = data["scripts"].get(k)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
        except Exception:
            pass

    if file_path.suffix.lower() == ".txt":
        for line in content.splitlines():
            s = line.strip()
            if s and not s.startswith(("#", "//", ";")):
                return s

    key_pattern = re.compile(
        r'^[ \t]*(?:test_command|test_cmd|command|cmd|test|run|exec|post_apply|testCommand)[ \t]*[:=][ \t]*["\']?(.*?)["\']?[ \t]*$',
        re.MULTILINE | re.IGNORECASE
    )
    m = key_pattern.search(content)
    if m:
        val = m.group(1).strip()
        if val:
            return val

    lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith(("#", "//", ";"))]
    if len(lines) == 1 and not lines[0].startswith(("[", "{")):
        return lines[0]

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
            "test_command": "",
            "description": "aiskel apply (-e / -T) 実行後に自動実行されるテスト・検証コマンドを記述してください",
        }
        config_path.write_text(json.dumps(config_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return config_path
    except Exception as e:
        print(f"⚠ 設定ファイルの自動生成に失敗しました: {e}", file=sys.stderr)
        return None


def load_test_command_from_meta(project_root: Path, output_dir: Optional[Path] = None) -> Optional[str]:
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
                    return f'"{target}"'
                return f'bash "{target}"' if target.suffix == ".sh" else f'"{target}"'

    return None