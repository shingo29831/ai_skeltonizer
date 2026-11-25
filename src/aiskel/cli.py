"""Module: @role: CLI引数の解析、実行オプションの検証、および各サブコマンドのディスパッチを担当する。"""
import argparse
import contextlib
import io
import os
import sys
import subprocess
import platform
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple, Union

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
from .cli_commit import handle_commit_command
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


_PASTE_RETRY_DELAYS: Tuple[float, ...] = (0.15, 0.3, 0.5)
_KNOWN_NON_COPY_SUBCOMMANDS = frozenset({
    "apply", "a", "revert", "r", "commit", "cm", "ci", "test", "t",
    "bloat", "b", "size", "docs", "d", "watch", "w", "listen", "l", "start", "s",
})


class _TeeStderr:
    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self._buffer = io.StringIO()

    def write(self, text: str) -> int:
        self._buffer.write(text)
        if self._wrapped is not None:
            return self._wrapped.write(text)
        return len(text)

    def flush(self) -> None:
        if self._wrapped is not None:
            self._wrapped.flush()

    def isatty(self) -> bool:
        return getattr(self._wrapped, "isatty", lambda: False)() if self._wrapped is not None else False

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)

    def getvalue(self) -> str:
        return self._buffer.getvalue()


@dataclass(frozen=True)
class CopyCommandResult:
    """aiskel c コマンドの構造化実行結果を保持する。"""

    exit_code: int
    pasted: bool = False
    paste_reason: str = ""
    copied_chars: int = 0
    error_message: str = ""


def _focus_and_paste_clipboard(
    *,
    no_focus: bool,
    no_paste: bool,
    focus_browser: bool,
    allowed_title_keywords: Optional[Sequence[str]] = None,
) -> Tuple[bool, str]:
    """クリップボード保存済みの内容をブラウザ入力欄へ貼り付け、(成功可否, 理由)を返す。"""
    if no_focus:
        return False, ""
    target_window: Optional[Union[int, str]] = None
    can_attempt_paste = False
    if focus_browser:
        target_window = focus_default_browser_target(interactive_only=True)
        if target_window is not None:
            print("   🌐 起動済みのデフォルトブラウザを最前面に表示しました。")
            can_attempt_paste = True
        else:
            return False, "起動済みのデフォルトブラウザを検出できませんでした"
    else:
        # ブリッジ経由で対象タブが既に最前面のときは別ブラウザへのフォーカス奪取を防ぐ
        can_attempt_paste = True

    if not can_attempt_paste or no_paste:
        return False, ""

    paste_res = paste_to_browser_prompt(
        interactive_only=True,
        target_window=target_window,
        allowed_title_keywords=allowed_title_keywords,
    )
    if getattr(paste_res, "target_window", None) is not None:
        target_window = paste_res.target_window
    # 一時的なUI遷移・フォーカス揺らぎによる貼り付け失敗を吸収する短時間リトライ
    for delay in _PASTE_RETRY_DELAYS:
        if paste_res.success or not getattr(paste_res, "retryable", False):
            break
        time.sleep(delay)
        if focus_browser and getattr(paste_res, "error_kind", None) and str(getattr(paste_res.error_kind, "value", "")) == "window_lost":
            refocused = focus_default_browser_target(interactive_only=True)
            if refocused is not None:
                target_window = refocused
        paste_res = paste_to_browser_prompt(
            interactive_only=True,
            target_window=target_window,
            allowed_title_keywords=allowed_title_keywords,
        )
        if getattr(paste_res, "target_window", None) is not None:
            target_window = paste_res.target_window

    if paste_res.success:
        print("   📝 プロンプト入力欄に貼り付けて自動入力しました。")
        return True, ""
    if paste_res.reason:
        print(f"   ℹ 自動貼り付けをスキップしました: {paste_res.reason}")
        return False, paste_res.reason
    return False, ""


def _is_copy_invocation(raw_args: Sequence[str]) -> bool:
    if not raw_args:
        return False
    if any(tok in ("-h", "--help") for tok in raw_args):
        return False
    if raw_args[0] in ("copy", "c", "cp"):
        return True
    if raw_args[0] in _KNOWN_NON_COPY_SUBCOMMANDS:
        return False
    return any(tok in ("-c", "--copy") for tok in raw_args)


