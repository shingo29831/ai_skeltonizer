"""Module: @role: Windows UI Automation COM APIを直接呼び出してプロンプト入力欄へフォーカスし、修飾キー干渉を排除したWin32 SendInputで貼り付けを実行する。"""
import ctypes
import time
from typing import Optional, Sequence

from .browser_focus.windows import _get_windows_default_browser_exe_names
from .browser_paste_types import (
    PasteErrorKind,
    PasteOutcome,
    is_loading_or_unready_title,
    is_login_or_auth_title,
)
from .browser_paste_windows_uia import (
    _CLSID_CUIAutomation,
    _CLSCTX_INPROC_SERVER,
    _COINIT_APARTMENTTHREADED,
    _GUID,
    _IID_IUIAutomation,
    _IS_64BIT,
    _RPC_E_CHANGED_MODE,
    _TreeScope_Descendants,
    _UIA_AriaPropertiesPropertyId,
    _UIA_AriaRolePropertyId,
    _UIA_BoundingRectanglePropertyId,
    _UIA_ControlTypePropertyId,
    _UIA_DocumentControlTypeId,
    _UIA_EditControlTypeId,
    _UIA_FullDescriptionPropertyId,
    _UIA_HasKeyboardFocusPropertyId,
    _UIA_HelpTextPropertyId,
    _UIA_IsEnabledPropertyId,
    _UIA_IsKeyboardFocusablePropertyId,
    _UIA_IsOffscreenPropertyId,
    _UIA_IsPasswordPropertyId,
    _UIA_NamePropertyId,
    _UIA_TextControlTypeId,
    _UIA_ValueIsReadOnlyPropertyId,
    _UIA_ValueValuePropertyId,
    _VARIANT,
    _VT_ARRAY,
    _VT_BOOL,
    _VT_BSTR,
    _VT_I4,
    _VT_R8,
    _com_call,
    _com_release,
    _find_visible_active_document,
    _score_candidate,
    _uia_create_bstr_condition,
    _uia_create_int_condition,
    _uia_create_or_condition,
    _uia_create_true_condition,
    _uia_find_all,
    _uia_find_first,
    _uia_get_bounding_rect,
    _uia_get_prop_bool,
    _uia_get_prop_str,
    find_best_prompt_element,
    is_browser_page_loading,
    is_prompt_element_value_empty,
    verify_element_focused,
)
from .browser_paste_windows_win32 import (
    _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
    _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    _HARDWAREINPUT,
    _INPUT,
    _INPUT_KEYBOARD,
    _INPUT_MOUSE,
    _INPUT_UNION,
    _KEYBDINPUT,
    _KEYEVENTF_KEYUP,
    _MOUSEEVENTF_LEFTDOWN,
    _MOUSEEVENTF_LEFTUP,
    _MOUSEINPUT,
    _POINT,
    _PROCESS_QUERY_LIMITED_INFORMATION,
    _VK_CONTROL,
    _VK_LCONTROL,
    _VK_LMENU,
    _VK_LSHIFT,
    _VK_LWIN,
    _VK_MENU,
    _VK_RCONTROL,
    _VK_RETURN,
    _VK_RMENU,
    _VK_RSHIFT,
    _VK_RWIN,
    _VK_SHIFT,
    _VK_V,
    _click_safe_region_preserving_cursor,
    _get_hwnd_process_exe_name,
    _get_window_title,
    _init_paste_win32_apis,
    _open_uia_window_root,
    _resolve_foreground_browser_window,
    _send_ctrl_enter_clean,
    _send_ctrl_key_clean,
    _send_ctrl_v_clean,
    _send_key_release_if_pressed,
    _validate_browser_window_title,
    _validate_foreground_hwnd,
)

_STABILITY_CHECK_DELAY_SEC = 0.35

_find_best_prompt_element = find_best_prompt_element
_is_browser_page_loading = is_browser_page_loading
_verify_element_focused = verify_element_focused


