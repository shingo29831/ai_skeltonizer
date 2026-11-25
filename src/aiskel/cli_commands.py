"""Module: @role: 肥大化検査(bloat)・テスト実行(test)・HTML抽出(docs)・監視(watch)・通常同期(sync)サブコマンドのハンドラを担当する。"""
import argparse
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Set, Tuple

from .cli_parser import process_comma_separated_args
from .cli_sync import resolve_output_dir, run_sync
from .config import SkeletonConfig, load_bloat_config_from_meta
from .core.bloat_detector import (
    DEFAULT_BLOAT_MAX_CHARS,
    DEFAULT_BLOAT_MAX_LINES,
    DEFAULT_TOP_FILES_COUNT,
    format_bloat_report,
    format_top_files_report,
    scan_project_files,
)
from .core.browser_focus import focus_default_browser
from .core.clipboard import set_clipboard_text
from .core.html_doc_parser import extract_html_docs, parse_html_to_markdown
from .core.test_runner import run_validation_test
from .core.token_counter import estimate_tokens
from .core.watcher import watch_project

def handle_bloat_command(
    parsed_args: argparse.Namespace,
    *,
    bloat_config_loader: Optional[Callable[[Path], Tuple[Optional[int], Optional[int], Set[str]]]] = None,
    project_scanner: Optional[Callable[..., Tuple[List[Any], List[Any]]]] = None,
    bloat_formatter: Optional[Callable[..., str]] = None,
    top_formatter: Optional[Callable[[List[Any]], str]] = None,
    comma_arg_processor: Optional[Callable[[List[str]], Set[str]]] = None,
    clipboard_writer: Optional[Callable[[str], None]] = None,
    browser_focuser: Optional[Callable[..., bool]] = None,
) -> int:
    """肥大化ファイル検出サブコマンド(bloat/b/size)を実行する。"""
    config_loader = bloat_config_loader or load_bloat_config_from_meta
    scanner = project_scanner or scan_project_files
    bloat_fmt = bloat_formatter or format_bloat_report
    top_fmt = top_formatter or format_top_files_report
    arg_processor = comma_arg_processor or process_comma_separated_args
    writer = clipboard_writer or set_clipboard_text
    focuser = browser_focuser or focus_default_browser

    project_root: Path = parsed_args.project_dir.resolve()
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたディレクトリが存在しません: {project_root}", file=sys.stderr)
        return 1

    meta_lines, meta_chars, meta_excludes = config_loader(project_root)
    max_lines = parsed_args.max_lines if parsed_args.max_lines is not None else (meta_lines or DEFAULT_BLOAT_MAX_LINES)
    max_chars = parsed_args.max_chars if parsed_args.max_chars is not None else (meta_chars or DEFAULT_BLOAT_MAX_CHARS)

    cli_excludes = arg_processor(parsed_args.exclude)
    all_excludes = meta_excludes | cli_excludes

    bloated, all_files = scanner(
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
            report = top_fmt(top_files)
            if report:
                print(f"\n{report}")
        copy_message = f"\n📋 ファイルサイズ上位 ({top_files_count}件) をクリップボードにコピーしました。"
    else:
        report = bloat_fmt(bloated, max_lines=max_lines, max_chars=max_chars)
        print(report)
        copy_message = f"\n📋 肥大化ファイル一覧 ({len(bloated)}件) をクリップボードにコピーしました (AIチャットにそのままペースト可能)。"

    if report and not getattr(parsed_args, "no_clipboard", False):
        clipboard_ok = False
        try:
            writer(report)
            clipboard_ok = True
            print(copy_message)
        except Exception as e:
            print(f"⚠ クリップボードへの保存に失敗しました: {e}", file=sys.stderr)
        if bloated and clipboard_ok and not getattr(parsed_args, "no_focus", False):
            try:
                if focuser(interactive_only=True):
                    print("   🌐 起動済みのデフォルトブラウザを最前面に表示しました。")
            except Exception as e:
                print(f"⚠ ブラウザの最前面表示に失敗しました: {e}", file=sys.stderr)
    return 0

def handle_test_command(
    parsed_args: argparse.Namespace,
    *,
    validation_runner: Optional[Callable[..., Tuple[bool, str]]] = None,
) -> int:
    """テスト実行サブコマンド(test/t)を実行する。"""
    runner = validation_runner or run_validation_test
    project_root: Path = parsed_args.dir.resolve()
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたディレクトリが存在しません: {project_root}", file=sys.stderr)
        return 1

    custom_cmd = " ".join(parsed_args.cmd).strip() if parsed_args.cmd else None
    is_verbose = not getattr(parsed_args, "quiet", False)
    timeout_sec = getattr(parsed_args, "timeout", 60)

    test_ok, test_output = runner(
        project_root,
        custom_cmd or "auto",
        timeout_sec=timeout_sec,
        verbose=is_verbose,
    )

    if test_ok:
        print("\n✅ テストが正常に完了しました。")
        return 0
    print("\n❌ テストが失敗しました。", file=sys.stderr)
    if not is_verbose and test_output:
        print("\n--- テスト失敗ログ ---", file=sys.stderr)
        print(test_output, file=sys.stderr)
        print("----------------------\n", file=sys.stderr)
    return 1

def handle_docs_command(
    parsed_args: argparse.Namespace,
    *,
    html_parser: Optional[Callable[[str], str]] = None,
    docs_extractor: Optional[Callable[..., Tuple[str, int, int, List[Path]]]] = None,
    token_estimator: Optional[Callable[[str], int]] = None,
    clipboard_writer: Optional[Callable[[str], None]] = None,
    base_dir: Optional[Path] = None,
) -> int:
    """HTMLドキュメント抽出サブコマンド(docs/d)を実行する。"""
    parser_fn = html_parser or parse_html_to_markdown
    extractor = docs_extractor or extract_html_docs
    estimator = token_estimator or estimate_tokens
    writer = clipboard_writer or set_clipboard_text

    targets = parsed_args.targets
    use_clipboard = getattr(parsed_args, "clipboard", False)
    output_file = getattr(parsed_args, "output", None)
    project_root = (base_dir or Path(".")).resolve()

    try:
        raw_stdin = sys.stdin.read() if (not targets and not sys.stdin.isatty()) else None
        if raw_stdin:
            formatted_text = parser_fn(raw_stdin)
            est_tokens = estimator(formatted_text)
            total_chars = len(formatted_text)
            processed_files: List[Path] = []
        else:
            target_paths = targets if targets else [Path(".")]
            formatted_text, total_chars, est_tokens, processed_files = extractor(target_paths, project_root=project_root)

        if output_file:
            output_file.resolve().write_text(formatted_text, encoding="utf-8")
            print(f"✔ 解析結果を保存しました: {output_file}", file=sys.stderr)

        if use_clipboard:
            writer(formatted_text)
            file_info = f" ({len(processed_files)}件のHTML)" if processed_files else ""
            print(
                f"📋 クリップボードに解析結果をコピーしました。{file_info} "
                f"(文字数: {total_chars:,} 文字, 推定トークン: 約 {est_tokens:,} tokens)",
                file=sys.stderr,
            )
        elif not output_file and sys.stderr.isatty() and processed_files:
            print(f"📑 {len(processed_files)} 件のHTMLドキュメントを解析・統合しました (約 {est_tokens:,} tokens)", file=sys.stderr)

        if not output_file:
            print(formatted_text)
        return 0
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"✖ HTMLドキュメント解析失敗: {e}", file=sys.stderr)
        return 1

