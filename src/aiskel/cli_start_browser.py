"""Module: @role: startコマンド用のブラウザURL検証・対応ドメイン判定・空チャットタブ再利用・貼り付けリトライ制御を担当する。"""
import inspect
import os
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from .core.browser_focus import focus_browser_target
from .core.browser_paste import PasteErrorKind, PasteResult, paste_to_browser_prompt
from .core.browser_tab_reuse import focus_existing_empty_chat_tab

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

def open_browser_url(
    url: str,
    interactive_only: bool = True,
    *,
    interactive_checker: Optional[Callable[[], bool]] = None,
    url_opener: Optional[Callable[..., bool]] = None,
) -> bool:
    """デフォルトブラウザで指定URLを新規タブとして開く。"""
    checker = interactive_checker or is_interactive_environment
    opener = url_opener or webbrowser.open
    # 非対話環境（テスト実行やCI）での意図しない実ブラウザ起動を防ぐ
    if interactive_only and not checker():
        return False
    try:
        return bool(opener(url, new=2))
    except Exception:
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

def is_supported_paste_url(
    url: str,
    *,
    keywords_resolver: Optional[Callable[[str], Optional[Tuple[str, ...]]]] = None,
) -> bool:
    """起動時自動貼り付けの対象となるAIチャットサービスのURLか判定する。"""
    resolver = keywords_resolver or get_allowed_title_keywords_for_url
    return resolver(url) is not None

def is_focus_success(res: object) -> bool:
    """ブラウザフォーカス操作の戻り値が成功を示すか判定する。"""
    return res is not None and res is not False

def is_loading_wait_reason(reason: str) -> bool:
    """貼り付け失敗理由がページ読込待機を示すか判定する。"""
    norm = (reason or "").lower()
    return any(kw in norm for kw in ("読み込み", "ページ遷移", "描画更新", "loading"))

def is_non_loading_mismatch_reason(reason: str) -> bool:
    """読込中以外の対象外ページ・入力欄未検出理由か判定する。"""
    text = reason or ""
    return text.startswith("許可対象外のページ") or text.startswith("ページ内に対象のプロンプト入力欄が見つかりませんでした")

def call_paste_to_browser_prompt_safe(
    paste_fn: Callable[..., PasteResult] = paste_to_browser_prompt,
    **kwargs: Any,
) -> PasteResult:
    """テストモック等のシグネチャ差異を吸収してブラウザ貼り付け関数を呼び出す。"""
    try:
        sig = inspect.signature(paste_fn)
        params = sig.parameters
        if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):
        pass
    return paste_fn(**kwargs)

def paste_with_retry(
    paster: Callable[..., PasteResult],
    target_window: Optional[Union[int, str]],
    retry_delays: Sequence[float],
    stop_event: threading.Event,
    timeout_sec: float = DEFAULT_PASTE_TIMEOUT_SEC,
    refocus_fn: Optional[Callable[[], Union[bool, int, str, None]]] = None,
) -> PasteResult:
    """ページ読込や一時的なフォーカス喪失を考慮して自動貼り付けをリトライ実行する。"""
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
        if is_non_loading_mismatch_reason(result.reason):
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

        if not printed_waiting and is_loading_wait_reason(result.reason):
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
                if not is_focus_success(refocused):
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

@dataclass
class StartBrowserReadyConfig:
    """ブリッジ起動完了時の既存空チャットタブ検出および自動貼り付け設定を保持する。"""

    target_url: str
    should_focus_on_start: bool
    paste_requested_on_start: bool
    supported_paste_url: bool
    should_paste_on_start: bool
    is_new_chat_url: bool
    no_focus: bool
    no_start_focus: bool
    allowed_title_keywords: Optional[Tuple[str, ...]]
    has_custom_focus_monkeypatch: bool
    paster: Callable[..., PasteResult]
    paste_retry_delays: Sequence[float]
    paste_timeout_sec: float
    prompt_paster_provided: bool
    browser_opener: Optional[Callable[[str], bool]] = None
    browser_focuser: Optional[Callable[[], Union[bool, int, str, None]]] = None
    existing_tab_focuser: Optional[Callable[..., Optional[Union[bool, int, str]]]] = None
    empty_tab_focuser: Callable[..., Optional[Union[bool, int, str]]] = focus_existing_empty_chat_tab
    window_refocuser: Callable[..., Optional[Union[int, str]]] = focus_browser_target
    interactive_checker: Callable[[], bool] = is_interactive_environment
    paste_retry_fn: Callable[..., PasteResult] = paste_with_retry

