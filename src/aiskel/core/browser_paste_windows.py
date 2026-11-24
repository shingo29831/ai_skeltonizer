"""Module: @role: Windows UI Automation COM APIを直接呼び出してプロンプト入力欄へフォーカスし、修飾キー干渉を排除したWin32 SendInputで貼り付けを実行する。"""
import ctypes
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Set, Tuple

from .browser_focus_windows import _get_windows_default_browser_exe_names
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

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STABILITY_CHECK_DELAY_SEC = 0.35

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_KEYEVENTF_KEYUP = 0x0002

_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_V = 0x56
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_VK_LSHIFT = 0xA0
_VK_RSHIFT = 0xA1
_VK_LCONTROL = 0xA2
_VK_RCONTROL = 0xA3
_VK_LMENU = 0xA4
_VK_RMENU = 0xA5

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)

_find_best_prompt_element = find_best_prompt_element
_is_browser_page_loading = is_browser_page_loading
_verify_element_focused = verify_element_focused


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("union", _INPUT_UNION),
    ]


_CLSID_CUIAutomation = _GUID(
    0xFF48DBA4, 0x60EF, 0x4201, (ctypes.c_ubyte * 8)(0xAA, 0x87, 0x54, 0x10, 0x3E, 0xEF, 0x59, 0x4E)
)
_IID_IUIAutomation = _GUID(
    0x30CBE57D, 0xD9D0, 0x452A, (ctypes.c_ubyte * 8)(0xAB, 0x13, 0x7A, 0xC5, 0xAC, 0x48, 0x25, 0xEE)
)


def _init_paste_win32_apis() -> Tuple[Any, Any, Any, Any]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    user32.GetCursorPos.restype = ctypes.c_int
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = ctypes.c_int
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
    user32.MapVirtualKeyW.restype = ctypes.c_uint
    if hasattr(user32, "SetThreadDpiAwarenessContext"):
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p

    # 64bit環境でのHANDLE切り捨てを防ぐため全kernel32 APIに厳密な型を設定する
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long

    oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
    oleaut32.SysAllocString.restype = ctypes.c_void_p
    oleaut32.VariantClear.argtypes = [ctypes.POINTER(_VARIANT)]
    oleaut32.VariantClear.restype = ctypes.c_long
    oleaut32.SafeArrayGetLBound.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
    oleaut32.SafeArrayGetLBound.restype = ctypes.c_long
    oleaut32.SafeArrayGetUBound.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
    oleaut32.SafeArrayGetUBound.restype = ctypes.c_long
    oleaut32.SafeArrayAccessData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    oleaut32.SafeArrayAccessData.restype = ctypes.c_long
    oleaut32.SafeArrayUnaccessData.argtypes = [ctypes.c_void_p]
    oleaut32.SafeArrayUnaccessData.restype = ctypes.c_long

    return user32, kernel32, ole32, oleaut32


def _com_call(obj_ptr: ctypes.c_void_p, index: int, restype: Any, argtypes: Sequence[Any], *args: Any) -> Any:
    vtable = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn_type = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = fn_type(vtable[index])
    return fn(obj_ptr, *args)


def _com_release(obj_ptr: Optional[ctypes.c_void_p]) -> None:
    if obj_ptr and obj_ptr.value:
        _com_call(obj_ptr, 2, ctypes.c_uint32, ())


def _get_hwnd_process_exe_name(hwnd: int, user32: Any, kernel32: Any) -> Optional[str]:
    pid = ctypes.c_uint32(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    h_proc = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid.value)
    if not h_proc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(len(buf))
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return None
    finally:
        kernel32.CloseHandle(h_proc)


