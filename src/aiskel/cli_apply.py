"""Module: @role: AIパッチの適用・リバート実行フロー全体の統括(オーケストレーション)を担当する。"""
import argparse
from pathlib import Path
from typing import Callable, List, Optional, Set

from .cli_apply_io import (
    has_applicable_patch_content as _has_applicable_patch_content,
    load_patch_text as _load_patch_text,
    report_patch_result,
)
from .cli_apply_post import (
    finalize_apply_commit,
    handle_partial_success,
    run_post_apply_validation,
    try_revert_last_commit,
)
from .cli_apply_session import (
    ApplySessionState,
    _execute_subcommand,
    _inject_project_root_if_needed,
    _split_command_args,
    _strip_outer_quotes,
    wait_for_next_patch,
)
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
from .config import ensure_default_config_file, load_test_command_from_meta
from .core.clipboard import get_clipboard_text, set_clipboard_text
from .core.patch_applier import apply_patch
from .core.test_runner import run_validation_test

__all__ = [
    "get_clipboard_text",
    "handle_apply_command",
    "_strip_outer_quotes",
    "_split_command_args",
    "_inject_project_root_if_needed",
    "_execute_subcommand",
    "_has_applicable_patch_content",
    "_load_patch_text",
]


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

    is_non_interactive = getattr(parsed_args, "non_interactive", False)
    patch_text = _load_patch_text(
        parsed_args,
        is_revert=is_revert,
        allow_stdin_fallback=True,
        clipboard_reader=get_clipboard_text,
        non_interactive=is_non_interactive,
    )
    if patch_text is None:
        return 1

    target_file = parsed_args.target.resolve() if parsed_args.target else None
    initial_commit_msg, patch_text = extract_commit_message(patch_text)

    if is_revert and not is_dry_run and try_revert_last_commit(
        project_root,
        initial_commit_msg,
        no_sync=getattr(parsed_args, "no_sync", False),
        on_auto_sync=on_auto_sync,
    ):
        return 0

    base_force_replace = getattr(parsed_args, "force_replace", False)
    session = ApplySessionState(
        current_patch=patch_text,
        target_file=target_file,
        active_force_replace=base_force_replace,
        base_force_replace=base_force_replace,
        initial_commit_msg=initial_commit_msg,
        test_option=test_option,
        yes=getattr(parsed_args, "yes", False),
        no_sync=getattr(parsed_args, "no_sync", False),
        no_bloat=getattr(parsed_args, "no_bloat", False),
    )

    total_modified_files: Set[Path] = set()
    total_success = 0
    total_skipped = 0
    last_fail = 0
    iteration = 0

    while True:
        iteration += 1
        patch_result = apply_patch(
            session.current_patch,
            project_root,
            session.target_file,
            force_replace=session.active_force_replace,
            revert=is_revert,
            dry_run=is_dry_run,
        )
        success, fail, skipped, modified_files = patch_result[0], patch_result[1], patch_result[2], patch_result[3]
        failed_details = getattr(patch_result, "failed_details", [])

        total_success += success
        total_skipped += skipped
        last_fail = fail
        total_modified_files.update(modified_files)

        report_patch_result(
            success=success,
            fail=fail,
            skipped=skipped,
            failed_details=failed_details,
            total_success=total_success,
            total_skipped=total_skipped,
            iteration=iteration,
            is_revert=is_revert,
            is_dry_run=is_dry_run,
            clipboard_writer=set_clipboard_text,
            copy_failure_report=not is_non_interactive,
        )

        if is_dry_run or is_revert or fail == 0 or is_non_interactive:
            break

        if success > 0:
            print(f"\n⚠ 成功した {success} 箇所を変更しました。")

        if not wait_for_next_patch(
            project_root=project_root,
            state=session,
            on_run_command=on_run_command,
            prompt_command_fn=prompt_user_command,
            clipboard_reader=get_clipboard_text,
            apply_patch_fn=apply_patch,
            parse_args_fn=parse_arguments,
        ):
            break

    commit_partial = False
    if not is_dry_run and not is_revert and last_fail > 0 and total_success > 0:
        commit_partial, total_success = handle_partial_success(
            project_root=project_root,
            initial_commit_msg=session.initial_commit_msg,
            total_success=total_success,
            total_modified_files=total_modified_files,
            confirm_fn=prompt_user_confirmation,
            rollback_fn=rollback_modified_files,
            non_interactive=is_non_interactive,
        )

    if is_dry_run:
        return 0 if last_fail == 0 else 1

    if is_revert and total_success > 0:
        clear_pending_commit(project_root, total_modified_files)

    test_failed = False
    if not is_revert and session.test_option is not None and total_success > 0 and last_fail == 0:
        test_failed = run_post_apply_validation(
            project_root=project_root,
            test_option=session.test_option,
            test_runner=run_validation_test,
        )

    committed_successfully = False
    if not is_revert and total_success > 0 and (last_fail == 0 or commit_partial):
        committed_successfully, aborted = finalize_apply_commit(
            project_root=project_root,
            initial_commit_msg=session.initial_commit_msg,
            total_modified_files=total_modified_files,
            test_failed=test_failed,
            commit_partial=commit_partial,
            auto_yes=session.yes,
            confirm_fn=prompt_user_confirmation,
            commit_fn=execute_git_commit,
            rollback_fn=rollback_modified_files,
            non_interactive=is_non_interactive,
        )
        if aborted:
            return 1

    if total_success > 0 and (last_fail == 0 or commit_partial) and not session.no_sync:
        action_name = "リバート" if is_revert else "置換"
        should_bloat = not is_revert and not session.no_bloat
        if on_auto_sync:
            on_auto_sync(project_root, action_name, should_bloat)

    return 0 if (last_fail == 0 or committed_successfully) else 1
