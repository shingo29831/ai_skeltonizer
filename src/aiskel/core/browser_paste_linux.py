"""Module: @role: Linux環境においてAT-SPIとキーボードエミュレーションを用いてブラウザ内のプロンプト入力欄へフォーカスし、Ctrl+V貼り付けを実行する。"""
import os
import time
from typing import Any, Optional, Sequence, Tuple

from .browser_focus.linux import _get_linux_browser_wm_hints
from .browser_paste_linux_atspi import (
    _clear_atspi_single_cache as _clear_atspi_single_cache,
    _clear_atspi_subtree_cache as _clear_atspi_subtree_cache,
    _collect_atspi_node_texts as _collect_atspi_node_texts,
    _disable_atspi_cache as _disable_atspi_cache,
    _find_atspi_prompt_node as _find_atspi_prompt_node,
    _get_atspi_existing_text as _get_atspi_existing_text,
    _has_focused_atspi_document as _has_focused_atspi_document,
    _is_atspi_document_busy as _is_atspi_document_busy,
    _is_atspi_prompt_empty as _is_atspi_prompt_empty,
    _score_atspi_texts as _score_atspi_texts,
    _verify_atspi_node_focused as _verify_atspi_node_focused,
)
from .browser_paste_linux_window import (
    _LINUX_BROWSER_BINARY_ALIASES as _LINUX_BROWSER_BINARY_ALIASES,
    _build_linux_hint_norms as _build_linux_hint_norms,
    _get_x11_active_window_id as _get_x11_active_window_id,
    _has_any_active_atspi_window as _has_any_active_atspi_window,
    _is_atspi_app_default_browser as _is_atspi_app_default_browser,
    _is_x11_window_alive as _is_x11_window_alive,
    _matches_browser_hint as _matches_browser_hint,
    _normalize_linux_token as _normalize_linux_token,
    _parse_x11_window_id as _parse_x11_window_id,
    _send_linux_ctrl_enter as _send_linux_ctrl_enter,
    _send_linux_ctrl_v as _send_linux_ctrl_v,
    _validate_linux_window_active as _validate_linux_window_active,
    _wait_active_atspi_window as _wait_active_atspi_window,
)
from .browser_paste_types import (
    PasteErrorKind,
    PasteOutcome,
    is_loading_or_unready_title,
    is_login_or_auth_title,
)

def _prepare_linux_atspi_context(
    prompt_labels: Optional[Sequence[str]] = None,
    require_prompt_labels: bool = False,
) -> Tuple[Optional[Tuple[bool, bool, Sequence[str], Any]], Optional[PasteOutcome]]:
    has_x11 = bool(os.environ.get("DISPLAY"))
    has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if not has_x11 and not has_wayland:
        return None, PasteOutcome(False, "GUIディスプレイ環境変数が設定されていません", False, PasteErrorKind.UNSUPPORTED)
    if require_prompt_labels and not prompt_labels:
        return None, PasteOutcome(False, "プロンプトラベルが指定されていません", False, PasteErrorKind.FAILED)

    browser_hints = _get_linux_browser_wm_hints()
    if not browser_hints:
        return None, PasteOutcome(False, "デフォルトブラウザの実行情報が特定できませんでした", False, PasteErrorKind.UNSUPPORTED)

    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as e:
        return None, PasteOutcome(False, f"PyGObject (Atspi 2.0) が利用できません: {e}", False, PasteErrorKind.UNSUPPORTED)

    return (has_x11, has_wayland, browser_hints, Atspi), None


