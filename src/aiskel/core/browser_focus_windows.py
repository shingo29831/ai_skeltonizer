"""Module: @role: Windows環境におけるデフォルトブラウザのレジストリ検出およびWin32 APIを用いた最上位表示制御を担当する。"""
import ctypes
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any, List, Optional, Tuple

_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SW_SHOW = 5
_SW_RESTORE = 9
_ASFW_ANY = 0xFFFFFFFF
_SWP_NO_MOVE_SIZE_SHOW = 0x0001 | 0x0002 | 0x0040


def _extract_exe_name_from_command(command: str) -> Optional[str]:
    """シェルオープンコマンド文字列からブラウザ実行ファイル名(小文字)を抽出する。"""
    if not command:
        return None
    expanded = os.path.expandvars(command.strip())
    if expanded.startswith('"'):
        end_quote = expanded.find('"', 1)
        if end_quote > 1:
            return PureWindowsPath(expanded[1:end_quote]).name.lower()

    match = re.search(r"([^\s\"]+\.exe)(?=\s|$)", expanded, flags=re.IGNORECASE)
    if match:
        return PureWindowsPath(match.group(1)).name.lower()

    tokens = expanded.split()
    return PureWindowsPath(tokens[0]).name.lower() if tokens else None


def _read_windows_command_exe(winreg_mod: Any, root_key: int, cmd_path: str) -> Optional[str]:
    """指定レジストリキーの shell\\open\\command から実行ファイル名を抽出する。"""
    try:
        with winreg_mod.OpenKey(root_key, cmd_path) as key:
            cmd_val, _ = winreg_mod.QueryValueEx(key, "")
            if isinstance(cmd_val, str):
                return _extract_exe_name_from_command(cmd_val)
    except OSError:
        return None
    return None


def _get_windows_default_browser_exe_names() -> List[str]:
    """Windowsレジストリ(UserChoiceLatest優先・既定プロトコルフォールバック)から実行ファイル名候補を取得する。"""
    import winreg

    candidates: List[str] = []
    for scheme in ("https", "http"):
        base_path = rf"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\{scheme}"
        # Windows 11 24H2+ の UserChoiceLatest\ProgId を優先し、旧 UserChoice へフォールバックする
        choice_paths = (rf"{base_path}\UserChoiceLatest\ProgId", rf"{base_path}\UserChoice")
        for key_path in choice_paths:
            prog_id: Optional[str] = None
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    val, _ = winreg.QueryValueEx(key, "ProgId")
                    if isinstance(val, str) and val.strip():
                        prog_id = val.strip()
            except OSError:
                continue

            if not prog_id:
                continue

            for root_key, cmd_path in (
                (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\shell\open\command"),
                (winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command"),
            ):
                exe_name = _read_windows_command_exe(winreg, root_key, cmd_path)
                if exe_name and exe_name not in candidates:
                    candidates.append(exe_name)
                    break
            # 有効なProgId解決後は旧UserChoiceを混ぜないため即終了する
            if candidates:
                return candidates

    # 既定ブラウザを一度も明示変更していない環境向けにプロトコルハンドラ直接登録を参照する
    for scheme in ("https", "http"):
        for root_key, cmd_path in (
            (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{scheme}\shell\open\command"),
            (winreg.HKEY_CLASSES_ROOT, rf"{scheme}\shell\open\command"),
        ):
            exe_name = _read_windows_command_exe(winreg, root_key, cmd_path)
            if exe_name and exe_name not in candidates:
                candidates.append(exe_name)
                return candidates

    return candidates


def _init_win32_apis() -> Tuple[Any, Any, Any, Any, Any]:
    """Win32 APIハンドルと64bit安全な型シグネチャを独立インスタンスで初期化する。"""
    # 共有windllへのグローバル型設定汚染を避けるため独立インスタンスを取得する
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmGetWindowAttribute.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    except OSError:
        dwmapi = None

    wnd_enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)

    # 64bit環境でのハンドル・ポインタ切り捨てを防止する型定義
    user32.EnumWindows.argtypes = [wnd_enum_proc, ctypes.c_ssize_t]
    user32.EnumWindows.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_int
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = ctypes.c_int
    user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetWindow.restype = ctypes.c_void_p
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint32]
    user32.AllowSetForegroundWindow.restype = ctypes.c_int
    user32.AttachThreadInput.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
    user32.AttachThreadInput.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.c_int
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint
    ]
    user32.SetWindowPos.restype = ctypes.c_int

    get_window_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    get_window_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
    get_window_long.restype = ctypes.c_ssize_t

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    return user32, kernel32, dwmapi, get_window_long, wnd_enum_proc


