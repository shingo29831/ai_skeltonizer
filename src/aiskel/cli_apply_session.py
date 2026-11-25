"""Module: @role: パッチ適用失敗時の対話式継続待機ループ、入力コマンド解析、およびサブコマンド再実行を担当する。"""
import argparse
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from .cli_apply_io import load_patch_text, print_failed_details
from .cli_interaction import extract_commit_message, prompt_user_command
from .cli_parser import parse_arguments
from .core.patch_applier import apply_patch

_COMMANDS_WITH_DIR_OPT = {"apply", "a", "revert", "r", "commit", "cm", "ci", "test", "t", "copy", "c", "cp"}
_BLOAT_VALUE_OPTS = {"-l", "--max-lines", "-c", "-C", "--max-chars", "-n", "--top", "--limit", "-e", "--exclude"}
_PASS_THROUGH_SUBCOMMANDS = {"copy", "c", "cp", "test", "t", "bloat", "b", "size", "docs", "d"}
_BLOCKED_IN_SESSION_SUBCOMMANDS = {"revert", "r", "commit", "cm", "ci"}


@dataclass
class ApplySessionState:
    current_patch: str
    target_file: Optional[Path]
    active_force_replace: bool
    base_force_replace: bool
    initial_commit_msg: Optional[str]
    test_option: Optional[Union[str, List[str]]]
    yes: bool
    no_sync: bool
    no_bloat: bool
    no_push: bool = False


def _strip_outer_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    return text


def _split_command_args(cmd_str: str) -> Optional[List[str]]:
    # なぜ必要か: Windowsパスのバックスラッシュがshlexで消失するのを防ぐため
    is_posix = os.name != "nt"
    try:
        tokens = shlex.split(cmd_str, posix=is_posix)
    except ValueError as e:
        print(f"⚠ コマンドの構文が不正です: {e}", file=sys.stderr)
        return None
    if not is_posix:
        cleaned = []
        for tok in tokens:
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
                cleaned.append(tok[1:-1])
            elif "=" in tok and tok.startswith("-"):
                opt, val = tok.split("=", 1)
                cleaned.append(f"{opt}={_strip_outer_quotes(val)}")
            else:
                cleaned.append(tok)
        return cleaned
    return tokens


def _inject_project_root_if_needed(tokens: List[str], project_root: Path) -> List[str]:
    if not tokens or project_root == Path(".").resolve():
        return tokens
    cmd = tokens[0]
    if cmd in _COMMANDS_WITH_DIR_OPT:
        if any(t == "--dir" or t.startswith("--dir=") for t in tokens[1:]):
            return tokens
        return [cmd, "--dir", str(project_root)] + tokens[1:]
    if cmd in ("bloat", "b", "size"):
        i = 1
        has_pos = False
        while i < len(tokens):
            t = tokens[i]
            if t in _BLOAT_VALUE_OPTS:
                i += 2
            elif t.startswith("-"):
                i += 1
            else:
                has_pos = True
                break
        if not has_pos:
            return tokens + [str(project_root)]
    return tokens


def _execute_subcommand(
    tokens: List[str],
    on_run_command: Optional[Callable[[List[str]], int]],
) -> int:
    runner = on_run_command
    if runner is None:
        from .cli import main as cli_main
        runner = cli_main
    try:
        return runner(tokens)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def _run_dry_run_preview(
    patch_body: str,
    project_root: Path,
    target_file: Optional[Path],
    force_replace: bool,
    apply_patch_fn: Callable[..., Any],
) -> None:
    dry_res = apply_patch_fn(
        patch_body,
        project_root,
        target_file,
        force_replace=force_replace,
        revert=False,
        dry_run=True,
    )
    print(f"\n=== [DRY-RUN] 適用結果 ===\n✔ 成功: {dry_res[0]} 箇所 / ✖ 失敗: {dry_res[1]} 箇所")
    dry_failed = getattr(dry_res, "failed_details", [])
    if dry_res[1] > 0 and dry_failed:
        print_failed_details(dry_failed)


