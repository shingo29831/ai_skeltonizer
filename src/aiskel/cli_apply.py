"""Module: @role: AIパッチの適用・リバート処理、対話式継続適用、テスト検証およびコミット実行を担当する。"""
import argparse
import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Set

from .cli_commit import (
    clear_pending_commit,
    execute_git_commit,
    save_pending_commit,
)
from .cli_interaction import (
    extract_commit_message,
    prompt_user_command,
    prompt_user_confirmation,
    rollback_modified_files,
)
from .cli_parser import parse_arguments
from .cli_sync import ensure_gitignore_updated
from .core.patch_parser import is_file_deletion_patch
from .config import ensure_default_config_file, load_test_command_from_meta
from .core.clipboard import set_clipboard_text
from .core.patch_applier import apply_patch
from .core.test_runner import run_validation_test


def get_clipboard_text() -> str:
    system = platform.system()
    try:
        if system == "Darwin":
            text = subprocess.check_output(["pbpaste"], encoding="utf-8")
        elif system == "Windows":
            text = subprocess.check_output(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw",
                ],
                encoding="utf-8",
            )
        elif system == "Linux":
            try:
                text = subprocess.check_output(["xclip", "-selection", "clipboard", "-o"], encoding="utf-8")
            except FileNotFoundError:
                text = subprocess.check_output(["xsel", "--clipboard", "--output"], encoding="utf-8")
        else:
            print(f"⚠ OS {system} のクリップボード自動取得は未対応です。", file=sys.stderr)
            return ""

        return text.replace("\r\n", "\n").replace("\r", "\n")
    except Exception as e:
        print(f"⚠ クリップボードの読み込みに失敗しました: {e}", file=sys.stderr)
        return ""


_COMMANDS_WITH_DIR_OPT = {"apply", "a", "revert", "r", "commit", "cm", "ci", "test", "t", "copy", "c", "cp"}
_BLOAT_VALUE_OPTS = {"-l", "--max-lines", "-c", "-C", "--max-chars", "-n", "--top", "--limit", "-e", "--exclude"}


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


def _has_applicable_patch_content(patch_text: str) -> bool:
    return bool(patch_text and (re.search(r"<{4,}", patch_text) or is_file_deletion_patch(patch_text)))


def _load_patch_text(
    parsed_args: argparse.Namespace,
    is_revert: bool,
    allow_stdin_fallback: bool = True,
) -> Optional[str]:
    action_name = "リバート" if is_revert else "適用"
    if parsed_args.patch_file:
        patch_file: Path = parsed_args.patch_file.resolve()
        if not patch_file.exists():
            print(f"エラー: パッチファイルが見つかりません: {patch_file}", file=sys.stderr)
            return None
        print(f"🚀 AIパッチの{action_name}を開始します (ファイル: {patch_file.name})")
        return patch_file.read_text(encoding="utf-8")
    if parsed_args.paste:
        if not allow_stdin_fallback and not sys.stdin.isatty():
            print("⚠ パイプ入力後の待機プロンプトでは -p/--paste を使用できません。クリップボード経由で 'aiskel a' を実行してください。", file=sys.stderr)
            return None
        print("🚀 AIの出力テキストをペーストしてください。")
        print("   (ペースト後、Windowsは Ctrl+Z を押してEnter、Mac/Linuxは Ctrl+D を押すと実行されます):")
        patch_text = sys.stdin.read()
        print(f"\n{action_name}を開始します...")
        return patch_text
    if allow_stdin_fallback and not sys.stdin.isatty():
        return sys.stdin.read()

    patch_text = get_clipboard_text()
    if not _has_applicable_patch_content(patch_text):
        if not patch_text:
            print("⚠ クリップボードが空です。")
        else:
            print("⚠ クリップボードに置換ブロック(<<<<)または削除指示が見つかりません。")
        if not allow_stdin_fallback:
            print("   修正パッチをコピーしてから再度 'aiskel a' を実行してください。")
            return None
        print("🚀 AIの出力テキストをペーストしてください。")
        print("   (ペースト後、Windowsは Ctrl+Z を押してEnter、Mac/Linuxは Ctrl+D を押すと実行されます):")
        patch_text = sys.stdin.read()
        print(f"\n{action_name}を開始します...")
        return patch_text

    print("🚀 クリップボードからAIの出力テキストを読み込みました。")
    return patch_text