def _find_target_browser_hwnds(
    exe_names: List[str],
    user32: Any,
    kernel32: Any,
    dwmapi: Any,
    get_window_long: Any,
    wnd_enum_proc: Any,
) -> Tuple[List[int], List[int]]:
    """Zオーダー優先で対象ブラウザの (可視HWNDリスト, 最小化HWNDリスト) を返す。"""
    target_set = set(exe_names)
    visible_windows: List[int] = []
    minimized_windows: List[int] = []

    def _is_cloaked(hwnd: int) -> bool:
        if dwmapi is None:
            return False
        cloaked = ctypes.c_uint32(0)
        hr = dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return hr == 0 and cloaked.value != 0

    def _get_process_exe_name(pid: int) -> Optional[str]:
        h_proc = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
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

    def _enum_callback(hwnd: Optional[int], _lparam: int) -> int:
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return 1
        if user32.GetWindow(hwnd, _GW_OWNER):
            return 1
        ex_style = get_window_long(hwnd, _GWL_EXSTYLE)
        if ex_style & (_WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE):
            return 1
        # 他仮想デスクトップや中断UWP等のクロークウィンドウを除外する
        if _is_cloaked(hwnd):
            return 1
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return 1

        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return 1

        proc_exe = _get_process_exe_name(pid.value)
        if proc_exe in target_set:
            if user32.IsIconic(hwnd):
                minimized_windows.append(hwnd)
            else:
                visible_windows.append(hwnd)
        return 1

    enum_proc = wnd_enum_proc(_enum_callback)
    user32.EnumWindows(enum_proc, 0)
    return visible_windows, minimized_windows


def _find_target_browser_hwnd(
    exe_names: List[str],
    user32: Any,
    kernel32: Any,
    dwmapi: Any,
    get_window_long: Any,
    wnd_enum_proc: Any,
) -> Optional[int]:
    """Zオーダー優先で可視・最小化ウィンドウを探索し、対象ブラウザのHWNDを返す。"""
    visible_windows, minimized_windows = _find_target_browser_hwnds(
        exe_names, user32, kernel32, dwmapi, get_window_long, wnd_enum_proc
    )
    return visible_windows[0] if visible_windows else (minimized_windows[0] if minimized_windows else None)


def _focus_windows_hwnd_with_apis(
    target_hwnd: int,
    user32: Any,
    kernel32: Any,
    get_window_long: Any,
) -> Optional[int]:
    """取得済みWin32 APIハンドルを用いて指定HWNDを最上位表示・フォーカスする。"""
    if not target_hwnd:
        return None

    fg_hwnd = user32.GetForegroundWindow()
    if fg_hwnd == target_hwnd:
        return target_hwnd

    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_tid = user32.GetWindowThreadProcessId(target_hwnd, None)

    user32.AllowSetForegroundWindow(_ASFW_ANY)

    attached_fg = False
    attached_target = False
    if fg_tid and fg_tid != cur_tid:
        attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, 1))
    if target_tid and target_tid != cur_tid and target_tid != fg_tid:
        attached_target = bool(user32.AttachThreadInput(cur_tid, target_tid, 1))

    try:
        # 最大化状態を維持するため最小化時のみ SW_RESTORE を呼ぶ
        if user32.IsIconic(target_hwnd):
            user32.ShowWindow(target_hwnd, _SW_RESTORE)
        else:
            user32.ShowWindow(target_hwnd, _SW_SHOW)

        user32.BringWindowToTop(target_hwnd)
        ok = bool(user32.SetForegroundWindow(target_hwnd))

        if not ok or user32.GetForegroundWindow() != target_hwnd:
            was_topmost = bool(get_window_long(target_hwnd, _GWL_EXSTYLE) & _WS_EX_TOPMOST)
            hwnd_topmost = ctypes.c_void_p(-1)
            hwnd_notopmost = ctypes.c_void_p(-2)
            user32.SetWindowPos(target_hwnd, hwnd_topmost, 0, 0, 0, 0, _SWP_NO_MOVE_SIZE_SHOW)
            # 元々最前面固定のウィンドウは NOTOPMOST で固定状態を解除しない
            if not was_topmost:
                user32.SetWindowPos(target_hwnd, hwnd_notopmost, 0, 0, 0, 0, _SWP_NO_MOVE_SIZE_SHOW)
            ok = bool(user32.SetForegroundWindow(target_hwnd))
        return target_hwnd if ok else None
    finally:
        if attached_target:
            user32.AttachThreadInput(cur_tid, target_tid, 0)
        if attached_fg:
            user32.AttachThreadInput(cur_tid, fg_tid, 0)


def focus_windows_hwnd(target_hwnd: int) -> Optional[int]:
    """指定HWNDのブラウザウィンドウを最上位表示し、成功時はそのHWNDを返す。"""
    if not target_hwnd:
        return None
    try:
        user32, kernel32, _, get_window_long, _ = _init_win32_apis()
        return _focus_windows_hwnd_with_apis(target_hwnd, user32, kernel32, get_window_long)
    except OSError:
        return None


def focus_windows_default_browser_hwnd() -> Optional[int]:
    """起動済みのWindowsデフォルトブラウザを最上位表示し、成功時は対象HWNDを返す。"""
    try:
        exe_names = _get_windows_default_browser_exe_names()
        if not exe_names:
            return None

        user32, kernel32, dwmapi, get_window_long, wnd_enum_proc = _init_win32_apis()
        target_hwnd = _find_target_browser_hwnd(
            exe_names, user32, kernel32, dwmapi, get_window_long, wnd_enum_proc
        )
        if not target_hwnd:
            return None
        return _focus_windows_hwnd_with_apis(target_hwnd, user32, kernel32, get_window_long)
    except OSError:
        return None


def focus_windows_default_browser() -> bool:
    """起動済みのWindowsデフォルトブラウザをZオーダー優先で探索し、最上位表示・フォーカスする。"""
    return focus_windows_default_browser_hwnd() is not None
