"""Module: @role: コーディング開始時のスケルトン同期・バンドルコピー・ブラウザ起動・AI Studio監視ブリッジ起動を統合制御する。"""
import argparse
import inspect
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from .cli_parser import process_comma_separated_args
from .cli_sync import resolve_output_dir, run_sync
from .config import SkeletonConfig, load_bundle_filename_from_meta
from .core.aistudio_bridge import run_bridge_server
from .core.aistudio_extension import DEFAULT_BRIDGE_HOST, DEFAULT_BRIDGE_PORT
from .core.browser_focus import focus_default_browser, focus_default_browser_target
from .core.browser_paste import PasteErrorKind, PasteResult, paste_to_browser_prompt
from .core.clipboard import set_clipboard_text
from .core.token_counter import estimate_tokens

DEFAULT_AISTUDIO_URL = "https://aistudio.google.com/"
DEFAULT_FOCUS_RETRY_DELAYS: Tuple[float, ...] = (0.3, 0.5, 0.7, 1.0, 1.5)
DEFAULT_PASTE_SETTLE_DELAY: float = 1.5
DEFAULT_PASTE_RETRY_DELAYS: Tuple[float, ...] = (0.5, 0.7, 1.0, 1.2, 1.5, 2.0, 2.0, 2.0, 2.5, 2.5)
DEFAULT_PASTE_TIMEOUT_SEC: float = 18.0
SUPPORTED_PASTE_DOMAIN_TITLE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "aistudio.google.com": ("google ai studio", "aistudio"),
    "gemini.google.com": ("gemini",),
    "chatgpt.com": ("chatgpt",),
    "chat.openai.com": ("chatgpt",),
    "claude.ai": ("claude",),
}
SUPPORTED_PASTE_DOMAINS: Tuple[str, ...] = tuple(SUPPORTED_PASTE_DOMAIN_TITLE_KEYWORDS.keys())
_ORIG_FOCUS_DEFAULT_BROWSER = focus_default_browser
DEFAULT_BUNDLE_FILENAMES: Tuple[str, ...] = (
    "architecture_bundle.txt",
    "architecture_bundle.md",
    "architecture_bundle.xml",
)


def is_interactive_environment() -> bool:
    """標準出力がTTYかつCI環境でない対話実行かどうかを判定する。"""
    return bool(sys.stdout is not None and sys.stdout.isatty()) and not bool(os.environ.get("CI"))


def validate_browser_url(raw_url: str) -> str:
    """ブラウザ起動用URLが安全なhttp/httpsスキームであることを検証する。"""
    cleaned = (raw_url or "").strip()
    if not cleaned:
        raise ValueError("URLが空です")
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"httpまたはhttps形式の有効なURLを指定してください: {raw_url}")
    return cleaned


def open_browser_url(url: str, interactive_only: bool = True) -> bool:
    """デフォルトブラウザで指定URLを新規タブとして開く。"""
    # 非対話環境（テスト実行やCI）での意図しない実ブラウザ起動を防ぐ
    if interactive_only and not is_interactive_environment():
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _resolve_existing_bundle_path(project_root: Path, output_dir: Path) -> Optional[Path]:
    configured = load_bundle_filename_from_meta(project_root, output_dir)
    if configured:
        candidate = output_dir / configured
        if candidate.is_file():
            return candidate
        # 自動生成デフォルト値と一致する場合のみ他拡張子へフォールバックし、明示的カスタム設定の誤代替を防ぐ
        if configured != DEFAULT_BUNDLE_FILENAMES[0]:
            return None
    for name in DEFAULT_BUNDLE_FILENAMES:
        candidate = output_dir / name
        if candidate.is_file():
            return candidate
    return None


