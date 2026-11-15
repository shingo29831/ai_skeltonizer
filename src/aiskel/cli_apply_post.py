"""Module: @role: パッチ適用後の直前コミット破棄、検証テスト実行、コミット確定およびロールバック処理を担当する。"""
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple, Union

from .cli_commit import (
    clear_pending_commit,
    execute_git_commit,
    save_pending_commit,
)
from .cli_interaction import (
    prompt_user_confirmation,
    rollback_modified_files,
)
from .core.test_runner import run_validation_test


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
) -> Tuple[bool, int]:
    confirm = confirm_fn or prompt_user_confirmation
    rollback = rollback_fn or rollback_modified_files

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
) -> Tuple[bool, bool]:
    confirm = confirm_fn or prompt_user_confirmation
    commit_exec = commit_fn or execute_git_commit
    rollback = rollback_fn or rollback_modified_files

    commit_msg = initial_commit_msg or "chore: apply patch"
    if not (initial_commit_msg or test_failed or commit_partial):
        save_pending_commit(project_root, None, total_modified_files)
        return False, False

    if initial_commit_msg and not commit_partial:
        print(f"\n📦 コミットメッセージを検出しました: {commit_msg}")
    prompt_msg = (
        "テストが失敗しましたが、コミットを実行しますか？ [Enter/Space: y, 他: n]: "
        if test_failed
        else "このメッセージでコミットを実行しますか？ [Enter/Space: y, 他: n]: "
    )
    should_commit = True if commit_partial else (False if test_failed else auto_yes)
    if not should_commit:
        should_commit = confirm(prompt_msg, default=False)

    if should_commit:
        print("コミットを実行します...")
        committed_successfully, had_diff, commit_err, _ = commit_exec(
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
        return committed_successfully, False

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