def build_skeleton_config(
    parsed_args: argparse.Namespace,
    project_root: Path,
    *,
    comma_arg_processor: Optional[Callable[[List[str]], Set[str]]] = None,
) -> SkeletonConfig:
    """CLIオプションと対象ルートからSkeletonConfigを構築する。"""
    arg_processor = comma_arg_processor or process_comma_separated_args
    resolved_full_paths = {
        (project_root / Path(p)).resolve() if not Path(p).is_absolute() else Path(p).resolve()
        for p in arg_processor(parsed_args.full_path)
    }
    return SkeletonConfig(
        full_code_paths=resolved_full_paths,
        keep_functions=arg_processor(parsed_args.keep_func),
        only_nodes=arg_processor(parsed_args.only_nodes),
        create_bundle=not parsed_args.no_bundle,
        bundle_format=parsed_args.format,
        policy_path=parsed_args.policy.resolve() if parsed_args.policy else None,
    )

def handle_watch_command(
    parsed_args: argparse.Namespace,
    *,
    output_dir_resolver: Optional[Callable[[Path, Optional[Path]], Path]] = None,
    sync_runner: Optional[Callable[..., Tuple[int, int, int, Optional[Path]]]] = None,
    watch_runner: Optional[Callable[..., None]] = None,
    comma_arg_processor: Optional[Callable[[List[str]], Set[str]]] = None,
) -> int:
    """プロジェクト監視サブコマンド(watch/w)を実行する。"""
    dir_resolver = output_dir_resolver or resolve_output_dir
    runner = sync_runner or run_sync
    watcher = watch_runner or watch_project

    project_root: Path = parsed_args.project_dir.resolve()
    output_dir: Path = dir_resolver(project_root, parsed_args.output_dir)
    config = build_skeleton_config(parsed_args, project_root, comma_arg_processor=comma_arg_processor)
    runner(project_root, output_dir, config, no_ui=parsed_args.no_ui, quiet=False)
    watcher(
        project_root,
        lambda: runner(project_root, output_dir, config, no_ui=parsed_args.no_ui, quiet=True),
        interval_sec=parsed_args.interval,
    )
    return 0

def handle_sync_command(
    parsed_args: argparse.Namespace,
    *,
    output_dir_resolver: Optional[Callable[[Path, Optional[Path]], Path]] = None,
    sync_runner: Optional[Callable[..., Tuple[int, int, int, Optional[Path]]]] = None,
    comma_arg_processor: Optional[Callable[[List[str]], Set[str]]] = None,
) -> int:
    """デフォルトのスケルトン同期コマンドを実行する。"""
    dir_resolver = output_dir_resolver or resolve_output_dir
    runner = sync_runner or run_sync
    arg_processor = comma_arg_processor or process_comma_separated_args

    project_root: Path = parsed_args.project_dir.resolve()
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたソースディレクトリが存在しません: {project_root}", file=sys.stderr)
        return 1

    output_dir: Path = dir_resolver(project_root, parsed_args.output_dir)
    if project_root == output_dir:
        print("エラー: ソースディレクトリと出力先ディレクトリに同じパスは指定できません。", file=sys.stderr)
        return 1

    config = build_skeleton_config(parsed_args, project_root, comma_arg_processor=arg_processor)
    print(f"解析を開始します: {project_root}")
    runner(
        project_root,
        output_dir,
        config,
        force=parsed_args.force,
        focus_paths=arg_processor(parsed_args.focus),
        focus_deps=parsed_args.focus_deps,
        git_diff=parsed_args.git_diff,
        no_ui=parsed_args.no_ui,
        quiet=False,
    )
    return 0