def paste_windows_browser_prompt(
    prompt_labels: Sequence[str],
    target_hwnd: Optional[int] = None,
    allowed_title_keywords: Sequence[str] = (),
    allow_non_empty: bool = False,
    quick_probe: bool = False,
    verify_stability: bool = False,
) -> PasteOutcome:
    """Windowsブラウザの対象プロンプト入力欄を検出し、フォーカスしてCtrl+Vを送信する。"""
    exe_names = set(_get_windows_default_browser_exe_names())
    if not exe_names:
        return PasteOutcome(False, "デフォルトブラウザの実行ファイルが特定できませんでした", False, PasteErrorKind.UNSUPPORTED)

    user32, kernel32, ole32, oleaut32 = _init_paste_win32_apis()
    hwnd, resolve_err = _resolve_foreground_browser_window(
        user32,
        kernel32,
        exe_names,
        target_hwnd,
        allow_closed_target_follow=True,
        get_hwnd_exe_fn=_get_hwnd_process_exe_name,
    )
    if resolve_err is not None or hwnd is None:
        return resolve_err or PasteOutcome(False, "対象ブラウザが最前面にありません", True, PasteErrorKind.WINDOW_LOST)

    title = _get_window_title(hwnd, user32)
    title_err = _validate_browser_window_title(
        title,
        hwnd,
        allowed_title_keywords,
        action_label="自動貼り付け",
        loading_msg="サイトを読み込み中のため完了を待機しています",
        disallowed_retryable=True,
        disallowed_error_kind=PasteErrorKind.TRANSIENT,
    )
    if title_err is not None:
        return title_err

    p_uia, root_el, should_uninit, open_err = _open_uia_window_root(
        ole32, hwnd, com_call_fn=_com_call, com_release_fn=_com_release
    )
    if open_err is not None or p_uia is None or root_el is None:
        return open_err or PasteOutcome(False, "UI Automationの初期化に失敗しました", False, PasteErrorKind.FAILED, target_window=hwnd)

    target_el: Optional[ctypes.c_void_p] = None
    try:

        if _is_browser_page_loading(p_uia, root_el, oleaut32):
            return PasteOutcome(
                False,
                "Webページを読み込み中のため完了を待機しています",
                True,
                PasteErrorKind.TRANSIENT,
                target_window=hwnd,
            )

        target_el, matched_lbl, err_reason = _find_best_prompt_element(
            p_uia, root_el, oleaut32, prompt_labels, quick_probe=quick_probe
        )
        if target_el is None:
            is_cond_err = "検索条件の生成" in err_reason
            return PasteOutcome(
                False,
                err_reason,
                not is_cond_err,
                PasteErrorKind.FAILED if is_cond_err else PasteErrorKind.TRANSIENT,
                target_window=hwnd,
            )

        if verify_stability:
            # 起動直後のリダイレクトやSPA再描画中の誤爆を防ぐため安定性を再確認し要素を再取得する
            time.sleep(_STABILITY_CHECK_DELAY_SEC)
            fg_err = _validate_foreground_hwnd(
                hwnd,
                user32,
                kernel32,
                exe_names,
                "ページ安定確認中にフォアグラウンドウィンドウが失われたため再試行します",
                "対象ブラウザウィンドウが新ウィンドウへ切り替わったため再試行します",
                "ページ安定確認中に別ウィンドウへ切り替わったため中断しました",
                get_hwnd_exe_fn=_get_hwnd_process_exe_name,
            )
            if fg_err is not None:
                return fg_err
            mid_title = _get_window_title(hwnd, user32)
            if is_login_or_auth_title(mid_title):
                return PasteOutcome(
                    False,
                    f"認証・ログイン画面のため自動貼り付けを中断しました (title: {mid_title})",
                    False,
                    PasteErrorKind.LOGIN_REQUIRED,
                    target_window=hwnd,
                )
            if mid_title != title or is_loading_or_unready_title(mid_title) or _is_browser_page_loading(p_uia, root_el, oleaut32):
                return PasteOutcome(
                    False,
                    "ページ遷移または読み込み中のため安定完了を待機しています",
                    True,
                    PasteErrorKind.TRANSIENT,
                    target_window=hwnd,
                )
            _com_release(target_el)
            target_el = None
            target_el, matched_lbl, err_reason = _find_best_prompt_element(
                p_uia, root_el, oleaut32, prompt_labels, quick_probe=quick_probe
            )
            if target_el is None:
                is_cond_err = "検索条件の生成" in err_reason
                return PasteOutcome(
                    False,
                    err_reason if is_cond_err else "ページ描画更新中のため再試行します",
                    not is_cond_err,
                    PasteErrorKind.FAILED if is_cond_err else PasteErrorKind.TRANSIENT,
                    target_window=hwnd,
                )

        if not allow_non_empty and not is_prompt_element_value_empty(
            target_el, oleaut32, prompt_labels, matched_lbl
        ):
            return PasteOutcome(
                False,
                "入力欄に既存テキストが存在するため自動貼り付けをスキップしました",
                False,
                PasteErrorKind.USER_ABORTED,
                target_window=hwnd,
            )

        # 物理カーソル移動やオーバーレイ誤爆を避けるためUIA SetFocusを最優先する
        _com_call(target_el, 3, ctypes.c_long, ())
        time.sleep(0.04)
        if not _verify_element_focused(p_uia, target_el, oleaut32):
            if not _click_safe_region_preserving_cursor(user32, target_el, oleaut32):
                return PasteOutcome(False, "プロンプト入力欄へのフォーカス設定に失敗しました", True, PasteErrorKind.TRANSIENT, target_window=hwnd)
            time.sleep(0.06)
            if not _verify_element_focused(p_uia, target_el, oleaut32):
                return PasteOutcome(
                    False,
                    "プロンプト入力欄がフォーカスを受け付けないため再試行します",
                    True,
                    PasteErrorKind.TRANSIENT,
                    target_window=hwnd,
                )

        fg_err = _validate_foreground_hwnd(
            hwnd,
            user32,
            kernel32,
            exe_names,
            "貼り付け直前にフォアグラウンドウィンドウが失われたため再試行します",
            "対象ブラウザウィンドウが新ウィンドウへ切り替わったため再試行します",
            "貼り付け直前に別ウィンドウへ切り替わったため中断しました",
            get_hwnd_exe_fn=_get_hwnd_process_exe_name,
        )
        if fg_err is not None:
            return fg_err

        if not _send_ctrl_v_clean(user32):
            return PasteOutcome(False, "Ctrl+Vキーイベントの送信に失敗しました", False, PasteErrorKind.FAILED, target_window=hwnd)

        return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=hwnd)
    finally:
        _com_release(target_el)
        _com_release(root_el)
        _com_release(p_uia)
        if should_uninit:
            ole32.CoUninitialize()