def _find_conflicting_sync_options(parsed_args: argparse.Namespace) -> List[str]:
    conflicts: List[str] = []
    if getattr(parsed_args, "full_path", []):
        conflicts.append("-f/--full-path")
    if getattr(parsed_args, "keep_func", []):
        conflicts.append("-k/--keep-func")
    if getattr(parsed_args, "focus", []):
        conflicts.append("--focus")
    if getattr(parsed_args, "focus_deps", False):
        conflicts.append("--focus-deps")
    if getattr(parsed_args, "only_nodes", []):
        conflicts.append("--only-nodes")
    if getattr(parsed_args, "_explicit_format", False) or getattr(parsed_args, "format", "txt") != "txt":
        conflicts.append("--format")
    if getattr(parsed_args, "policy", None) is not None:
        conflicts.append("--policy")
    if getattr(parsed_args, "force", False):
        conflicts.append("--force")
    if getattr(parsed_args, "no_ui", False):
        conflicts.append("--no-ui")
    if getattr(parsed_args, "git_diff", False):
        conflicts.append("-g/--git-diff")
    return conflicts


def _copy_bundle_to_clipboard(
    bundle_path: Optional[Path],
    project_root: Path,
    clipboard_writer: Callable[[str], None],
    no_sync: bool = False,
    no_bundle: bool = False,
) -> bool:
    if no_bundle:
        print("ℹ --no-bundle が指定されているためバンドルのクリップボードコピーをスキップしました。")
        return False
    if bundle_path is None or not bundle_path.is_file():
        hint = "（--no-sync の場合は事前にバンドルを生成してください）" if no_sync else ""
        print(
            f"⚠ バンドルファイルが見つからないためクリップボードへのコピーをスキップしました{hint}。直前のクリップボード内容のままです。",
            file=sys.stderr,
        )
        return False
    try:
        content = bundle_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"⚠ バンドルファイルの読み込みに失敗しました: {e}", file=sys.stderr)
        return False
    if not content.strip():
        print("⚠ バンドルファイルが空のためクリップボードへのコピーをスキップしました。", file=sys.stderr)
        return False

    try:
        clipboard_writer(content)
        est_tokens = estimate_tokens(content)
        try:
            display_path = bundle_path.relative_to(project_root)
        except ValueError:
            display_path = bundle_path
        print(
            f"\n📋 バンドルをクリップボードにコピーしました: {display_path} "
            f"(文字数: {len(content):,} 文字, 推定約 {est_tokens:,} tokens)"
        )
        return True
    except Exception as e:
        print(f"⚠ クリップボードへのコピーに失敗しました: {e}", file=sys.stderr)
        return False


def get_allowed_title_keywords_for_url(url: str) -> Optional[Tuple[str, ...]]:
    """起動URLのドメインに対応する許可ウィンドウタイトルキーワードを返す。"""
    hostname = (urlsplit((url or "").strip()).hostname or "").lower().rstrip(".")
    if not hostname:
        return None
    for domain, keywords in SUPPORTED_PASTE_DOMAIN_TITLE_KEYWORDS.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return keywords
    return None


def is_supported_paste_url(url: str) -> bool:
    """起動時自動貼り付けの対象となるAIチャットサービスのURLか判定する。"""
    return get_allowed_title_keywords_for_url(url) is not None


def _is_focus_success(res: object) -> bool:
    return res is not None and res is not False


def _focus_with_retry(
    focuser: Callable[[], Union[bool, int, str, None]],
    retry_delays: Sequence[float],
    stop_event: threading.Event,
) -> Optional[Union[bool, int, str]]:
    res = focuser()
    if _is_focus_success(res):
        return res
    for delay in retry_delays:
        if stop_event.wait(delay):
            return None
        res = focuser()
        if _is_focus_success(res):
            return res
    return None


def _is_loading_wait_reason(reason: str) -> bool:
    norm = (reason or "").lower()
    return any(kw in norm for kw in ("読み込み", "ページ遷移", "描画更新", "loading"))


def _is_non_loading_mismatch_reason(reason: str) -> bool:
    text = reason or ""
    return text.startswith("許可対象外のページ") or text.startswith("ページ内に対象のプロンプト入力欄が見つかりませんでした")


def _call_paste_to_browser_prompt_safe(**kwargs: Any) -> PasteResult:
    try:
        sig = inspect.signature(paste_to_browser_prompt)
        params = sig.parameters
        if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):
        pass
    return paste_to_browser_prompt(**kwargs)


