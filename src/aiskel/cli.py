"""Module: @role: CLI引数の解析、実行オプションの検証、および各サブコマンドのディスパッチを担当する。"""
import argparse
import os
import sys
import subprocess
import platform
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple, Union

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


def main(args: Optional[List[str]] = None) -> int:
    try:
        parsed_args = parse_arguments(args)

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
                if not getattr(parsed_args, "quiet", False):
                    print(f"✔ 肥大化ファイルは検出されませんでした (基準: 行数 >= {max_lines}, 文字数 >= {max_chars:,})")
                    top_count = getattr(parsed_args, "top_count", DEFAULT_TOP_FILES_COUNT)
                    if top_count > 0 and all_files:
                        all_files.sort(key=lambda x: (x.chars, x.lines), reverse=True)
                        report = format_top_files_report(all_files[:top_count])
                        if report:
                            print(f"\n{report}")
                return 0

            report = format_bloat_report(bloated, max_lines=max_lines, max_chars=max_chars)
            print(report)

            if not getattr(parsed_args, "no_clipboard", False):
                try:
                    set_clipboard_text(report)
                    print(f"\n📋 肥大化ファイル一覧 ({len(bloated)}件) をクリップボードにコピーしました (AIチャットにそのままペースト可能)。")
                except Exception as e:
                    print(f"⚠ クリップボードへの保存に失敗しました: {e}", file=sys.stderr)
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
            max_chars = parsed_args.max_chars
            with_deps = getattr(parsed_args, "deps", False)
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
                return 0
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
                # なぜ必要か: 失敗時にAIへ即座に相談できるようエラー詳細を自動保存
                try:
                    set_clipboard_text(error_report)
                    print("📋 失敗原因をクリップボードに保存しました (AIチャットにそのままペースト可能)。", file=sys.stderr)
                except Exception:
                    pass
                return 1

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
            return handle_apply_command(parsed_args, on_auto_sync=_trigger_auto_sync)

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