def handle_apply_command(
    parsed_args: argparse.Namespace,
    on_auto_sync: Optional[Callable[[Path, str, bool], None]] = None,
    on_run_command: Optional[Callable[[List[str]], int]] = None,
) -> int:
    is_revert = getattr(parsed_args, "revert", False) or parsed_args.command in ("revert", "r")
    is_dry_run = getattr(parsed_args, "dry_run", False)
    test_option = getattr(parsed_args, "test", None) or getattr(parsed_args, "exec_cmd", None)
    project_root: Path = parsed_args.dir.resolve()
    if test_option is None and not getattr(parsed_args, "no_test", False):
        if load_test_command_from_meta(project_root):
            test_option = "auto"
    output_dir = project_root / "ai_meta"
    if not is_dry_run:
        ensure_gitignore_updated(project_root, output_dir)
    created_cfg = ensure_default_config_file(output_dir, project_root)
    if created_cfg:
        try:
            rel_cfg = created_cfg.relative_to(project_root)
        except ValueError:
            rel_cfg = created_cfg
        print(f"✨ 設定ファイルを自動作成しました: {rel_cfg}")

    patch_text = _load_patch_text(parsed_args, is_revert=is_revert, allow_stdin_fallback=True)
    if patch_text is None:
        return 1

    target_file = parsed_args.target.resolve() if parsed_args.target else None
    initial_commit_msg, patch_text = extract_commit_message(patch_text)

    if is_revert and initial_commit_msg:
        try:
            last_commit_msg = subprocess.check_output(
                ["git", "log", "-1", "--pretty=%B"], cwd=project_root, encoding="utf-8", stderr=subprocess.DEVNULL
            ).strip()
            if last_commit_msg.splitlines()[0].strip() == initial_commit_msg.splitlines()[0].strip():
                print(f"\n📦 直前のコミットが対象パッチのコミットと一致しました: {initial_commit_msg}")
                print("コミットを破棄(git reset --hard HEAD~1)して元に戻します...")
                subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=project_root, check=True)
                print("✔ コミットの破棄が完了しました。")
                if not getattr(parsed_args, "no_sync", False) and on_auto_sync:
                    on_auto_sync(project_root, "コミット破棄", False)
                return 0
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    total_modified_files: Set[Path] = set()
    total_success = 0
    total_skipped = 0
    last_fail = 0
    current_patch = patch_text
    active_force_replace = getattr(parsed_args, "force_replace", False)
    iteration = 0

    while True:
        iteration += 1
        patch_result = apply_patch(
            current_patch,
            project_root,
            target_file,
            force_replace=active_force_replace,
            revert=is_revert,
            dry_run=is_dry_run,
        )
        success, fail, skipped, modified_files = patch_result[0], patch_result[1], patch_result[2], patch_result[3]
        failed_details = getattr(patch_result, "failed_details", [])

        total_success += success
        total_skipped += skipped
        last_fail = fail
        total_modified_files.update(modified_files)

        action_name = "リバート" if is_revert else "適用"
        mode_prefix = "[DRY-RUN] " if is_dry_run else ""
        print(f"\n=== {mode_prefix}{action_name}結果 ===")
        iter_note = f" (累計: {total_success} 箇所)" if iteration > 1 else ""
        print(f"✔ 成功: {success} 箇所{iter_note}")
        if skipped > 0 or (iteration > 1 and total_skipped > 0):
            sk_note = f" (累計: {total_skipped} 箇所)" if iteration > 1 else ""
            print(f"⏭ スキップ (適用済み): {skipped} 箇所{sk_note}")
        if fail > 0:
            print(f"✖ 失敗: {fail} 箇所")
            if failed_details:
                print("\n【失敗箇所】")
                for d in failed_details:
                    d_lines = d.splitlines()
                    print(f"  - {d_lines[0]}")
                    for sub_line in d_lines[1:]:
                        print(f"    {sub_line}")

            if not is_dry_run and not is_revert:
                if total_success > 0:
                    header = f"提示されたパッチのうち一部のみ適用されました (成功: {total_success}箇所, 失敗: {fail}箇所)。"
                else:
                    header = f"提示されたパッチの適用に失敗しました (失敗: {fail}箇所)。"
                report_lines = [
                    header,
                    "以下の失敗箇所について、現在のコードと整合する修正パッチを再提示してください。\n",
                    "【失敗箇所】",
                ]
                if failed_details:
                    for d in failed_details:
                        d_lines = d.splitlines()
                        report_lines.append(f"- {d_lines[0]}")
                        for sub_line in d_lines[1:]:
                            report_lines.append(f"  {sub_line}")
                else:
                    report_lines.append("- 置換テキストの不一致または構文エラー")
                report_text = "\n".join(report_lines)
                try:
                    set_clipboard_text(report_text)
                    status_label = "一部適用の結果" if total_success > 0 else "適用失敗の結果"
                    print(f"📋 {status_label}と失敗箇所をクリップボードにコピーしました (AIチャットにそのままペースト可能)。")
                except Exception as e:
                    print(f"⚠ クリップボードへの保存に失敗しました: {e}", file=sys.stderr)

        if is_dry_run or is_revert or fail == 0:
            break

        if success > 0:
            print(f"\n⚠ 成功した {success} 箇所を変更しました。")

        next_patch_ready = False
        while True:
            raw_cmd = prompt_user_command(
                "\nコマンドを入力してください (aiskel c ..., aiskel a [-f], 終了: q / Enter): "
            )
            if raw_cmd is None:
                break
            cmd_str = raw_cmd.strip()
            if not cmd_str or cmd_str.lower() in ("q", "quit", "exit"):
                print("ℹ パッチの継続待機を終了しました。")
                break
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
                    next_args = parse_arguments(run_tokens)
                except SystemExit:
                    continue
                if getattr(next_args, "revert", False):
                    print("⚠ 継続待機中は --revert を実行できません。終了(q / Enter)後に実行してください。")
                    continue
                if next_args.dir.resolve() != project_root:
                    print(f"⚠ 待機セッション中は --dir を変更できません (現在: {project_root})。", file=sys.stderr)
                    continue

                loaded = _load_patch_text(next_args, is_revert=False, allow_stdin_fallback=False)
                if loaded is None:
                    continue

                extracted_commit, next_patch_body = extract_commit_message(loaded)
                if not next_patch_body.strip():
                    print("⚠ パッチ内容が空です。")
                    continue

                next_target = next_args.target.resolve() if next_args.target else target_file
                next_force = getattr(next_args, "force_replace", False) or getattr(parsed_args, "force_replace", False)

                if getattr(next_args, "dry_run", False):
                    dry_res = apply_patch(
                        next_patch_body,
                        project_root,
                        next_target,
                        force_replace=next_force,
                        revert=False,
                        dry_run=True,
                    )
                    print(f"\n=== [DRY-RUN] 適用結果 ===\n✔ 成功: {dry_res[0]} 箇所 / ✖ 失敗: {dry_res[1]} 箇所")
                    dry_failed = getattr(dry_res, "failed_details", [])
                    if dry_res[1] > 0 and dry_failed:
                        print("\n【失敗箇所】")
                        for d in dry_failed:
                            d_lines = d.splitlines()
                            print(f"  - {d_lines[0]}")
                            for sub_line in d_lines[1:]:
                                print(f"    {sub_line}")
                    continue

                if not initial_commit_msg and extracted_commit:
                    initial_commit_msg = extracted_commit
                current_patch = next_patch_body
                target_file = next_target
                active_force_replace = next_force
                next_test_opt = getattr(next_args, "test", None) or getattr(next_args, "exec_cmd", None)
                if next_test_opt is not None:
                    test_option = next_test_opt
                if getattr(next_args, "no_test", False):
                    test_option = None
                if getattr(next_args, "yes", False):
                    parsed_args.yes = True
                if getattr(next_args, "no_sync", False):
                    parsed_args.no_sync = True
                if getattr(next_args, "no_bloat", False):
                    parsed_args.no_bloat = True
                next_patch_ready = True
                break

            if subcmd in ("copy", "c", "cp", "test", "t", "bloat", "b", "size", "docs", "d"):
                _execute_subcommand(run_tokens, on_run_command)
            elif subcmd in ("revert", "r", "commit", "cm", "ci"):
                print(f"⚠ 継続待機中は '{subcmd}' を実行できません。終了(q / Enter)後に実行してください。")
            else:
                print(f"⚠ 未対応のコマンドです: '{subcmd}' (例: aiskel c ..., aiskel a [-f], 終了: q / Enter)")

        if not next_patch_ready:
            break

    commit_partial = False
    committed_successfully = False
    if not is_dry_run and not is_revert and last_fail > 0 and total_success > 0:
        commit_msg_preview = initial_commit_msg or "chore: apply patch"
        if initial_commit_msg:
            print(f"\n📦 コミットメッセージを検出しました: {commit_msg_preview}")
            commit_prompt = f"適用に成功した変更 ({total_success}箇所) をこのメッセージでコミットしますか？ [Enter/Space: y, 他: n]: "
        else:
            commit_prompt = f"\n適用に成功した変更 ({total_success}箇所) をコミットしますか？ [Enter/Space: y, 他: n]: "

        if prompt_user_confirmation(commit_prompt, default=False):
            commit_partial = True
        else:
            rollback_prompt = "これまでに適用した変更を元に戻しますか？ [Enter/Space: y, 他: n]: "
            if prompt_user_confirmation(rollback_prompt, default=False):
                rollback_modified_files(project_root, total_modified_files)
                clear_pending_commit(project_root, total_modified_files)
                total_modified_files.clear()
                total_success = 0
            else:
                save_pending_commit(project_root, initial_commit_msg, total_modified_files)
                print("ℹ コミットをスキップし、適用済みの変更はそのまま保持されています。")
                if initial_commit_msg:
                    print("💾 コミットメッセージを保持しました (後から 'aiskel commit' でコミットできます)")
                print("  (変更を取り消す場合は 'aiskel revert' または 'git checkout .' を実行してください)")

    if is_dry_run:
        return 0 if last_fail == 0 else 1

    if is_revert and total_success > 0:
        clear_pending_commit(project_root, total_modified_files)

    test_failed = False
    if not is_revert and test_option is not None and total_success > 0 and last_fail == 0:
        test_ok, test_output = run_validation_test(project_root, test_option)
        if test_ok:
            print("✅ 検証テストに成功しました。")
        else:
            test_failed = True
            print("\n❌ 検証テストが失敗しました。", file=sys.stderr)
            if test_output:
                print("\n--- テスト失敗ログ ---", file=sys.stderr)
                print(test_output, file=sys.stderr)
                print("----------------------\n", file=sys.stderr)

    if not is_revert and total_success > 0 and (last_fail == 0 or commit_partial):
        commit_msg = initial_commit_msg or "chore: apply patch"
        if initial_commit_msg or test_failed or commit_partial:
            if initial_commit_msg and not commit_partial:
                print(f"\n📦 コミットメッセージを検出しました: {commit_msg}")
            prompt_msg = (
                "テストが失敗しましたが、コミットを実行しますか？ [Enter/Space: y, 他: n]: "
                if test_failed
                else "このメッセージでコミットを実行しますか？ [Enter/Space: y, 他: n]: "
            )
            should_commit = True if commit_partial else (False if test_failed else getattr(parsed_args, "yes", False))
            if not should_commit:
                should_commit = prompt_user_confirmation(prompt_msg, default=False)

            if should_commit:
                print("コミットを実行します...")
                committed_successfully, had_diff, commit_err, _ = execute_git_commit(
                    project_root,
                    commit_msg,
                    target_files=total_modified_files,
                )
                if committed_successfully:
                    clear_pending_commit(project_root, total_modified_files)
                elif commit_err is not None:
                    print(f"⚠ コミットに失敗しました: {commit_err}")
                    save_pending_commit(project_root, initial_commit_msg, total_modified_files)
                    if initial_commit_msg:
                        print("💾 コミットメッセージを保持しました (修正後に 'aiskel commit' でコミットできます)")
                elif not had_diff:
                    print("ℹ 変更内容が既存コードと同一のため、コミットをスキップしました。")
                    clear_pending_commit(project_root, total_modified_files)
            else:
                print("ℹ コミットを拒否（スキップ）しました。")
                if test_failed:
                    rollback_prompt = "テストが失敗した変更を元に戻しますか？ [Enter/Space: y, 他: n]: "
                    if prompt_user_confirmation(rollback_prompt, default=False):
                        rollback_modified_files(project_root, total_modified_files)
                        clear_pending_commit(project_root, total_modified_files)
                        total_modified_files.clear()
                        total_success = 0
                        return 1
                save_pending_commit(project_root, initial_commit_msg, total_modified_files)
                print("ℹ コードの変更内容はそのまま保持されています。")
                if initial_commit_msg:
                    print("💾 コミットメッセージを保持しました (後から 'aiskel commit' でコミットできます)")
                print("  (変更を取り消す場合は 'aiskel revert' または 'git checkout .' を実行してください)")
                if test_failed:
                    return 1
        else:
            save_pending_commit(project_root, None, total_modified_files)

    if total_success > 0 and (last_fail == 0 or commit_partial) and not getattr(parsed_args, "no_sync", False):
        action_name = "リバート" if is_revert else "置換"
        should_bloat = not is_revert and not getattr(parsed_args, "no_bloat", False)
        if on_auto_sync:
            on_auto_sync(project_root, action_name, should_bloat)

    return 0 if (last_fail == 0 or committed_successfully) else 1
