"""Module: @role: CLIからの単体ファイル移動・名前変更コマンド(aiskel move/mv/rename)を担当する。"""
import argparse
import sys
from pathlib import Path
from typing import Callable, Optional

from .cli_apply_post import finalize_apply_commit
from .cli_commit import execute_git_commit
from .cli_interaction import prompt_user_confirmation, rollback_modified_files
from .cli_sync import ensure_gitignore_updated
from .config import ensure_default_config_file, ensure_write_paths_sample_file
from .core.patch_applier import apply_patch


def handle_move_command(
    parsed_args: argparse.Namespace,
    on_auto_sync: Optional[Callable[[Path, str, bool], None]] = None,
) -> int:
    """指定されたファイルを移動または名前変更し、必要に応じてコミットと自動同期を行う。"""
    project_root: Path = parsed_args.dir.resolve()
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたディレクトリが存在しません: {project_root}", file=sys.stderr)
        return 1

    is_dry_run = getattr(parsed_args, "dry_run", False)
    force_replace = getattr(parsed_args, "force_replace", False)
    no_sync = getattr(parsed_args, "no_sync", False)
    no_push = getattr(parsed_args, "no_push", False)
    auto_yes = getattr(parsed_args, "yes", False)

    src_raw = str(parsed_args.src)
    dst_raw = str(parsed_args.dst)

    output_dir = project_root / "ai_meta"
    if not is_dry_run:
        ensure_gitignore_updated(project_root, output_dir)
        ensure_default_config_file(output_dir, project_root)
        ensure_write_paths_sample_file(output_dir, project_root)

    patch_text = f"移動: {src_raw} -> {dst_raw}\n"
    result = apply_patch(
        patch_text,
        project_root,
        force_replace=force_replace,
        dry_run=is_dry_run,
    )
    success, fail, skipped, modified_files = result[0], result[1], result[2], result[3]

    if is_dry_run:
        return 0 if fail == 0 else 1

    if fail > 0:
        for detail in getattr(result, "failed_details", []):
            print(f"  - {detail}", file=sys.stderr)
        return 1

    if success > 0:
        commit_msg = f"refactor: move {src_raw} to {dst_raw}"
        if len(commit_msg) > 72:
            commit_msg = f"refactor: move {Path(src_raw).name} to {Path(dst_raw).name}"
        _, aborted = finalize_apply_commit(
            project_root=project_root,
            initial_commit_msg=commit_msg,
            total_modified_files=set(modified_files),
            test_failed=False,
            commit_partial=False,
            auto_yes=auto_yes,
            confirm_fn=prompt_user_confirmation,
            commit_fn=execute_git_commit,
            rollback_fn=rollback_modified_files,
            non_interactive=not sys.stdin.isatty(),
            no_push=no_push,
        )
        if aborted:
            return 1
        if not no_sync and on_auto_sync:
            on_auto_sync(project_root, "ファイル移動", False)
    elif skipped > 0:
        return 0

    return 0
