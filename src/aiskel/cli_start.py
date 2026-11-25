"""Module: @role: コーディング開始時のスケルトン同期・バンドルコピー・既存空タブ再利用・AI Studio監視ブリッジ起動を統合制御する。"""
import argparse
import inspect
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .cli_parser import process_comma_separated_args
from .cli_start_browser import (
    DEFAULT_AISTUDIO_URL,
    DEFAULT_FOCUS_RETRY_DELAYS,
    DEFAULT_PASTE_RETRY_DELAYS,
    DEFAULT_PASTE_SETTLE_DELAY,
    DEFAULT_PASTE_TIMEOUT_SEC,
    SUPPORTED_PASTE_DOMAINS,
    SUPPORTED_PASTE_DOMAIN_TITLE_KEYWORDS,
    StartBrowserReadyConfig,
    call_paste_to_browser_prompt_safe,
    execute_start_browser_ready,
    get_allowed_title_keywords_for_url,
    is_focus_success as _is_focus_success,
    is_interactive_environment,
    is_loading_wait_reason as _is_loading_wait_reason,
    is_non_loading_mismatch_reason as _is_non_loading_mismatch_reason,
    is_supported_paste_url as _is_supported_paste_url_impl,
    open_browser_url as _open_browser_url_impl,
    paste_with_retry as _paste_with_retry,
    validate_browser_url,
)
from .cli_start_bundle import (
    DEFAULT_BUNDLE_FILENAMES,
    build_start_skeleton_config,
    copy_bundle_to_clipboard,
    find_conflicting_sync_options as _find_conflicting_sync_options,
    resolve_existing_bundle_path,
    validate_start_arguments,
)
from .cli_sync import resolve_output_dir, run_sync
from .config import load_bundle_filename_from_meta
from .core.aistudio_bridge import MAX_PATCH_HANDOFF_MAX_TABS, run_bridge_server
from .core.aistudio_extension import DEFAULT_BRIDGE_HOST, DEFAULT_BRIDGE_PORT
from .core.browser_focus import focus_browser_target, focus_default_browser, focus_default_browser_target
from .core.browser_paste import PasteResult, paste_to_browser_prompt
from .core.browser_tab_reuse import focus_existing_empty_chat_tab
from .core.browser_tab_reuse.types import is_new_chat_target_url
from .core.clipboard import set_clipboard_text
from .core.token_counter import estimate_tokens

_ORIG_FOCUS_DEFAULT_BROWSER = focus_default_browser
_ORIG_FOCUS_DEFAULT_BROWSER_TARGET = focus_default_browser_target

def open_browser_url(url: str, interactive_only: bool = True) -> bool:
    """デフォルトブラウザで指定URLを新規タブとして開く。"""
    return _open_browser_url_impl(
        url,
        interactive_only=interactive_only,
        interactive_checker=is_interactive_environment,
        url_opener=webbrowser.open,
    )

def _resolve_existing_bundle_path(project_root: Path, output_dir: Path) -> Optional[Path]:
    return resolve_existing_bundle_path(
        project_root,
        output_dir,
        bundle_filename_loader=load_bundle_filename_from_meta,
    )

def _copy_bundle_to_clipboard(
    bundle_path: Optional[Path],
    project_root: Path,
    clipboard_writer: Callable[[str], None],
    no_sync: bool = False,
    no_bundle: bool = False,
) -> bool:
    return copy_bundle_to_clipboard(
        bundle_path,
        project_root,
        clipboard_writer,
        no_sync=no_sync,
        no_bundle=no_bundle,
        token_estimator=estimate_tokens,
    )

def is_supported_paste_url(url: str) -> bool:
    """起動時自動貼り付けの対象となるAIチャットサービスのURLか判定する。"""
    return _is_supported_paste_url_impl(url, keywords_resolver=get_allowed_title_keywords_for_url)