def _get_window_title(hwnd: int, user32: Any) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _validate_foreground_hwnd(
    hwnd: int,
    user32: Any,
    kernel32: Any,
    exe_names: Set[str],
    lost_msg: str,
    switched_browser_msg: str,
    switched_other_msg: str,
) -> Optional[PasteOutcome]:
    cur_fg_ptr = user32.GetForegroundWindow()
    cur_fg = int(cur_fg_ptr) if cur_fg_ptr else 0
    if cur_fg == hwnd:
        return None
    if not cur_fg:
        return PasteOutcome(False, lost_msg, True, PasteErrorKind.WINDOW_LOST)
    cur_fg_exe = _get_hwnd_process_exe_name(cur_fg, user32, kernel32)
    if cur_fg_exe in exe_names and (
        not user32.IsWindow(ctypes.c_void_p(hwnd)) or not user32.IsWindowVisible(ctypes.c_void_p(hwnd))
    ):
        return PasteOutcome(False, switched_browser_msg, True, PasteErrorKind.TRANSIENT, target_window=cur_fg)
    return PasteOutcome(False, switched_other_msg, False, PasteErrorKind.USER_ABORTED)


def _send_key_release_if_pressed(user32: Any, inputs: List[_INPUT], vk: int) -> None:
    if user32.GetAsyncKeyState(vk) & 0x8000:
        scan = user32.MapVirtualKeyW(vk, 0)
        ev = _INPUT(type=_INPUT_KEYBOARD)
        ev.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=_KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
        inputs.append(ev)


def _send_ctrl_v_clean(user32: Any) -> bool:
    inputs: List[_INPUT] = []
    # コマンド実行直後にユーザーが押下している修飾キーによる誤結合(Ctrl+Shift+V等)を防止する
    for mod_vk in (
        _VK_SHIFT, _VK_LSHIFT, _VK_RSHIFT,
        _VK_MENU, _VK_LMENU, _VK_RMENU,
        _VK_LWIN, _VK_RWIN,
        _VK_CONTROL, _VK_LCONTROL, _VK_RCONTROL,
    ):
        _send_key_release_if_pressed(user32, inputs, mod_vk)

    scan_ctrl = user32.MapVirtualKeyW(_VK_CONTROL, 0)
    scan_v = user32.MapVirtualKeyW(_VK_V, 0)
    for vk, scan, flags in (
        (_VK_CONTROL, scan_ctrl, 0),
        (_VK_V, scan_v, 0),
        (_VK_V, scan_v, _KEYEVENTF_KEYUP),
        (_VK_CONTROL, scan_ctrl, _KEYEVENTF_KEYUP),
    ):
        ev = _INPUT(type=_INPUT_KEYBOARD)
        ev.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        inputs.append(ev)

    arr = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
    if sent == len(inputs):
        time.sleep(0.05)
        return True
    return False


