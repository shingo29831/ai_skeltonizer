"""Module: @role: CLI引数の解析、実行オプションの検証、および各サブコマンドのディスパッチを担当する。"""
import contextlib
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from .config import SkeletonConfig, load_bloat_config_from_meta
from .core.bloat_detector import (
    detect_bloated_files,
    format_bloat_report,
    format_top_files_report,
    scan_project_files,
    DEFAULT_BLOAT_MAX_LINES,
    DEFAULT_BLOAT_MAX_CHARS,
    DEFAULT_TOP_FILES_COUNT,
)
from .core.token_counter import estimate_tokens
from .core.browser_focus import focus_default_browser, focus_default_browser_target
from .core.browser_paste import paste_to_browser_prompt
from .core.clipboard import set_clipboard_text
from .core.code_extractor import extract_and_format_snippets
from .core.html_doc_parser import extract_html_docs, parse_html_to_markdown
from .core.watcher import watch_project
from .core.test_runner import run_validation_test, try_handle_cd_command
from .cli_interaction import (
    extract_commit_message,
    read_single_key_from_tty,
    prompt_user_confirmation,
    rollback_modified_files,
)
from .cli_apply import handle_apply_command
from .cli_move import handle_move_command
from .cli_commands import (
    handle_bloat_command,
    handle_docs_command,
    handle_scope_command,
    handle_sync_command,
    handle_test_command,
    handle_watch_command,
)
from .cli_commit import handle_commit_command
from .cli_copy import (
    CopyCommandResult,
    KNOWN_NON_COPY_SUBCOMMANDS,
    PASTE_RETRY_DELAYS,
    TeeStderr,
    execute_copy_command,
    focus_and_paste_clipboard,
    handle_copy_parse_failure,
    is_copy_invocation,
)
from .cli_listen import handle_listen_command
from .cli_start import handle_start_command
from .cli_parser import (
    parse_arguments,
    normalize_cli_args,
    process_comma_separated_args,
    _normalize_cli_args,
    _process_comma_separated_args,
)
from .cli_sync import (
    resolve_output_dir,
    run_sync,
    trigger_auto_sync,
    _resolve_output_dir,
    _run_sync,
    _trigger_auto_sync,
)

_run_validation_test = run_validation_test
_try_handle_cd_command = try_handle_cd_command
_extract_commit_message = extract_commit_message
_read_single_key_from_tty = read_single_key_from_tty
_prompt_user_confirmation = prompt_user_confirmation
_rollback_modified_files = rollback_modified_files


_PASTE_RETRY_DELAYS = PASTE_RETRY_DELAYS
_KNOWN_NON_COPY_SUBCOMMANDS = KNOWN_NON_COPY_SUBCOMMANDS
_TeeStderr = TeeStderr
_is_copy_invocation = is_copy_invocation

def _focus_and_paste_clipboard(
    *,
    no_focus: bool,
    no_paste: bool,
    focus_browser: bool,
    allowed_title_keywords: Optional[Sequence[str]] = None,
) -> Tuple[bool, str]:
    return focus_and_paste_clipboard(
        no_focus=no_focus,
        no_paste=no_paste,
        focus_browser=focus_browser,
        allowed_title_keywords=allowed_title_keywords,
        browser_target_focuser=focus_default_browser_target,
        prompt_paster=paste_to_browser_prompt,
        paste_retry_delays=_PASTE_RETRY_DELAYS,
    )

def _handle_copy_parse_failure(
    raw_args: Sequence[str],
    stderr_text: str,
    exit_code: int,
    *,
    focus_browser: bool,
    allowed_title_keywords: Optional[Sequence[str]],
) -> CopyCommandResult:
    return handle_copy_parse_failure(
        raw_args,
        stderr_text,
        exit_code,
        focus_browser=focus_browser,
        allowed_title_keywords=allowed_title_keywords,
        clipboard_writer=set_clipboard_text,
        paste_fn=_focus_and_paste_clipboard,
    )

def _execute_copy_command(
    parsed_args,
    copy_specs: List[str],
    project_root: Path,
    focus_browser: bool = True,
    allowed_title_keywords: Optional[Sequence[str]] = None,
) -> CopyCommandResult:
    return execute_copy_command(
        parsed_args,
        copy_specs,
        project_root,
        focus_browser=focus_browser,
        allowed_title_keywords=allowed_title_keywords,
        snippet_extractor=extract_and_format_snippets,
        clipboard_writer=set_clipboard_text,
        paste_fn=_focus_and_paste_clipboard,
    )


