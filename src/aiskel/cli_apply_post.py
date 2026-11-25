"""Module: @role: パッチ適用後の直前コミット破棄、検証テスト実行、コミット確定およびロールバック処理を担当する。"""
import inspect
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

from .cli_apply_io import (
    _format_command_summary,
    copy_static_analysis_failure_report_to_clipboard,
)
from .cli_commit import (
    clear_pending_commit,
    execute_git_commit,
    save_pending_commit,
)
from .cli_interaction import (
    prompt_user_confirmation,
    rollback_modified_files,
)
from .core.clipboard import set_clipboard_text
from .core.test_runner import run_static_analysis, run_validation_test


def try_revert_last_commit(
    project_root: Path,
    initial_commit_msg: Optional[str],
    no_sync: bool,
    on_auto_sync: Optional[Callable[[Path, str, bool], None]] = None,
) -> bool:
    if not initial_commit_msg or not initial_commit_msg.splitlines():
        return False
    try:
        last_commit_msg = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=project_root,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
        ).strip()
        if last_commit_msg and last_commit_msg.splitlines()[0].strip() == initial_commit_msg.splitlines()[0].strip():
            print(f"\n📦 直前のコミットが対象パッチのコミットと一致しました: {initial_commit_msg}")
            print("コミットを破棄(git reset --hard HEAD~1)して元に戻します...")
            subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=project_root, check=True)
            print("✔ コミットの破棄が完了しました。")
            if not no_sync and on_auto_sync:
                on_auto_sync(project_root, "コミット破棄", False)
            return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return False


def _save_and_announce_pending_commit(
    project_root: Path,
    commit_msg: Optional[str],
    modified_files: Set[Path],
    status_notice: str,
) -> None:
    save_pending_commit(project_root, commit_msg, modified_files)
    print(status_notice)
    if commit_msg:
        print("💾 コミットメッセージを保持しました (後から 'aiskel commit' でコミットできます)")
    print("  (変更を取り消す場合は 'aiskel revert' または 'git checkout .' を実行してください)")


def handle_partial_success(
    project_root: Path,
    initial_commit_msg: Optional[str],
    total_success: int,
    total_modified_files: Set[Path],
    confirm_fn: Optional[Callable[..., bool]] = None,
    rollback_fn: Optional[Callable[[Path, Set[Path]], bool]] = None,
    non_interactive: bool = False,
) -> Tuple[bool, int]:
    confirm = confirm_fn or prompt_user_confirmation
    rollback = rollback_fn or rollback_modified_files

    if not non_interactive:
        commit_msg_preview = initial_commit_msg or "chore: apply patch"
        if initial_commit_msg:
            print(f"\n📦 コミットメッセージを検出しました: {commit_msg_preview}")
            commit_prompt = f"適用に成功した変更 ({total_success}箇所) をこのメッセージでコミットしますか？ [Enter/Space: y, 他: n]: "
        else:
            commit_prompt = f"\n適用に成功した変更 ({total_success}箇所) をコミットしますか？ [Enter/Space: y, 他: n]: "

        if confirm(commit_prompt, default=False):
            return True, total_success

        rollback_prompt = "これまでに適用した変更を元に戻しますか？ [Enter/Space: y, 他: n]: "
        if confirm(rollback_prompt, default=False):
            rollback(project_root, total_modified_files)
            clear_pending_commit(project_root, total_modified_files)
            total_modified_files.clear()
            return False, 0

    _save_and_announce_pending_commit(
        project_root,
        initial_commit_msg,
        total_modified_files,
        "ℹ コミットをスキップし、適用済みの変更はそのまま保持されています。",
    )
    return False, total_success


def run_post_apply_validation(
    project_root: Path,
    test_option: Union[str, List[str]],
    test_runner: Optional[Callable[..., Tuple[bool, str]]] = None,
) -> bool:
    runner = test_runner or run_validation_test
    test_ok, test_output = runner(project_root, test_option)
    if test_ok:
        print("✅ 検証テストに成功しました。")
        return False

    print("\n❌ 検証テストが失敗しました。", file=sys.stderr)
    if test_output:
        print("\n--- テスト失敗ログ ---", file=sys.stderr)
        print(test_output, file=sys.stderr)
        print("----------------------\n", file=sys.stderr)
    return True

def _invoke_rollback(
    rollback_exec: Callable[..., bool],
    project_root: Path,
    modified_files: Set[Path],
    snapshot: Optional[Dict[Path, Optional[bytes]]] = None,
) -> bool:
    if snapshot:
        try:
            params = inspect.signature(rollback_exec).parameters
            supports_snapshot = "snapshot" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):
            supports_snapshot = False
        if supports_snapshot:
            return rollback_exec(project_root, modified_files, snapshot=snapshot)
    return rollback_exec(project_root, modified_files)