def wait_for_next_patch(
    project_root: Path,
    state: ApplySessionState,
    on_run_command: Optional[Callable[[List[str]], int]] = None,
    prompt_command_fn: Optional[Callable[[str], Optional[str]]] = None,
    clipboard_reader: Optional[Callable[[], str]] = None,
    apply_patch_fn: Optional[Callable[..., Any]] = None,
    parse_args_fn: Optional[Callable[[Optional[List[str]]], argparse.Namespace]] = None,
) -> bool:
    prompt_cmd = prompt_command_fn or prompt_user_command
    patch_fn = apply_patch_fn or apply_patch
    parse_fn = parse_args_fn or parse_arguments

    while True:
        raw_cmd = prompt_cmd(
            "\nコマンドを入力してください (aiskel c ..., aiskel a [-f], 終了: q / Enter): "
        )
        if raw_cmd is None:
            return False
        cmd_str = raw_cmd.strip()
        if not cmd_str or cmd_str.lower() in ("q", "quit", "exit"):
            print("ℹ パッチの継続待機を終了しました。")
            return False
        if cmd_str.startswith("```"):
            continue

        tokens = _split_command_args(cmd_str)
        if not tokens:
            continue
        if tokens[0] in ("$", ">"):
            tokens = tokens[1:]
        if tokens and tokens[0].lower() == "aiskel":
            tokens = tokens[1:]
        if not tokens:
            continue

        run_tokens = _inject_project_root_if_needed(tokens, project_root)
        subcmd = run_tokens[0]
        if subcmd in ("apply", "a"):
            try:
                next_args = parse_fn(run_tokens)
            except SystemExit:
                continue
            if getattr(next_args, "revert", False):
                print("⚠ 継続待機中は --revert を実行できません。終了(q / Enter)後に実行してください。")
                continue
            if next_args.dir.resolve() != project_root:
                print(f"⚠ 待機セッション中は --dir を変更できません (現在: {project_root})。", file=sys.stderr)
                continue

            loaded = load_patch_text(
                next_args,
                is_revert=False,
                allow_stdin_fallback=False,
                clipboard_reader=clipboard_reader,
            )
            if loaded is None:
                continue

            extracted_commit, next_patch_body = extract_commit_message(loaded)
            if not next_patch_body.strip():
                print("⚠ パッチ内容が空です。")
                continue

            next_target = next_args.target.resolve() if next_args.target else state.target_file
            next_force = getattr(next_args, "force_replace", False) or state.base_force_replace

            if getattr(next_args, "dry_run", False):
                _run_dry_run_preview(next_patch_body, project_root, next_target, next_force, patch_fn)
                continue

            if not state.initial_commit_msg and extracted_commit:
                state.initial_commit_msg = extracted_commit
            state.current_patch = next_patch_body
            state.target_file = next_target
            state.active_force_replace = next_force

            next_test_opt = getattr(next_args, "test", None) or getattr(next_args, "exec_cmd", None)
            if next_test_opt is not None:
                state.test_option = next_test_opt
            if getattr(next_args, "no_test", False):
                state.test_option = None
            if getattr(next_args, "yes", False):
                state.yes = True
            if getattr(next_args, "no_push", False):
                state.no_push = True
            if getattr(next_args, "no_sync", False):
                state.no_sync = True
            if getattr(next_args, "no_bloat", False):
                state.no_bloat = True
            return True

        if subcmd in _PASS_THROUGH_SUBCOMMANDS:
            _execute_subcommand(run_tokens, on_run_command)
        elif subcmd in _BLOCKED_IN_SESSION_SUBCOMMANDS:
            print(f"⚠ 継続待機中は '{subcmd}' を実行できません。終了(q / Enter)後に実行してください。")
        else:
            print(f"⚠ 未対応のコマンドです: '{subcmd}' (例: aiskel c ..., aiskel a [-f], 終了: q / Enter)")
