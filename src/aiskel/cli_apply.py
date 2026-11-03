"""Module: @role: AIパッチの適用・リバート処理、対話式継続適用、テスト検証およびコミット実行を担当する。"""
import argparse
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Set

from .cli_interaction import (
    extract_commit_message,
    prompt_user_confirmation,
    rollback_modified_files,
)
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


def handle_apply_command(
    parsed_args: argparse.Namespace,
    on_auto_sync: Optional[Callable[[Path, str, bool], None]] = None,
) -> int:
    is_revert = getattr(parsed_args, "revert", False) or parsed_args.command in ("revert", "r")
    is_dry_run = getattr(parsed_args, "dry_run", False)
    test_option = getattr(parsed_args, "test", None) or getattr(parsed_args, "exec_cmd", None)
    project_root: Path = parsed_args.dir.resolve()
    if test_option is None and not getattr(parsed_args, "no_test", False):
        if load_test_command_from_meta(project_root):
            test_option = "auto"
    output_dir = project_root / "ai_meta"
    created_cfg = ensure_default_config_file(output_dir, project_root)
    if created_cfg:
        try:
            rel_cfg = created_cfg.relative_to(project_root)
        except ValueError:
            rel_cfg = created_cfg
        print(f"✨ 設定ファイルを自動作成しました: {rel_cfg}")

    if parsed_args.patch_file:
        patch_file: Path = parsed_args.patch_file.resolve()
        if not patch_file.exists():
            print(f"エラー: パッチファイルが見つかりません: {patch_file}", file=sys.stderr)
            return 1
        action_name = "リバート" if is_revert else "適用"
        print(f"🚀 AIパッチの{action_name}を開始します (ファイル: {patch_file.name})")
        patch_text = patch_file.read_text(encoding="utf-8")
    elif parsed_args.paste:
        action_name = "リバート" if is_revert else "適用"
        print("🚀 AIの出力テキストをペーストしてください。")
        print("   (ペースト後、Windowsは Ctrl+Z を押してEnter、Mac/Linuxは Ctrl+D を押すと実行されます):")
        patch_text = sys.stdin.read()
        print(f"\n{action_name}を開始します...")
    elif not sys.stdin.isatty():
        patch_text = sys.stdin.read()
    else:
        patch_text = get_clipboard_text()
        if not patch_text or not re.search(r"<{4,}", patch_text):
            if not patch_text:
                print("⚠ クリップボードが空です。")
            else:
                print("⚠ クリップボードに置換ブロック(<<<<)が見つかりません。")
            action_name = "リバート" if is_revert else "適用"
            print("🚀 AIの出力テキストをペーストしてください。")
            print("   (ペースト後、Windowsは Ctrl+Z を押してEnter、Mac/Linuxは Ctrl+D を押すと実行されます):")
            patch_text = sys.stdin.read()
            print(f"\n{action_name}を開始します...")
        else:
            print("🚀 クリップボードからAIの出力テキストを読み込みました。")

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
    iteration = 0

    while True:
        iteration += 1
        patch_result = apply_patch(
            current_patch,
            project_root,
            target_file,
            force_replace=getattr(parsed_args, "force_replace", False),
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

        retry_prompt = "\n修正パッチをコピーして、続けて適用しますか？ [Enter/Space: y, 他: n]: "
        if not prompt_user_confirmation(retry_prompt, default=False):
            print("ℹ パッチの継続適用を終了しました。")
            break

        new_text = get_clipboard_text()
        while not new_text or not re.search(r"<{4,}", new_text):
            if not new_text:
                print("⚠ クリップボードが空です。修正したパッチをコピーしてください。")
            else:
                print("⚠ クリップボードに置換ブロック(<<<<)が見つかりません。")
            if not prompt_user_confirmation("クリップボードを再取得しますか？ [Enter/Space: y, 他: n]: ", default=True):
                break
            new_text = get_clipboard_text()

        if not new_text or not re.search(r"<{4,}", new_text):
            print("ℹ パッチの継続適用を終了しました。")
            break

        extracted_commit, current_patch = extract_commit_message(new_text)
        if not initial_commit_msg and extracted_commit:
            initial_commit_msg = extracted_commit
        print("🚀 クリップボードから修正パッチを読み込みました。続けて適用します...")

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
                total_modified_files.clear()
                total_success = 0
            else:
                print("ℹ コミットをスキップし、適用済みの変更はそのまま保持されています。")
                print("  (変更を取り消す場合は 'aiskel revert' または 'git checkout .' を実行してください)")

    if is_dry_run:
        return 0 if last_fail == 0 else 1

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
                try:
                    for f in total_modified_files:
                        subprocess.run(["git", "add", str(f)], cwd=project_root, check=True)
                    # 差分ゼロ時のgit commit失敗(nothing added to commit)を防止
                    staged_diff = subprocess.check_output(
                        ["git", "diff", "--cached", "--name-only"], cwd=project_root, encoding="utf-8"
                    ).strip()
                    if staged_diff:
                        subprocess.run(["git", "commit", "-m", commit_msg], cwd=project_root, check=True)
                        print("✔ コミットが完了しました。")
                        committed_successfully = True
                        push_prompt = "リモートリポジトリにプッシュ (git push) しますか？ [Enter/Space: y, 他: n]: "
                        if prompt_user_confirmation(push_prompt, default=False):
                            print("プッシュを実行中...")
                            push_res = subprocess.run(["git", "push"], cwd=project_root)
                            if push_res.returncode == 0:
                                print("✔ プッシュが完了しました。")
                            else:
                                print("⚠ プッシュに失敗しました（上記のエラーを確認してください）。", file=sys.stderr)
                        else:
                            print("ℹ プッシュをスキップしました。")
                    else:
                        print("ℹ 変更内容が既存コードと同一のため、コミットをスキップしました。")
                except (subprocess.CalledProcessError, FileNotFoundError) as e:
                    print(f"⚠ コミットに失敗しました: {e}")
            else:
                print("ℹ コミットを拒否（スキップ）しました。")
                if test_failed:
                    rollback_prompt = "テストが失敗した変更を元に戻しますか？ [Enter/Space: y, 他: n]: "
                    if prompt_user_confirmation(rollback_prompt, default=False):
                        rollback_modified_files(project_root, total_modified_files)
                        total_modified_files.clear()
                        total_success = 0
                        return 1
                print("ℹ コードの変更内容はそのまま保持されています。")
                print("  (変更を取り消す場合は 'aiskel revert' または 'git checkout .' を実行してください)")
                if test_failed:
                    return 1

    if total_success > 0 and (last_fail == 0 or commit_partial) and not getattr(parsed_args, "no_sync", False):
        action_name = "リバート" if is_revert else "置換"
        should_bloat = not is_revert and not getattr(parsed_args, "no_bloat", False)
        if on_auto_sync:
            on_auto_sync(project_root, action_name, should_bloat)

    return 0 if (last_fail == 0 or committed_successfully) else 1