def _click_safe_region_preserving_cursor(user32: Any, el: ctypes.c_void_p, oleaut32: Any) -> bool:
    prev_dpi = None
    has_dpi_api = hasattr(user32, "SetThreadDpiAwarenessContext")
    if has_dpi_api:
        prev_dpi = user32.SetThreadDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        if not prev_dpi:
            prev_dpi = user32.SetThreadDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE)

    try:
        rect = _uia_get_bounding_rect(el, oleaut32)
        if not rect or rect[2] <= 0 or rect[3] <= 0:
            return False

        # 右側・下側のオーバーレイボタンを避け入力欄左上の安全領域を対象にする
        click_x = int(round(rect[0] + min(24.0, rect[2] * 0.2)))
        click_y = int(round(rect[1] + min(16.0, rect[3] * 0.5)))

        orig_pt = _POINT()
        has_orig = bool(user32.GetCursorPos(ctypes.byref(orig_pt)))
        if not user32.SetCursorPos(click_x, click_y):
            return False
        try:
            time.sleep(0.02)
            down = _INPUT(type=_INPUT_MOUSE)
            down.union.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=_MOUSEEVENTF_LEFTDOWN, time=0, dwExtraInfo=0)
            up = _INPUT(type=_INPUT_MOUSE)
            up.union.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=_MOUSEEVENTF_LEFTUP, time=0, dwExtraInfo=0)
            arr = (_INPUT * 2)(down, up)
            return user32.SendInput(2, arr, ctypes.sizeof(_INPUT)) == 2
        finally:
            if has_orig:
                time.sleep(0.01)
                user32.SetCursorPos(orig_pt.x, orig_pt.y)
    finally:
        if has_dpi_api and prev_dpi:
            user32.SetThreadDpiAwarenessContext(prev_dpi)


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
    fg_hwnd_ptr = user32.GetForegroundWindow()
    fg_hwnd = int(fg_hwnd_ptr) if fg_hwnd_ptr else 0
    if not fg_hwnd:
        # ウィンドウ遷移直後の一時的なフォーカス喪失時のみリトライを許容する
        return PasteOutcome(False, "対象ブラウザが最前面にありません", True, PasteErrorKind.WINDOW_LOST)

    fg_exe = _get_hwnd_process_exe_name(fg_hwnd, user32, kernel32)
    if target_hwnd:
        if fg_hwnd == target_hwnd:
            hwnd = target_hwnd
        elif not user32.IsWindow(ctypes.c_void_p(target_hwnd)) or not user32.IsWindowVisible(ctypes.c_void_p(target_hwnd)):
            # 一時ウィンドウ終了後に新ブラウザウィンドウへ遷移した場合のみ追従し、非ブラウザ復帰時は奪取しない
            if fg_exe in exe_names:
                hwnd = fg_hwnd
            else:
                return PasteOutcome(
                    False,
                    "対象ブラウザウィンドウが閉じられ別アプリへ切り替わったため中断しました",
                    False,
                    PasteErrorKind.USER_ABORTED,
                )
        elif fg_exe in exe_names:
            return PasteOutcome(
                False,
                "別のブラウザウィンドウが最前面になったため中断しました",
                False,
                PasteErrorKind.USER_ABORTED,
            )
        else:
            return PasteOutcome(
                False,
                "別アプリへ切り替わったため自動貼り付けを中断しました",
                False,
                PasteErrorKind.USER_ABORTED,
            )
    else:
        hwnd = fg_hwnd

    if fg_exe not in exe_names:
        return PasteOutcome(
            False,
            f"最前面ウィンドウ ({fg_exe}) がデフォルトブラウザではありません",
            False,
            PasteErrorKind.USER_ABORTED,
        )

    title = _get_window_title(hwnd, user32)
    if is_login_or_auth_title(title):
        return PasteOutcome(
            False,
            f"認証・ログイン画面のため自動貼り付けを中断しました (title: {title})",
            False,
            PasteErrorKind.LOGIN_REQUIRED,
            target_window=hwnd,
        )
    if is_loading_or_unready_title(title):
        return PasteOutcome(
            False,
            f"サイトを読み込み中のため完了を待機しています (title: {title})",
            True,
            PasteErrorKind.TRANSIENT,
            target_window=hwnd,
        )
    if allowed_title_keywords:
        title_lower = title.lower()
        if not any(kw in title_lower for kw in allowed_title_keywords):
            return PasteOutcome(
                False,
                f"許可対象外のページのため自動貼り付けをスキップしました (title: {title})",
                True,
                PasteErrorKind.TRANSIENT,
                target_window=hwnd,
            )

    hr_co = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    should_uninit = hr_co in (0, 1)
    if hr_co < 0 and hr_co != _RPC_E_CHANGED_MODE:
        return PasteOutcome(False, f"COM初期化に失敗しました (HRESULT={hr_co:#x})", False, PasteErrorKind.FAILED, target_window=hwnd)

    p_uia = ctypes.c_void_p()
    root_el = ctypes.c_void_p()
    target_el: Optional[ctypes.c_void_p] = None
    try:
        hr_create = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_CUIAutomation),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_IUIAutomation),
            ctypes.byref(p_uia),
        )
        if hr_create < 0 or not p_uia.value:
            return PasteOutcome(False, f"UI Automationの初期化に失敗しました (HRESULT={hr_create:#x})", False, PasteErrorKind.FAILED, target_window=hwnd)

        hr_el = _com_call(
            p_uia, 6, ctypes.c_long, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.c_void_p(hwnd), ctypes.byref(root_el),
        )
        if hr_el < 0 or not root_el.value:
            return PasteOutcome(False, f"ブラウザウィンドウのUIA要素取得に失敗しました (HRESULT={hr_el:#x})", True, PasteErrorKind.TRANSIENT, target_window=hwnd)

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