def _handle_copy_parse_failure(
    raw_args: Sequence[str],
    stderr_text: str,
    exit_code: int,
    *,
    focus_browser: bool,
    allowed_title_keywords: Optional[Sequence[str]],
) -> CopyCommandResult:
    detail = " ".join(line.strip() for line in stderr_text.splitlines() if line.strip()) or f"終了コード {exit_code}"
    cmd_text = "aiskel " + " ".join(raw_args)
    error_report = (
        f"[aiskel c コマンド引数エラー]\n"
        f"コマンド: {cmd_text}\n"
        f"失敗原因: {detail}\n\n"
        f"上記エラーを解消するための正しい aiskel c コマンド（オプションや対象指定）を提示してください。"
    )
    no_error_clipboard = "--no-error-clipboard" in raw_args
    no_focus = "--no-focus" in raw_args
    no_paste = "--no-paste" in raw_args

    copied_ok = False
    pasted = False
    paste_reason = ""
    if not no_error_clipboard:
        try:
            set_clipboard_text(error_report)
            copied_ok = True
            print("📋 失敗原因をクリップボードに保存しました (AIチャットにそのままペースト可能)。", file=sys.stderr)
        except Exception:
            copied_ok = False

    if copied_ok:
        pasted, paste_reason = _focus_and_paste_clipboard(
            no_focus=no_focus,
            no_paste=no_paste,
            focus_browser=focus_browser,
            allowed_title_keywords=allowed_title_keywords,
        )

    return CopyCommandResult(
        exit_code=exit_code,
        pasted=pasted,
        paste_reason="" if pasted else (paste_reason or detail),
        copied_chars=len(error_report) if copied_ok else 0,
        error_message=detail,
    )