def _paste_with_retry(
    paster: Callable[..., PasteResult],
    target_window: Optional[Union[int, str]],
    retry_delays: Sequence[float],
    stop_event: threading.Event,
    timeout_sec: float = DEFAULT_PASTE_TIMEOUT_SEC,
    refocus_fn: Optional[Callable[[], Union[bool, int, str, None]]] = None,
) -> PasteResult:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    current_target = target_window
    result = paster(target_window=current_target)
    if result.target_window is not None:
        current_target = result.target_window
    if result.success or not result.retryable or stop_event.is_set():
        return result

    printed_waiting = False
    non_loading_mismatch_count = 0
    window_lost_count = 0
    cap_long_retries = len(retry_delays) > 5

    for delay in retry_delays:
        if _is_non_loading_mismatch_reason(result.reason):
            non_loading_mismatch_count += 1
            if cap_long_retries and non_loading_mismatch_count > 4:
                return result
        else:
            non_loading_mismatch_count = 0

        if result.error_kind == PasteErrorKind.WINDOW_LOST:
            window_lost_count += 1
            if cap_long_retries and window_lost_count > 3:
                return result
        else:
            window_lost_count = 0

        remaining = deadline - time.monotonic()
        if remaining <= 0 or delay > remaining:
            return result

        if not printed_waiting and _is_loading_wait_reason(result.reason):
            print("⏳ サイトの読み込み完了を待機してから自動貼り付けを実行します...", flush=True)
            printed_waiting = True

        if delay > 0 and stop_event.wait(delay):
            return result
        if time.monotonic() >= deadline or stop_event.is_set():
            return result

        if result.error_kind == PasteErrorKind.WINDOW_LOST:
            if refocus_fn is None:
                current_target = None
            else:
                refocused = refocus_fn()
                if not _is_focus_success(refocused):
                    # 一時的なウィンドウ再生成中は残りリトライ回数を消化して再試行する
                    continue
                current_target = None if isinstance(refocused, bool) else refocused
                if time.monotonic() >= deadline or stop_event.is_set():
                    return result

        result = paster(target_window=current_target)
        if result.target_window is not None:
            current_target = result.target_window
        if result.success or not result.retryable or stop_event.is_set():
            return result
    return result