def execute_start_browser_ready(stop_event: threading.Event, config: StartBrowserReadyConfig) -> None:
    """ブリッジ起動完了後に既存の空チャットタブを再利用し、必要に応じて自動貼り付けを行う。"""
    if stop_event.is_set():
        return

    if not config.should_focus_on_start:
        if config.paste_requested_on_start and config.supported_paste_url and (config.no_focus or config.no_start_focus):
            print("ℹ --no-focus / --no-start-focus が指定されているため、起動時の既存空チャットタブ検出・自動貼り付けをスキップしました。", flush=True)
        return

    if not config.supported_paste_url:
        if config.paste_requested_on_start:
            print(f"ℹ 自動貼り付け対応外のURLのため起動時の既存空チャットタブ検出・自動貼り付けをスキップしました: {config.target_url}", flush=True)
        return

    if not config.is_new_chat_url:
        print(f"ℹ 新規チャットURLではないため起動時の既存空チャットタブ検出・自動貼り付けをスキップしました: {config.target_url}", flush=True)
        return

    # ブラウザ起動・フォーカスがモック注入されていない実制御時のみ実ブラウザの空タブ再利用を行う
    can_probe_real_empty_tab = (
        config.existing_tab_focuser is None
        and config.browser_opener is None
        and config.browser_focuser is None
        and not config.has_custom_focus_monkeypatch
        and config.interactive_checker()
    )
    if config.existing_tab_focuser is None and not can_probe_real_empty_tab:
        if not config.interactive_checker():
            print("ℹ 非対話環境（非TTYまたはCI）のため起動時の既存空チャットタブ検出をスキップしました。", flush=True)
        return

    reused_existing_tab = False
    probe_failed = False
    focus_result: Optional[Union[bool, int, str]] = None
    try:
        reused_focus = (
            config.existing_tab_focuser(config.target_url, config.allowed_title_keywords)
            if config.existing_tab_focuser is not None
            else config.empty_tab_focuser(
                target_url=config.target_url,
                allowed_title_keywords=config.allowed_title_keywords,
                interactive_only=True,
            )
        )
        if stop_event.is_set():
            return
        if is_focus_success(reused_focus):
            reused_existing_tab = True
            focus_result = reused_focus
            print("🌐 起動済みの空チャットタブを検出し、最前面に表示しました。", flush=True)
    except Exception as e:
        probe_failed = True
        if not stop_event.is_set():
            print(f"⚠ 起動済み空チャットタブの検出に失敗しました: {e}", file=sys.stderr, flush=True)

    if stop_event.is_set():
        return
    if not reused_existing_tab:
        if not probe_failed:
            print("ℹ 起動済みの空チャットタブが見つからなかったため、起動時の自動貼り付けをスキップしました。", flush=True)
        return

    if config.should_paste_on_start and is_focus_success(focus_result) and not stop_event.is_set():
        if not config.prompt_paster_provided and not config.interactive_checker():
            print("ℹ 非対話環境（非TTYまたはCI）のため自動貼り付けをスキップしました。", flush=True)
            return
        try:
            # Pythonのboolはint継承のためTrueをHWNDとして渡さないよう除外する
            target_window = None if isinstance(focus_result, bool) else focus_result
            refocus_fn: Callable[[], Union[bool, int, str, None]]
            if target_window is not None and config.browser_focuser is None:
                refocus_fn = lambda: config.window_refocuser(target_window, interactive_only=True)
            elif config.existing_tab_focuser is not None and config.browser_focuser is None:
                refocus_fn = lambda: config.existing_tab_focuser(config.target_url, config.allowed_title_keywords)  # type: ignore[misc]
            elif config.browser_focuser is not None:
                refocus_fn = config.browser_focuser
            else:
                refocus_fn = lambda: config.empty_tab_focuser(
                    target_url=config.target_url,
                    allowed_title_keywords=config.allowed_title_keywords,
                    interactive_only=True,
                )

            paste_res = config.paste_retry_fn(
                config.paster,
                target_window=target_window,
                retry_delays=config.paste_retry_delays,
                stop_event=stop_event,
                timeout_sec=config.paste_timeout_sec,
                refocus_fn=refocus_fn,
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