def run_structured_command(
    args: Optional[List[str]] = None,
    *,
    focus_browser: bool = True,
    allowed_title_keywords: Optional[Sequence[str]] = None,
) -> CopyCommandResult:
    """CLIコマンドを実行し、copy系コマンド時は貼り付け成否を含む構造化結果を返す。"""
    raw_args = list(args) if args is not None else sys.argv[1:]
    tee_stderr = _TeeStderr(sys.stderr)
    try:
        with contextlib.redirect_stderr(tee_stderr):
            parsed_args = parse_arguments(args)
        is_copy_cmd = hasattr(parsed_args, "command") and parsed_args.command in ("copy", "c", "cp")
        copy_specs = parsed_args.specs if is_copy_cmd else getattr(parsed_args, "copy", None)
        if copy_specs:
            project_root = (parsed_args.dir if is_copy_cmd else parsed_args.project_dir).resolve()
            return _execute_copy_command(
                parsed_args,
                copy_specs=copy_specs,
                project_root=project_root,
                focus_browser=focus_browser,
                allowed_title_keywords=allowed_title_keywords,
            )
    except SystemExit as e:
        exit_code = int(e.code) if isinstance(e.code, int) else 1
        if exit_code != 0 and _is_copy_invocation(raw_args):
            return _handle_copy_parse_failure(
                raw_args,
                tee_stderr.getvalue(),
                exit_code,
                focus_browser=focus_browser,
                allowed_title_keywords=allowed_title_keywords,
            )
        raise
    except Exception as e:
        print(f"\n致命的なエラーが発生しました: {e}", file=sys.stderr)
        return CopyCommandResult(exit_code=1, pasted=False, paste_reason=str(e), error_message=str(e))
    return CopyCommandResult(exit_code=main(args), pasted=False)


def main(args: Optional[List[str]] = None) -> int:
    raw_args = list(args) if args is not None else sys.argv[1:]
    tee_stderr = _TeeStderr(sys.stderr)
    try:
        try:
            with contextlib.redirect_stderr(tee_stderr):
                parsed_args = parse_arguments(args)
        except SystemExit as e:
            exit_code = int(e.code) if isinstance(e.code, int) else 1
            if exit_code != 0 and _is_copy_invocation(raw_args):
                return _handle_copy_parse_failure(
                    raw_args,
                    tee_stderr.getvalue(),
                    exit_code,
                    focus_browser=True,
                    allowed_title_keywords=None,
                ).exit_code
            raise

        cmd = getattr(parsed_args, "command", None)
        if cmd in ("bloat", "b", "size"):
            return handle_bloat_command(
                parsed_args,
                bloat_config_loader=load_bloat_config_from_meta,
                project_scanner=scan_project_files,
                bloat_formatter=format_bloat_report,
                top_formatter=format_top_files_report,
                comma_arg_processor=_process_comma_separated_args,
                clipboard_writer=set_clipboard_text,
                browser_focuser=focus_default_browser,
            )

        if cmd in ("test", "t"):
            return handle_test_command(parsed_args, validation_runner=_run_validation_test)

        is_copy_cmd = cmd in ("copy", "c", "cp")
        copy_specs = parsed_args.specs if is_copy_cmd else getattr(parsed_args, "copy", None)
        if copy_specs:
            project_root = (parsed_args.dir if is_copy_cmd else parsed_args.project_dir).resolve()
            return _execute_copy_command(
                parsed_args,
                copy_specs=copy_specs,
                project_root=project_root,
                focus_browser=True,
            ).exit_code

        if cmd in ("scope", "sc"):
            return handle_scope_command(
                parsed_args,
                comma_arg_processor=_process_comma_separated_args,
                clipboard_writer=set_clipboard_text,
                token_estimator=estimate_tokens,
            )

        if cmd in ("docs", "d"):
            return handle_docs_command(
                parsed_args,
                html_parser=parse_html_to_markdown,
                docs_extractor=extract_html_docs,
                token_estimator=estimate_tokens,
                clipboard_writer=set_clipboard_text,
            )

        if cmd in ("apply", "a", "revert", "r", "check", "chk", "verify"):
            return handle_apply_command(parsed_args, on_auto_sync=_trigger_auto_sync, on_run_command=main)

        if cmd in ("commit", "cm", "ci"):
            return handle_commit_command(parsed_args, on_auto_sync=_trigger_auto_sync)

        if cmd in ("move", "mv", "rename"):
            return handle_move_command(parsed_args, on_auto_sync=_trigger_auto_sync)

        if cmd in ("listen", "l"):
            return handle_listen_command(
                parsed_args,
                on_run_command=main,
                on_run_structured=run_structured_command,
            )

        if cmd in ("start", "s"):
            return handle_start_command(
                parsed_args,
                on_run_command=main,
                on_run_structured=run_structured_command,
            )

        if cmd in ("watch", "w"):
            return handle_watch_command(
                parsed_args,
                output_dir_resolver=_resolve_output_dir,
                sync_runner=_run_sync,
                watch_runner=watch_project,
                comma_arg_processor=_process_comma_separated_args,
            )

        return handle_sync_command(
            parsed_args,
            output_dir_resolver=_resolve_output_dir,
            sync_runner=_run_sync,
            comma_arg_processor=_process_comma_separated_args,
        )

    except Exception as e:
        print(f"\n致命的なエラーが発生しました: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