def paste_linux_browser_prompt(
    prompt_labels: Sequence[str],
    target_window_id: Optional[str] = None,
    allowed_title_keywords: Sequence[str] = (),
    allow_non_empty: bool = False,
    quick_probe: bool = False,
    verify_stability: bool = False,
) -> PasteOutcome:
    """Linuxのアクティブブラウザ上でプロンプト入力欄をフォーカスし、Ctrl+Vを送信する。"""
    ctx, err = _prepare_linux_atspi_context(prompt_labels=prompt_labels, require_prompt_labels=True)
    if err is not None:
        return err
    assert ctx is not None
    has_x11, has_wayland, browser_hints, Atspi = ctx

    timeout_sec = 0.5 if quick_probe else 1.0
    active_win, win_err, retryable, err_kind = _wait_active_atspi_window(
        Atspi, allowed_title_keywords, browser_hints, timeout_sec=timeout_sec
    )
    if active_win is None:
        return PasteOutcome(False, win_err, retryable, err_kind)

    resolved_win_id: Optional[str] = target_window_id
    expected_x11_id: Optional[int] = None
    target_x11_id = _parse_x11_window_id(target_window_id)
    if has_x11 and target_x11_id is not None:
        active_x11_id = _get_x11_active_window_id()
        if active_x11_id is not None:
            if active_x11_id != target_x11_id:
                if _is_x11_window_alive(target_x11_id):
                    return PasteOutcome(
                        False,
                        "別のブラウザウィンドウまたは別アプリが最前面になったため中断しました",
                        False,
                        PasteErrorKind.USER_ABORTED,
                    )
                # 一時ウィンドウ終了後にデフォルトブラウザの新ウィンドウへ遷移した場合のみ追従する
                resolved_win_id = hex(active_x11_id)
                expected_x11_id = active_x11_id
            else:
                expected_x11_id = target_x11_id

    if _is_atspi_document_busy(active_win, Atspi):
        return PasteOutcome(
            False,
            "Webページを読み込み中のため完了を待機しています",
            True,
            PasteErrorKind.TRANSIENT,
            target_window=resolved_win_id,
        )

    labels_lower = [lbl.strip().lower() for lbl in prompt_labels if lbl.strip()]
    roles = {Atspi.Role.ENTRY, Atspi.Role.TEXT}
    found = _find_atspi_prompt_node(active_win, labels_lower, roles, Atspi)
    if found is None:
        return PasteOutcome(
            False,
            "ページ内に対象のプロンプト入力欄が見つかりませんでした",
            True,
            PasteErrorKind.TRANSIENT,
            target_window=resolved_win_id,
        )

    if verify_stability:
        # 起動直後のリダイレクトやSPA再描画中の誤貼付を防ぐため安定性を再確認する
        _clear_atspi_single_cache(active_win)
        try:
            initial_title = (active_win.get_name() or "").strip()
        except Exception:
            initial_title = ""
        time.sleep(0.35)
        win_err_mid = _validate_linux_window_active(
            active_win,
            Atspi,
            browser_hints,
            has_x11,
            expected_x11_id,
            "ページ安定確認中に別ウィンドウへ切り替わったため中断しました",
        )
        if win_err_mid is not None:
            return win_err_mid
        _clear_atspi_single_cache(active_win)
        try:
            mid_title = (active_win.get_name() or "").strip()
        except Exception:
            return PasteOutcome(
                False,
                "ページ安定確認に失敗したため再試行します",
                True,
                PasteErrorKind.TRANSIENT,
                target_window=resolved_win_id,
            )
        if is_login_or_auth_title(mid_title):
            return PasteOutcome(
                False,
                f"認証・ログイン画面のため自動貼り付けを中断しました (title: {mid_title})",
                False,
                PasteErrorKind.LOGIN_REQUIRED,
                target_window=resolved_win_id,
            )
        if mid_title != initial_title or is_loading_or_unready_title(mid_title) or _is_atspi_document_busy(active_win, Atspi):
            return PasteOutcome(
                False,
                "ページ遷移または読み込み中のため安定完了を待機しています",
                True,
                PasteErrorKind.TRANSIENT,
                target_window=resolved_win_id,
            )
        _clear_atspi_subtree_cache(active_win)
        found = _find_atspi_prompt_node(active_win, labels_lower, roles, Atspi)
        if found is None:
            return PasteOutcome(
                False,
                "ページ描画更新中のため再試行します",
                True,
                PasteErrorKind.TRANSIENT,
                target_window=resolved_win_id,
            )

    target_node, _, matched_lbl = found
    if not allow_non_empty and not _is_atspi_prompt_empty(target_node, labels_lower, matched_lbl):
        return PasteOutcome(
            False,
            "入力欄に既存テキストが存在するため自動貼り付けをスキップしました",
            False,
            PasteErrorKind.USER_ABORTED,
            target_window=resolved_win_id,
        )

    comp = target_node.get_component_iface()
    if comp is None or not comp.grab_focus():
        return PasteOutcome(
            False,
            "プロンプト入力欄へのフォーカス設定に失敗しました",
            True,
            PasteErrorKind.TRANSIENT,
            target_window=resolved_win_id,
        )

    time.sleep(0.05)
    if not _verify_atspi_node_focused(target_node, Atspi):
        comp.grab_focus()
        time.sleep(0.06)
        if not _verify_atspi_node_focused(target_node, Atspi):
            return PasteOutcome(
                False,
                "プロンプト入力欄がフォーカスを受け付けないため再試行します",
                True,
                PasteErrorKind.TRANSIENT,
                target_window=resolved_win_id,
            )

    win_err_final = _validate_linux_window_active(
        active_win,
        Atspi,
        browser_hints,
        has_x11,
        expected_x11_id,
        "貼り付け直前に別ウィンドウへ切り替わったため中断しました",
    )
    if win_err_final is not None:
        return win_err_final

    return _send_linux_ctrl_v(is_wayland_only=(has_wayland and not has_x11), target_window=resolved_win_id)


