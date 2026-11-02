"""Module: @role: プロジェクト検証テストコマンドの実行、ワーキングディレクトリ追従、および実行ログ集約を担当する。"""
import os
import sys
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union

from ..config import load_test_command_from_meta


def try_handle_cd_command(
    cmd: str, current_dir: Path, dir_stack: List[Path], prev_dir: Optional[Path] = None
) -> Tuple[bool, bool, Path, Optional[Path], str]:
    # なぜ必要か: cd等によるワーキングディレクトリ変更を後続コマンドへ確実に引き継ぐため
    trimmed = cmd.strip()
    if not trimmed or any(op in trimmed for op in ("&&", "||", "|", ";", ">", "<")):
        return False, True, current_dir, prev_dir, ""

    parts = trimmed.split(None, 1)
    op = parts[0].lower()

    if op in ("cd", "chdir"):
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower().startswith("/d"):
            arg = arg[2:].strip()
        if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'")):
            arg = arg[1:-1].strip()

        if not arg:
            return True, True, current_dir, prev_dir, f"カレントディレクトリ: {current_dir}"

        if arg == "-":
            if prev_dir is None:
                return True, False, current_dir, prev_dir, "直前のディレクトリ (OLDPWD) が設定されていません。"
            return True, True, prev_dir, current_dir, f"直前のディレクトリに戻りました: {prev_dir}"

        target = Path(arg).expanduser() if arg.startswith("~") else Path(arg)
        resolved = (current_dir / target).resolve() if not target.is_absolute() else target.resolve()

        if not resolved.exists():
            return True, False, current_dir, prev_dir, f"指定されたディレクトリが存在しません: {arg} (解決先: {resolved})"
        if not resolved.is_dir():
            return True, False, current_dir, prev_dir, f"指定されたパスはディレクトリではありません: {arg} (解決先: {resolved})"

        return True, True, resolved, current_dir, f"ディレクトリを移動しました: {resolved}"

    if op == "pushd":
        arg = parts[1].strip() if len(parts) > 1 else ""
        if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'")):
            arg = arg[1:-1].strip()
        if not arg:
            return True, False, current_dir, prev_dir, "pushd には移動先ディレクトリの指定が必要です。"

        target = Path(arg).expanduser() if arg.startswith("~") else Path(arg)
        resolved = (current_dir / target).resolve() if not target.is_absolute() else target.resolve()
        if not resolved.exists() or not resolved.is_dir():
            return True, False, current_dir, prev_dir, f"指定されたディレクトリが存在しません: {arg} (解決先: {resolved})"

        dir_stack.append(current_dir)
        return True, True, resolved, current_dir, f"ディレクトリを移動しました (pushd): {resolved}"

    if op == "popd":
        if not dir_stack:
            return True, False, current_dir, prev_dir, "ディレクトリスタックが空のため popd を実行できません。"
        popped = dir_stack.pop()
        if not popped.exists() or not popped.is_dir():
            return True, False, current_dir, prev_dir, f"スタック内のディレクトリが存在しません: {popped}"
        return True, True, popped, current_dir, f"ディレクトリを復帰しました (popd): {popped}"

    return False, True, current_dir, prev_dir, ""


def run_validation_test(
    project_root: Path,
    test_command: Optional[Union[str, List[str]]],
    timeout_sec: int = 60,
    verbose: bool = False
) -> Tuple[bool, str]:
    """検証テストを実行し、成否と実行ログを返す。"""
    commands: List[str] = []
    if not test_command or test_command == "auto":
        # なぜ必要か: ai_metaの設定ファイルに指定されたコマンドを最優先し、未指定時のみ自動検出へ倒すため
        meta_cmds = load_test_command_from_meta(project_root)
        if meta_cmds:
            commands = meta_cmds
            cmd_summary = " && ".join(commands) if len(commands) > 1 else commands[0]
            print(f"⚙ ai_meta の設定ファイルからコマンドを読み込みました ({len(commands)}件): {cmd_summary}")
        else:
            venv_pytest = None
            for venv_name in (".venv", "venv", "env"):
                scripts_dir = project_root / venv_name / ("Scripts" if sys.platform == "win32" else "bin")
                pt = scripts_dir / ("pytest.exe" if sys.platform == "win32" else "pytest")
                if pt.exists():
                    venv_pytest = f'"{pt}"'
                    break

            if (project_root / "pyproject.toml").exists() or (project_root / "pytest.ini").exists() or (project_root / "tests").exists():
                detected = venv_pytest or f'"{sys.executable}" -m pytest'
            elif (project_root / "package.json").exists():
                detected = "npm test"
            elif (project_root / "Cargo.toml").exists():
                detected = "cargo test"
            elif (project_root / "go.mod").exists():
                detected = "go test ./..."
            else:
                detected = venv_pytest or f'"{sys.executable}" -m pytest'
            commands = [detected]
    elif isinstance(test_command, list):
        commands = [c.strip() for c in test_command if c.strip()]
    else:
        commands = [c.strip() for c in test_command.splitlines() if c.strip() and not c.strip().startswith(("#", "//", ";"))]

    output_logs: List[str] = []
    total = len(commands)
    current_dir = project_root
    dir_stack: List[Path] = []
    prev_dir: Optional[Path] = None

    for idx, cmd in enumerate(commands, 1):
        step_label = f"[{idx}/{total}] " if total > 1 else ""
        print(f"\n🧪 検証テストを実行中: {step_label}{cmd}")

        is_cd, ok, new_dir, new_prev, msg = try_handle_cd_command(cmd, current_dir, dir_stack, prev_dir)
        if is_cd:
            header = f"=== {step_label}{cmd} ===\n" if total > 1 else ""
            if not ok:
                output_logs.append(f"{header}エラー: {msg}\n")
                if verbose:
                    print(f"✖ エラー: {msg}", file=sys.stderr)
                return False, "\n".join(output_logs)
            current_dir, prev_dir = new_dir, new_prev
            output_logs.append(f"{header}{msg}\n")
            if verbose and msg:
                print(f"✔ {msg}")
            continue

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=current_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
            )
            header = f"=== {step_label}{cmd} ===\n" if total > 1 else ""
            output_logs.append(f"{header}{proc.stdout}")
            if verbose and proc.stdout:
                sys.stdout.write(proc.stdout)
                if not proc.stdout.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
            if proc.returncode != 0:
                return False, "\n".join(output_logs)
        except subprocess.TimeoutExpired:
            return False, f"テスト実行がタイムアウトしました ({timeout_sec}秒): {cmd}"
        except Exception as e:
            return False, f"テスト実行コマンドの起動に失敗しました: {cmd} ({e})"

    return True, "\n".join(output_logs)