def handle_start_command(
    parsed_args: argparse.Namespace,
    on_run_command: Callable[[List[str]], int],
    sync_runner: Optional[Callable[..., object]] = None,
    clipboard_writer: Optional[Callable[[str], None]] = None,
    browser_opener: Optional[Callable[[str], bool]] = None,
    browser_focuser: Optional[Callable[[], Union[bool, int, str, None]]] = None,
    focus_retry_delays: Sequence[float] = DEFAULT_FOCUS_RETRY_DELAYS,
    prompt_paster: Optional[Callable[..., PasteResult]] = None,
    paste_retry_delays: Sequence[float] = DEFAULT_PASTE_RETRY_DELAYS,
    paste_settle_delay: float = DEFAULT_PASTE_SETTLE_DELAY,
    paste_timeout_sec: float = DEFAULT_PASTE_TIMEOUT_SEC,
) -> int:
    """プロジェクト同期・バンドルコピー・ブラウザ起動・AI Studio監視ブリッジ待機を一括開始する。"""
    project_root: Path = parsed_args.project_dir.resolve()
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたプロジェクトディレクトリが存在しません: {project_root}", file=sys.stderr)
        return 1

    output_dir: Path = resolve_output_dir(project_root, getattr(parsed_args, "output_dir", None))
    if project_root == output_dir:
        print("エラー: ソースディレクトリと出力先ディレクトリに同じパスは指定できません。", file=sys.stderr)
        return 1

    port = getattr(parsed_args, "port", DEFAULT_BRIDGE_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not (0 <= port <= 65535):
        print(f"エラー: ポート番号は 0〜65535 の範囲で指定してください: {port}", file=sys.stderr)
        return 1

    no_browser = getattr(parsed_args, "no_browser", False)
    target_url = ""
    if not no_browser:
        try:
            target_url = validate_browser_url(getattr(parsed_args, "url", DEFAULT_AISTUDIO_URL))
        except ValueError as e:
            print(f"エラー: ブラウザURLが不正です: {e}", file=sys.stderr)
            return 1

    no_sync = getattr(parsed_args, "no_sync", False)
    if no_sync:
        conflicts = _find_conflicting_sync_options(parsed_args)
        if conflicts:
            print(
                f"エラー: --no-sync 指定時は同期オプション ({', '.join(conflicts)}) を併用できません。",
                file=sys.stderr,
            )
            return 1

    no_bundle = getattr(parsed_args, "no_bundle", False)
    no_clipboard = getattr(parsed_args, "no_clipboard", False)
    bundle_path: Optional[Path] = None
    printed_prior = False

    if not no_sync:
        resolved_full_paths = {
            (project_root / Path(p)).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            for p in process_comma_separated_args(getattr(parsed_args, "full_path", []))
        }
        policy_arg = getattr(parsed_args, "policy", None)
        config = SkeletonConfig(
            full_code_paths=resolved_full_paths,
            keep_functions=process_comma_separated_args(getattr(parsed_args, "keep_func", [])),
            only_nodes=process_comma_separated_args(getattr(parsed_args, "only_nodes", [])),
            create_bundle=not no_bundle,
            bundle_format=getattr(parsed_args, "format", "txt"),
            policy_path=policy_arg.resolve() if policy_arg else None,
        )
        run_sync_fn = sync_runner or run_sync
        sync_result = run_sync_fn(
            project_root,
            output_dir,
            config,
            force=getattr(parsed_args, "force", False),
            focus_paths=process_comma_separated_args(getattr(parsed_args, "focus", [])),
            focus_deps=getattr(parsed_args, "focus_deps", False),
            git_diff=getattr(parsed_args, "git_diff", False),
            no_ui=getattr(parsed_args, "no_ui", False),
            quiet=False,
        )
        printed_prior = True
        if isinstance(sync_result, tuple) and len(sync_result) >= 4:
            bundle_path = sync_result[3]
    elif not no_bundle and not no_clipboard:
        bundle_path = _resolve_existing_bundle_path(project_root, output_dir)

    copied_bundle = False
    if not no_clipboard:
        writer = clipboard_writer or set_clipboard_text
        copied_bundle = _copy_bundle_to_clipboard(
            bundle_path,
            project_root,
            writer,
            no_sync=no_sync,
            no_bundle=no_bundle,
        )
        printed_prior = True

    no_focus = getattr(parsed_args, "no_focus", False)
    no_start_focus = getattr(parsed_args, "no_start_focus", False)
    no_auto_focus = getattr(parsed_args, "no_auto_focus", False)
    no_paste = getattr(parsed_args, "no_paste", False)
    no_start_paste = getattr(parsed_args, "no_start_paste", False)
    no_auto_paste = getattr(parsed_args, "no_auto_paste", False)
    # --no-browser時は明示起動しないため起動時フォーカス奪取を抑止し、自動実行時のみ独立設定に従う
    should_focus_on_start = (not no_browser) and (not no_focus) and (not no_start_focus)
    paste_requested_on_start = (not no_browser) and copied_bundle and (not no_paste) and (not no_start_paste)
    allowed_title_keywords = get_allowed_title_keywords_for_url(target_url) if not no_browser else None
    supported_paste_url = allowed_title_keywords is not None
    should_paste_on_start = should_focus_on_start and paste_requested_on_start and supported_paste_url
    bridge_no_focus = no_focus or no_auto_focus
    bridge_no_paste = no_paste or no_auto_paste

    def _default_start_focuser() -> Union[bool, int, str, None]:
        # 既存テスト等が cli_start.focus_default_browser を差し替えた場合も後方互換を保つ
        if focus_default_browser is not _ORIG_FOCUS_DEFAULT_BROWSER:
            return focus_default_browser(interactive_only=True)
        return focus_default_browser_target(interactive_only=True)

    opener = browser_opener or (lambda url: open_browser_url(url, interactive_only=True))
    focuser = browser_focuser or _default_start_focuser
    paster = prompt_paster or (
        lambda target_window=None: _call_paste_to_browser_prompt_safe(
            interactive_only=True,
            target_window=target_window,
            allowed_title_keywords=allowed_title_keywords,
            quick_probe=True,
            verify_stability=True,
        )
    )

    def _on_bridge_ready(stop_event: threading.Event) -> None:
        if stop_event.is_set():
            return

        opened = False
        if browser_opener is None and not is_interactive_environment():
            print("ℹ 非対話環境（非TTYまたはCI）のためブラウザの自動起動をスキップしました。", flush=True)
        else:
            try:
                opened = bool(opener(target_url))
                if stop_event.is_set():
                    return
                if opened:
                    print(f"🌐 ブラウザで開きました: {target_url}", flush=True)
                else:
                    print(f"⚠ ブラウザでURLを開けませんでした: {target_url}", file=sys.stderr, flush=True)
            except Exception as e:
                if not stop_event.is_set():
                    print(f"⚠ ブラウザの起動に失敗しました: {e}", file=sys.stderr, flush=True)

        if stop_event.is_set():
            return

        # ブラウザ起動に成功した場合のみ最前面表示を行い、未起動時や失敗時の誤フォーカスを防ぐ
        focus_result: Optional[Union[bool, int, str]] = None
        if should_focus_on_start and opened:
            if browser_focuser is None and not is_interactive_environment():
                print("ℹ 非対話環境（非TTYまたはCI）のためブラウザの最前面表示をスキップしました。", flush=True)
            else:
                try:
                    # 新規起動時はウィンドウ生成までタイムラグがあるため段階的にリトライする
                    focus_result = _focus_with_retry(focuser, retry_delays=focus_retry_delays, stop_event=stop_event)
                    if stop_event.is_set():
                        return
                    if _is_focus_success(focus_result):
                        print("🌐 起動済みのデフォルトブラウザを最前面に表示しました。", flush=True)
                    else:
                        print("⚠ 起動済みのデフォルトブラウザを検出できず、最前面表示をスキップしました。", file=sys.stderr, flush=True)
                except Exception as e:
                    if not stop_event.is_set():
                        print(f"⚠ ブラウザの最前面表示に失敗しました: {e}", file=sys.stderr, flush=True)
        elif paste_requested_on_start and opened and supported_paste_url and (no_focus or no_start_focus):
            print("ℹ --no-focus / --no-start-focus が指定されているため、起動時の自動貼り付けもスキップしました。", flush=True)

        if stop_event.is_set():
            return

        if opened and paste_requested_on_start and should_focus_on_start and not supported_paste_url:
            print(f"ℹ 自動貼り付け対応外のURLのため起動時の自動貼り付けをスキップしました: {target_url}", flush=True)

        if should_paste_on_start and opened and _is_focus_success(focus_result) and not stop_event.is_set():
            if prompt_paster is None and not is_interactive_environment():
                print("ℹ 非対話環境（非TTYまたはCI）のため自動貼り付けをスキップしました。", flush=True)
            else:
                try:
                    # フォーカス確定後からタブ遷移・SPA描画完了まで待機し、既存タブへの誤貼り付けを防ぐ
                    if paste_settle_delay > 0 and stop_event.wait(paste_settle_delay):
                        return

                    # Pythonのboolはint継承のためTrueをHWNDとして渡さないよう除外する
                    target_window = None if isinstance(focus_result, bool) else focus_result
                    paste_res = _paste_with_retry(
                        paster,
                        target_window=target_window,
                        retry_delays=paste_retry_delays,
                        stop_event=stop_event,
                        timeout_sec=paste_timeout_sec,
                        refocus_fn=focuser,
                    )
                    if stop_event.is_set():
                        return
                    if paste_res.success:
                        print("📝 プロンプト入力欄に貼り付けて自動入力しました。", flush=True)
                    elif paste_res.reason:
                        print(f"ℹ 自動貼り付けをスキップしました: {paste_res.reason}", flush=True)
                except Exception as e:
                    if not stop_event.is_set():
                        print(f"⚠ 自動貼り付けに失敗しました: {e}", file=sys.stderr, flush=True)

    if printed_prior:
        print()

    return run_bridge_server(
        project_root=project_root,
        on_run_command=on_run_command,
        host=getattr(parsed_args, "host", DEFAULT_BRIDGE_HOST),
        port=port,
        explicit_token=getattr(parsed_args, "token", None),
        extension_dir_arg=getattr(parsed_args, "extension_dir", None),
        no_focus=bridge_no_focus,
        no_paste=bridge_no_paste,
        on_ready=_on_bridge_ready if not no_browser else None,
    )