def trigger_linux_browser_run(
    prompt_labels: Sequence[str],
    target_window_id: Optional[str] = None,
    allowed_title_keywords: Sequence[str] = (),
) -> PasteOutcome:
    """Linuxのアクティブブラウザ対象ページで入力欄へフォーカスし、Ctrl+Enterを送信する。"""
    ctx, err = _prepare_linux_atspi_context(require_prompt_labels=False)
    if err is not None:
        return err
    assert ctx is not None
    has_x11, has_wayland, browser_hints, Atspi = ctx

    active_win, win_err, _, err_kind = _wait_active_atspi_window(
        Atspi, allowed_title_keywords, browser_hints, timeout_sec=0.5
    )
    if active_win is None:
        return PasteOutcome(False, win_err, False, err_kind)

    target_x11_id = _parse_x11_window_id(target_window_id)
    if has_x11 and target_x11_id is not None:
        active_x11_id = _get_x11_active_window_id()
        if active_x11_id is not None and active_x11_id != target_x11_id:
            return PasteOutcome(False, "別ウィンドウが最前面のため自動送信を中断しました", False, PasteErrorKind.USER_ABORTED)

    labels_lower = [lbl.strip().lower() for lbl in prompt_labels if lbl.strip()]
    roles = {Atspi.Role.ENTRY, Atspi.Role.TEXT}
    found = _find_atspi_prompt_node(active_win, labels_lower, roles, Atspi)
    if found is not None:
        if not _verify_atspi_node_focused(found[0], Atspi):
            comp = found[0].get_component_iface()
            if comp is not None:
                comp.grab_focus()
                time.sleep(0.05)
            if not _verify_atspi_node_focused(found[0], Atspi):
                return PasteOutcome(False, "プロンプト入力欄へのフォーカス設定に失敗しました", False, PasteErrorKind.FAILED, target_window=target_window_id)
    elif not _has_focused_atspi_document(active_win, Atspi):
        return PasteOutcome(False, "送信対象のプロンプト入力欄を特定できませんでした", False, PasteErrorKind.FAILED, target_window=target_window_id)

    return _send_linux_ctrl_enter(is_wayland_only=(has_wayland and not has_x11), target_window=target_window_id)