def trigger_windows_browser_run(
    prompt_labels: Sequence[str],
    target_hwnd: Optional[int] = None,
    allowed_title_keywords: Sequence[str] = (),
) -> PasteOutcome:
    """最前面のWindowsブラウザ対象ページで入力欄へフォーカスし、Ctrl+Enterを送信する。"""
    exe_names = set(_get_windows_default_browser_exe_names())
    if not exe_names:
        return PasteOutcome(False, "デフォルトブラウザの実行ファイルが特定できませんでした", False, PasteErrorKind.UNSUPPORTED)

    user32, kernel32, ole32, oleaut32 = _init_paste_win32_apis()
    hwnd, resolve_err = _resolve_foreground_browser_window(
        user32,
        kernel32,
        exe_names,
        target_hwnd,
        allow_closed_target_follow=False,
        strict_target_mismatch_msg="別ウィンドウが最前面のため自動送信を中断しました",
        get_hwnd_exe_fn=_get_hwnd_process_exe_name,
    )
    if resolve_err is not None or hwnd is None:
        return resolve_err or PasteOutcome(False, "対象ブラウザが最前面にありません", True, PasteErrorKind.WINDOW_LOST)

    title = _get_window_title(hwnd, user32)
    title_err = _validate_browser_window_title(
        title,
        hwnd,
        allowed_title_keywords,
        action_label="自動送信",
        loading_msg="サイトを読み込み中のため自動送信をスキップしました",
        disallowed_retryable=False,
        disallowed_error_kind=PasteErrorKind.USER_ABORTED,
    )
    if title_err is not None:
        return title_err

    p_uia, root_el, should_uninit, open_err = _open_uia_window_root(
        ole32, hwnd, com_call_fn=_com_call, com_release_fn=_com_release
    )
    if open_err is not None or p_uia is None or root_el is None:
        return open_err or PasteOutcome(False, "UI Automationの初期化に失敗しました", False, PasteErrorKind.FAILED, target_window=hwnd)

    target_el: Optional[ctypes.c_void_p] = None
    doc_el: Optional[ctypes.c_void_p] = None
    try:

        target_el, _, _ = _find_best_prompt_element(p_uia, root_el, oleaut32, prompt_labels, quick_probe=True)
        if target_el is not None:
            if not _verify_element_focused(p_uia, target_el, oleaut32):
                _com_call(target_el, 3, ctypes.c_long, ())
                time.sleep(0.04)
                if not _verify_element_focused(p_uia, target_el, oleaut32):
                    _click_safe_region_preserving_cursor(user32, target_el, oleaut32)
                    time.sleep(0.05)
                    if not _verify_element_focused(p_uia, target_el, oleaut32):
                        return PasteOutcome(False, "プロンプト入力欄へのフォーカス設定に失敗しました", True, PasteErrorKind.TRANSIENT, target_window=hwnd)
        else:
            # プレースホルダー変化時はDocument領域にフォーカスがあることを確認しアドレスバー誤爆を防ぐ
            doc_el = _find_visible_active_document(p_uia, root_el, oleaut32)
            if doc_el is None or not _verify_element_focused(p_uia, doc_el, oleaut32):
                return PasteOutcome(False, "送信対象のプロンプト入力欄を特定できませんでした", False, PasteErrorKind.FAILED, target_window=hwnd)

        fg_err = _validate_foreground_hwnd(
            hwnd,
            user32,
            kernel32,
            exe_names,
            "送信直前にフォアグラウンドウィンドウが失われました",
            "対象ブラウザウィンドウが切り替わったため中断しました",
            "送信直前に別ウィンドウへ切り替わったため中断しました",
            get_hwnd_exe_fn=_get_hwnd_process_exe_name,
        )
        if fg_err is not None:
            return fg_err

        if not _send_ctrl_enter_clean(user32):
            return PasteOutcome(False, "Ctrl+Enterキーイベントの送信に失敗しました", False, PasteErrorKind.FAILED, target_window=hwnd)

        return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=hwnd)
    finally:
        _com_release(target_el)
        _com_release(doc_el)
        _com_release(root_el)
        _com_release(p_uia)
        if should_uninit:
            ole32.CoUninitialize()