def _execute_copy_command(
    parsed_args: argparse.Namespace,
    copy_specs: List[str],
    project_root: Path,
    focus_browser: bool = True,
    allowed_title_keywords: Optional[Sequence[str]] = None,
) -> CopyCommandResult:
    max_chars = parsed_args.max_chars
    with_deps = getattr(parsed_args, "deps", False)
    no_focus = getattr(parsed_args, "no_focus", False)
    no_paste = getattr(parsed_args, "no_paste", False)
    try:
        formatted_text, est_tokens, item_count = extract_and_format_snippets(
            copy_specs, project_root, max_chars=max_chars, with_deps=with_deps
        )
        set_clipboard_text(formatted_text)
        deps_note = " (依存ノード自動解決含む)" if with_deps else ""
        print(f"📋 クリップボードに {item_count} 件のコード{deps_note}をコピーしました。")
        print(f"   - 総文字数   : {len(formatted_text):,} 文字")
        print(f"   - 推定トークン: 約 {est_tokens:,} tokens")
        print("   AIチャットへそのままペーストしてご利用いただけます。")

        pasted, paste_reason = _focus_and_paste_clipboard(
            no_focus=no_focus,
            no_paste=no_paste,
            focus_browser=focus_browser,
            allowed_title_keywords=allowed_title_keywords,
        )
        return CopyCommandResult(
            exit_code=0,
            pasted=pasted,
            paste_reason=paste_reason,
            copied_chars=len(formatted_text),
        )
    except (ValueError, FileNotFoundError, KeyError, RuntimeError) as e:
        print(f"✖ コピー失敗: {e}", file=sys.stderr)
        cmd_flag = "-d " if with_deps else ""
        specs_str = " ".join(copy_specs)
        error_report = (
            f"[aiskel c コード抽出エラー]\n"
            f"コマンド: aiskel c {cmd_flag}{specs_str}\n"
            f"失敗原因: {e}\n\n"
            f"上記エラーを解消するための正しい対象指定（ファイルパスやノード名）を提示してください。"
        )
        # なぜ必要か: 失敗時にAIへ即座に相談できるようエラー詳細を自動保存し成功時と同様に貼り付ける
        copied_ok = False
        pasted = False
        paste_reason = ""
        if not getattr(parsed_args, "no_error_clipboard", False):
            try:
                set_clipboard_text(error_report)
                copied_ok = True
                print("📋 失敗原因をクリップボードに保存しました (AIチャットにそのままペースト可能)。", file=sys.stderr)
            except Exception:
                copied_ok = False
        if copied_ok:
            pasted, paste_reason = _focus_and_paste_clipboard(
                no_focus=no_focus,
                no_paste=no_paste,
                focus_browser=focus_browser,
                allowed_title_keywords=allowed_title_keywords,
            )
        return CopyCommandResult(
            exit_code=1,
            pasted=pasted,
            paste_reason="" if pasted else (paste_reason or str(e)),
            copied_chars=len(error_report) if copied_ok else 0,
            error_message=str(e),
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

        if hasattr(parsed_args, "command") and parsed_args.command in ("bloat", "b", "size"):
            project_root: Path = parsed_args.project_dir.resolve()
            if not project_root.exists() or not project_root.is_dir():
                print(f"エラー: 指定されたディレクトリが存在しません: {project_root}", file=sys.stderr)
                return 1

            meta_lines, meta_chars, meta_excludes = load_bloat_config_from_meta(project_root)
            max_lines = parsed_args.max_lines if parsed_args.max_lines is not None else (meta_lines or DEFAULT_BLOAT_MAX_LINES)
            max_chars = parsed_args.max_chars if parsed_args.max_chars is not None else (meta_chars or DEFAULT_BLOAT_MAX_CHARS)

            cli_excludes = _process_comma_separated_args(parsed_args.exclude)
            all_excludes = meta_excludes | cli_excludes

            bloated, all_files = scan_project_files(
                project_root=project_root,
                max_lines=max_lines,
                max_chars=max_chars,
                exclude_paths=all_excludes,
            )

            if not bloated:
                if getattr(parsed_args, "quiet", False):
                    return 0
                print(f"✔ 肥大化ファイルは検出されませんでした (基準: 行数 >= {max_lines}, 文字数 >= {max_chars:,})")
                top_count = getattr(parsed_args, "top_count", DEFAULT_TOP_FILES_COUNT)
                report = ""
                top_files_count = 0
                if top_count is not None and top_count > 0 and all_files:
                    all_files.sort(key=lambda x: (x.chars, x.lines), reverse=True)
                    top_files = all_files[:top_count]
                    top_files_count = len(top_files)
                    report = format_top_files_report(top_files)
                    if report:
                        print(f"\n{report}")
                copy_message = f"\n📋 ファイルサイズ上位 ({top_files_count}件) をクリップボードにコピーしました。"
            else:
                report = format_bloat_report(bloated, max_lines=max_lines, max_chars=max_chars)
                print(report)
                copy_message = f"\n📋 肥大化ファイル一覧 ({len(bloated)}件) をクリップボードにコピーしました (AIチャットにそのままペースト可能)。"

            if report and not getattr(parsed_args, "no_clipboard", False):
                clipboard_ok = False
                try:
                    set_clipboard_text(report)
                    clipboard_ok = True
                    print(copy_message)
                except Exception as e:
                    print(f"⚠ クリップボードへの保存に失敗しました: {e}", file=sys.stderr)
                if bloated and clipboard_ok and not getattr(parsed_args, "no_focus", False):
                    try:
                        if focus_default_browser(interactive_only=True):
                            print("   🌐 起動済みのデフォルトブラウザを最前面に表示しました。")
                    except Exception as e:
                        print(f"⚠ ブラウザの最前面表示に失敗しました: {e}", file=sys.stderr)
            return 0

        if hasattr(parsed_args, "command") and parsed_args.command in ("test", "t"):
            project_root: Path = parsed_args.dir.resolve()
            if not project_root.exists() or not project_root.is_dir():
                print(f"エラー: 指定されたディレクトリが存在しません: {project_root}", file=sys.stderr)
                return 1

            custom_cmd = " ".join(parsed_args.cmd).strip() if parsed_args.cmd else None
            is_verbose = not getattr(parsed_args, "quiet", False)
            timeout_sec = getattr(parsed_args, "timeout", 60)

            test_ok, test_output = _run_validation_test(
                project_root,
                custom_cmd or "auto",
                timeout_sec=timeout_sec,
                verbose=is_verbose,
            )

            if test_ok:
                print("\n✅ テストが正常に完了しました。")
                return 0
            else:
                print("\n❌ テストが失敗しました。", file=sys.stderr)
                if not is_verbose and test_output:
                    print("\n--- テスト失敗ログ ---", file=sys.stderr)
                    print(test_output, file=sys.stderr)
                    print("----------------------\n", file=sys.stderr)
                return 1

        is_copy_cmd = hasattr(parsed_args, "command") and parsed_args.command in ("copy", "c", "cp")
        copy_specs = parsed_args.specs if is_copy_cmd else getattr(parsed_args, "copy", None)
        if copy_specs:
            project_root = (parsed_args.dir if is_copy_cmd else parsed_args.project_dir).resolve()
            return _execute_copy_command(
                parsed_args,
                copy_specs=copy_specs,
                project_root=project_root,
                focus_browser=True,
            ).exit_code

        if hasattr(parsed_args, "command") and parsed_args.command in ("docs", "d"):
            targets = parsed_args.targets
            use_clipboard = getattr(parsed_args, "clipboard", False)
            output_file = getattr(parsed_args, "output", None)
            project_root = Path(".").resolve()

            try:
                raw_stdin = sys.stdin.read() if (not targets and not sys.stdin.isatty()) else None
                if raw_stdin:
                    formatted_text = parse_html_to_markdown(raw_stdin)
                    est_tokens = estimate_tokens(formatted_text)
                    total_chars = len(formatted_text)
                    processed_files = []
                else:
                    target_paths = targets if targets else [Path(".")]
                    formatted_text, total_chars, est_tokens, processed_files = extract_html_docs(target_paths, project_root=project_root)

                if output_file:
                    output_file.resolve().write_text(formatted_text, encoding="utf-8")
                    print(f"✔ 解析結果を保存しました: {output_file}", file=sys.stderr)

                if use_clipboard:
                    set_clipboard_text(formatted_text)
                    file_info = f" ({len(processed_files)}件のHTML)" if processed_files else ""
                    print(f"📋 クリップボードに解析結果をコピーしました。{file_info} (文字数: {total_chars:,} 文字, 推定トークン: 約 {est_tokens:,} tokens)", file=sys.stderr)
                elif not output_file and sys.stderr.isatty() and processed_files:
                    print(f"📑 {len(processed_files)} 件のHTMLドキュメントを解析・統合しました (約 {est_tokens:,} tokens)", file=sys.stderr)

                if not output_file:
                    print(formatted_text)
                return 0
            except (FileNotFoundError, ValueError, RuntimeError) as e:
                print(f"✖ HTMLドキュメント解析失敗: {e}", file=sys.stderr)
                return 1

        if hasattr(parsed_args, "command") and parsed_args.command in ("apply", "a", "revert", "r"):
            return handle_apply_command(parsed_args, on_auto_sync=_trigger_auto_sync, on_run_command=main)

        if hasattr(parsed_args, "command") and parsed_args.command in ("commit", "cm", "ci"):
            return handle_commit_command(parsed_args, on_auto_sync=_trigger_auto_sync)

        if hasattr(parsed_args, "command") and parsed_args.command in ("listen", "l"):
            return handle_listen_command(
                parsed_args,
                on_run_command=main,
                on_run_structured=run_structured_command,
            )

        if hasattr(parsed_args, "command") and parsed_args.command in ("start", "s"):
            return handle_start_command(
                parsed_args,
                on_run_command=main,
                on_run_structured=run_structured_command,
            )

        if hasattr(parsed_args, "command") and parsed_args.command in ("watch", "w"):
            project_root: Path = parsed_args.project_dir.resolve()
            output_dir: Path = _resolve_output_dir(project_root, parsed_args.output_dir)
            resolved_full_paths = {
                (project_root / Path(p)).resolve() if not Path(p).is_absolute() else Path(p).resolve()
                for p in _process_comma_separated_args(parsed_args.full_path)
            }
            config = SkeletonConfig(
                full_code_paths=resolved_full_paths,
                keep_functions=_process_comma_separated_args(parsed_args.keep_func),
                only_nodes=_process_comma_separated_args(parsed_args.only_nodes),
                create_bundle=not parsed_args.no_bundle,
                bundle_format=parsed_args.format,
                policy_path=parsed_args.policy.resolve() if parsed_args.policy else None,
            )
            _run_sync(project_root, output_dir, config, no_ui=parsed_args.no_ui, quiet=False)
            watch_project(
                project_root,
                lambda: _run_sync(project_root, output_dir, config, no_ui=parsed_args.no_ui, quiet=True),
                interval_sec=parsed_args.interval,
            )
            return 0

        project_root: Path = parsed_args.project_dir.resolve()
        if not project_root.exists() or not project_root.is_dir():
            print(f"エラー: 指定されたソースディレクトリが存在しません: {project_root}", file=sys.stderr)
            return 1

        output_dir: Path = _resolve_output_dir(project_root, parsed_args.output_dir)
        if project_root == output_dir:
            print("エラー: ソースディレクトリと出力先ディレクトリに同じパスは指定できません。", file=sys.stderr)
            return 1

        resolved_full_paths = {
            (project_root / Path(p)).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            for p in _process_comma_separated_args(parsed_args.full_path)
        }

        config = SkeletonConfig(
            full_code_paths=resolved_full_paths,
            keep_functions=_process_comma_separated_args(parsed_args.keep_func),
            only_nodes=_process_comma_separated_args(parsed_args.only_nodes),
            create_bundle=not parsed_args.no_bundle,
            bundle_format=parsed_args.format,
            policy_path=parsed_args.policy.resolve() if parsed_args.policy else None,
        )

        print(f"解析を開始します: {project_root}")
        _run_sync(
            project_root,
            output_dir,
            config,
            force=parsed_args.force,
            focus_paths=_process_comma_separated_args(parsed_args.focus),
            focus_deps=parsed_args.focus_deps,
            git_diff=parsed_args.git_diff,
            no_ui=parsed_args.no_ui,
            quiet=False,
        )
        return 0

    except Exception as e:
        print(f"\n致命的なエラーが発生しました: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