def run_post_apply_static_analysis(
    project_root: Path,
    static_commands: Union[str, List[str]],
    total_modified_files: Set[Path],
    *,
    snapshot: Optional[Dict[Path, Optional[bytes]]] = None,
    static_runner: Optional[Callable[..., Tuple[bool, str]]] = None,
    rollback_fn: Optional[Callable[..., bool]] = None,
    clipboard_writer: Optional[Callable[[str], None]] = None,
    non_interactive: bool = False,
) -> bool:
    """静的コード解析を実行し、失敗時は変更を元に戻してエラー内容をプロンプトへ出力・保存する。"""
    runner = static_runner or run_static_analysis
    rollback = rollback_fn or rollback_modified_files
    writer = clipboard_writer or set_clipboard_text

    ok, output = runner(project_root, static_commands)
    if ok:
        print("✅ 静的コード解析に成功しました。")
        return False

    cmd_summary = _format_command_summary(static_commands)

    print("\n❌ 静的コード解析が失敗しました。変更を元に戻します。", file=sys.stderr)
    if cmd_summary:
        print(f"実行コマンド: {cmd_summary}", file=sys.stderr)
    if output:
        print("\n--- 静的コード解析エラーログ ---", file=sys.stderr)
        print(output, file=sys.stderr)
        print("--------------------------------\n", file=sys.stderr)

    _invoke_rollback(rollback, project_root, total_modified_files, snapshot=snapshot)
    clear_pending_commit(project_root, total_modified_files)
    total_modified_files.clear()

    if not non_interactive:
        copy_static_analysis_failure_report_to_clipboard(
            static_commands,
            output,
            clipboard_writer=writer,
        )
    return True


def _invoke_commit_exec(
    commit_exec: Callable[..., Tuple[bool, bool, Optional[str], bool]],
    project_root: Path,
    commit_msg: str,
    target_files: Set[Path],
    skip_push: bool,
) -> Tuple[bool, bool, Optional[str], bool]:
    # 既存のテスト用commit_fnモックとの引数互換性を保ちつつプッシュを抑止する
    if skip_push:
        try:
            params = inspect.signature(commit_exec).parameters
            supports_no_push = "no_push" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):
            supports_no_push = False
        if supports_no_push:
            return commit_exec(
                project_root,
                commit_msg,
                target_files=target_files,
                no_push=True,
            )
    return commit_exec(
        project_root,
        commit_msg,
        target_files=target_files,
    )

def finalize_apply_commit(
    project_root: Path,
    initial_commit_msg: Optional[str],
    total_modified_files: Set[Path],
    test_failed: bool,
    commit_partial: bool,
    auto_yes: bool,
    confirm_fn: Optional[Callable[..., bool]] = None,
    commit_fn: Optional[Callable[..., Tuple[bool, bool, Optional[str], bool]]] = None,
    rollback_fn: Optional[Callable[[Path, Set[Path]], bool]] = None,
    non_interactive: bool = False,
    no_push: bool = False,
) -> Tuple[bool, bool]:
    confirm = confirm_fn or prompt_user_confirmation
    commit_exec = commit_fn or execute_git_commit
    rollback = rollback_fn or rollback_modified_files

    commit_msg = initial_commit_msg or "chore: apply patch"
    # -y指定時はcommitタグ不在でもデフォルトメッセージで自動コミットを成立させる
    if not (initial_commit_msg or test_failed or commit_partial or auto_yes):
        save_pending_commit(project_root, None, total_modified_files)
        return False, False

    if initial_commit_msg and not commit_partial:
        print(f"\n📦 コミットメッセージを検出しました: {commit_msg}")
    elif auto_yes and not initial_commit_msg and not test_failed:
        print(f"\n📦 コミットメッセージ未指定のためデフォルトメッセージを使用します: {commit_msg}")
    prompt_msg = (
        "テストが失敗しましたが、コミットを実行しますか？ [Enter/Space: y, 他: n]: "
        if test_failed
        else "このメッセージでコミットを実行しますか？ [Enter/Space: y, 他: n]: "
    )
    should_commit = True if commit_partial else (False if test_failed else auto_yes)
    if not should_commit and not non_interactive:
        should_commit = confirm(prompt_msg, default=False)

    if should_commit:
        print("コミットを実行します...")
        committed_successfully, had_diff, commit_err, _ = _invoke_commit_exec(
            commit_exec,
            project_root,
            commit_msg,
            total_modified_files,
            skip_push=(no_push or non_interactive),
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
        return committed_successfully, False

    if non_interactive:
        notice = (
            "ℹ 非対話モードのためテスト失敗後の変更を保持しコミットを保留しました。"
            if test_failed
            else "ℹ 非対話モードのため変更を適用しコミットは保留しました。"
        )
        _save_and_announce_pending_commit(
            project_root,
            initial_commit_msg,
            total_modified_files,
            notice,
        )
        return False, test_failed

    print("ℹ コミットを拒否（スキップ）しました。")
    if test_failed:
        rollback_prompt = "テストが失敗した変更を元に戻しますか？ [Enter/Space: y, 他: n]: "
        if confirm(rollback_prompt, default=False):
            rollback(project_root, total_modified_files)
            clear_pending_commit(project_root, total_modified_files)
            total_modified_files.clear()
            return False, True

    _save_and_announce_pending_commit(
        project_root,
        initial_commit_msg,
        total_modified_files,
        "ℹ コードの変更内容はそのまま保持されています。",
    )
    return False, test_failed