def _call_paste_to_browser_prompt_safe(**kwargs: Any) -> PasteResult:
    return call_paste_to_browser_prompt_safe(paste_fn=paste_to_browser_prompt, **kwargs)


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
    existing_tab_focuser: Optional[Callable[..., Optional[Union[bool, int, str]]]] = None,
    on_run_structured: Optional[Callable[..., Any]] = None,
) -> int:
    """プロジェクト同期・バンドルコピー・既存空タブ再利用・AI Studio監視ブリッジ待機を一括開始する。"""
    launch_cwd: Path = Path.cwd().resolve()
    project_root: Path = parsed_args.project_dir.resolve()
    valid, output_dir, port, patch_handoff_max_tabs, no_browser, target_url = validate_start_arguments(
        parsed_args,
        project_root,
        output_dir_resolver=resolve_output_dir,
        default_bridge_port=DEFAULT_BRIDGE_PORT,
        max_patch_handoff_max_tabs=MAX_PATCH_HANDOFF_MAX_TABS,
        default_aistudio_url=DEFAULT_AISTUDIO_URL,
        url_validator=validate_browser_url,
        conflict_finder=_find_conflicting_sync_options,
    )
    if not valid or output_dir is None:
        return 1

    no_sync = getattr(parsed_args, "no_sync", False)
    no_bundle = getattr(parsed_args, "no_bundle", False)
    no_clipboard = getattr(parsed_args, "no_clipboard", False)
    bundle_path: Optional[Path] = None
    printed_prior = False

    if not no_sync:
        config = build_start_skeleton_config(parsed_args, project_root, create_bundle=not no_bundle)
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
    auto_run = getattr(parsed_args, "auto_run", None)
    if auto_run is True and (bridge_no_focus or bridge_no_paste):
        print(
            "⚠ 自動フォーカスまたは自動貼り付けが無効化されているため --auto-run を無効化します。",
            file=sys.stderr,
        )
        auto_run = False

    paster = prompt_paster or (
        lambda target_window=None: _call_paste_to_browser_prompt_safe(
            interactive_only=True,
            target_window=target_window,
            allowed_title_keywords=allowed_title_keywords,
            quick_probe=True,
            verify_stability=True,
        )
    )

    is_new_chat_url = is_new_chat_target_url(target_url) if not no_browser else False
    has_custom_focus_monkeypatch = (
        focus_default_browser is not _ORIG_FOCUS_DEFAULT_BROWSER
        or focus_default_browser_target is not _ORIG_FOCUS_DEFAULT_BROWSER_TARGET
    )
    ready_config = StartBrowserReadyConfig(
        target_url=target_url,
        should_focus_on_start=should_focus_on_start,
        paste_requested_on_start=paste_requested_on_start,
        supported_paste_url=supported_paste_url,
        should_paste_on_start=should_paste_on_start,
        is_new_chat_url=is_new_chat_url,
        no_focus=no_focus,
        no_start_focus=no_start_focus,
        allowed_title_keywords=allowed_title_keywords,
        has_custom_focus_monkeypatch=has_custom_focus_monkeypatch,
        paster=paster,
        paste_retry_delays=paste_retry_delays,
        paste_timeout_sec=paste_timeout_sec,
        prompt_paster_provided=prompt_paster is not None,
        browser_opener=browser_opener,
        browser_focuser=browser_focuser,
        existing_tab_focuser=existing_tab_focuser,
        empty_tab_focuser=focus_existing_empty_chat_tab,
        window_refocuser=focus_browser_target,
        interactive_checker=is_interactive_environment,
        paste_retry_fn=_paste_with_retry,
    )

    def _on_bridge_ready(stop_event: threading.Event) -> None:
        execute_start_browser_ready(stop_event, ready_config)

    if printed_prior:
        print()

    bridge_kwargs: Dict[str, Any] = {
        "project_root": project_root,
        "on_run_command": on_run_command,
        "host": getattr(parsed_args, "host", DEFAULT_BRIDGE_HOST),
        "port": port,
        "explicit_token": getattr(parsed_args, "token", None),
        "extension_dir_arg": getattr(parsed_args, "extension_dir", None),
        "no_focus": bridge_no_focus,
        "no_paste": bridge_no_paste,
        "auto_run": auto_run,
        "structured_command_runner": on_run_structured,
        "on_ready": _on_bridge_ready if not no_browser else None,
        "output_dir": output_dir,
        "patch_handoff_max_tabs": patch_handoff_max_tabs,
    }
    try:
        sig = inspect.signature(run_bridge_server)
        if "command_cwd" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            bridge_kwargs["command_cwd"] = launch_cwd
    except (TypeError, ValueError):
        pass

    return run_bridge_server(**bridge_kwargs)
