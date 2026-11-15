"""Module: @role: 起動済みのデフォルトブラウザを検出し、ウィンドウを最上位表示（フォーカス）するOS別制御を提供する。"""
import ctypes
import os
import platform
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import List, Optional, Set

_VALID_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+$")
_LSAPPINFO_BUNDLE_RE = re.compile(r'bundleID=(?:"([^"]+)"|([^\s]+))')
_IGNORED_LINUX_HINTS: Set[str] = {
    "sh", "bash", "dash", "zsh", "fish", "env", "exec", "flatpak", "snap",
    "gio", "gtk-launch", "xdg-open", "python", "python3", "perl", "ruby",
    "dbus-launch", "nohup", "setsid",
}
_CHANNEL_SUFFIXES = ("-stable", "-beta", "-dev", "-canary", "-nightly", "-bin")


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


def _read_windows_command_exe(winreg_mod: object, root_key: int, cmd_path: str) -> Optional[str]:
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


def _focus_windows_default_browser() -> bool:
    """起動済みのWindowsデフォルトブラウザをZオーダー優先で探索し、最上位表示・フォーカスする。"""
    exe_names = _get_windows_default_browser_exe_names()
    if not exe_names:
        return False

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

    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    DWMWA_CLOAKED = 14
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SW_SHOW = 5
    SW_RESTORE = 9
    ASFW_ANY = 0xFFFFFFFF

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)

    # 64bit環境でのハンドル・ポインタ切り捨てを防止する型定義
    user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_ssize_t]
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

    target_set = set(exe_names)
    visible_windows: List[int] = []
    minimized_windows: List[int] = []

    def _is_cloaked(hwnd: int) -> bool:
        if dwmapi is None:
            return False
        cloaked = ctypes.c_uint32(0)
        hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return hr == 0 and cloaked.value != 0

    def _get_process_exe_name(pid: int) -> Optional[str]:
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
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
        if user32.GetWindow(hwnd, GW_OWNER):
            return 1
        ex_style = get_window_long(hwnd, GWL_EXSTYLE)
        if ex_style & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
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

    enum_proc = WNDENUMPROC(_enum_callback)
    user32.EnumWindows(enum_proc, 0)

    target_hwnd = visible_windows[0] if visible_windows else (minimized_windows[0] if minimized_windows else None)
    if not target_hwnd:
        return False

    fg_hwnd = user32.GetForegroundWindow()
    if fg_hwnd == target_hwnd:
        return True

    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_tid = user32.GetWindowThreadProcessId(target_hwnd, None)

    user32.AllowSetForegroundWindow(ASFW_ANY)

    attached_fg = False
    attached_target = False
    if fg_tid and fg_tid != cur_tid:
        attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, 1))
    if target_tid and target_tid != cur_tid and target_tid != fg_tid:
        attached_target = bool(user32.AttachThreadInput(cur_tid, target_tid, 1))

    try:
        # 最大化状態を維持するため最小化時のみ SW_RESTORE を呼ぶ
        if user32.IsIconic(target_hwnd):
            user32.ShowWindow(target_hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(target_hwnd, SW_SHOW)

        user32.BringWindowToTop(target_hwnd)
        ok = bool(user32.SetForegroundWindow(target_hwnd))

        if not ok or user32.GetForegroundWindow() != target_hwnd:
            was_topmost = bool(get_window_long(target_hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
            HWND_TOPMOST = ctypes.c_void_p(-1)
            HWND_NOTOPMOST = ctypes.c_void_p(-2)
            SWP_FLAGS = 0x0001 | 0x0002 | 0x0040
            user32.SetWindowPos(target_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
            # 元々最前面固定のウィンドウは NOTOPMOST で固定状態を解除しない
            if not was_topmost:
                user32.SetWindowPos(target_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_FLAGS)
            ok = bool(user32.SetForegroundWindow(target_hwnd))
        return ok
    finally:
        if attached_target:
            user32.AttachThreadInput(cur_tid, target_tid, 0)
        if attached_fg:
            user32.AttachThreadInput(cur_tid, fg_tid, 0)


def _get_macos_default_browser_bundle_id() -> str:
    """macOSのLaunchServices設定からデフォルトブラウザのBundle IDを取得する。"""
    plist_path = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    if plist_path.exists():
        try:
            with plist_path.open("rb") as f:
                data = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException, ValueError, EOFError):
            data = None

        if isinstance(data, dict):
            handlers = data.get("LSHandlers", [])
            if isinstance(handlers, list):
                for scheme in ("https", "http"):
                    for item in handlers:
                        if not isinstance(item, dict) or item.get("LSHandlerURLScheme") != scheme:
                            continue
                        for role_key in ("LSHandlerRoleAll", "LSHandlerRoleViewer"):
                            val = item.get(role_key)
                            if isinstance(val, str):
                                candidate = val.strip()
                                if _VALID_BUNDLE_ID_RE.fullmatch(candidate):
                                    return candidate
    return "com.apple.Safari"


def _resolve_running_macos_bundle_id(target_bundle_id: str) -> Optional[str]:
    """lsappinfo listから大文字小文字を無視して起動中アプリの正規Bundle IDを解決する。"""
    res = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True, timeout=2)
    if res.returncode != 0:
        return None

    target_lower = target_bundle_id.lower()
    for match in _LSAPPINFO_BUNDLE_RE.finditer(res.stdout):
        running_id = (match.group(1) or match.group(2) or "").strip()
        if running_id.lower() == target_lower and _VALID_BUNDLE_ID_RE.fullmatch(running_id):
            return running_id
    return None


def _focus_macos_default_browser() -> bool:
    """起動済みのmacOSデフォルトブラウザのみをアクティブ化する。"""
    bundle_id = _get_macos_default_browser_bundle_id()
    if not _VALID_BUNDLE_ID_RE.fullmatch(bundle_id):
        return False

    # plist内の小文字表記を吸収しつつ未インストール時の検索ダイアログを抑止する
    resolved_id = _resolve_running_macos_bundle_id(bundle_id)
    if not resolved_id:
        return False

    # Apple Events(TCC Automation)権限拒否を回避するため open -b で前面化する
    res = subprocess.run(["open", "-b", resolved_id], capture_output=True, text=True, timeout=3)
    return res.returncode == 0


def _append_linux_hint(hints: List[str], raw_hint: str) -> None:
    """有効かつ汎用シェル名でないLinuxヒント文字列のみを重複なく追加する。"""
    hint = raw_hint.strip().lower()
    if len(hint) < 2 or hint in _IGNORED_LINUX_HINTS:
        return
    if hint not in hints:
        hints.append(hint)
    for suffix in _CHANNEL_SUFFIXES:
        if hint.endswith(suffix):
            base = hint[: -len(suffix)]
            if len(base) >= 2 and base not in _IGNORED_LINUX_HINTS and base not in hints:
                hints.append(base)


def _extract_linux_exec_hints(exec_line: str, _depth: int = 0) -> List[str]:
    """.desktopのExec=値から引用符やラッパー(env/sh/flatpak/snap等)を除去して実体ヒントを抽出する。"""
    if _depth > 3:
        return []
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        tokens = exec_line.split()

    tokens = [t for t in tokens if not (len(t) == 2 and t.startswith("%"))]
    if not tokens:
        return []

    hints: List[str] = []
    idx = 0
    while idx < len(tokens):
        cmd_name = Path(tokens[idx]).name.lower()
        if cmd_name == "exec":
            idx += 1
            continue

        if cmd_name == "env":
            idx += 1
            while idx < len(tokens):
                tok = tokens[idx]
                if tok in ("-u", "-C", "-S"):
                    idx += 2
                elif tok.startswith("-") or ("=" in tok and not tok.startswith("/")):
                    idx += 1
                else:
                    break
            continue

        if cmd_name in ("sh", "bash", "dash", "zsh"):
            idx += 1
            while idx < len(tokens) and tokens[idx].startswith("-"):
                flag = tokens[idx]
                idx += 1
                if "c" in flag and idx < len(tokens):
                    return _extract_linux_exec_hints(tokens[idx], _depth + 1)
            continue

        if cmd_name in ("flatpak", "snap") and idx + 1 < len(tokens) and tokens[idx + 1] == "run":
            idx += 2
            while idx < len(tokens) and tokens[idx].startswith("-"):
                idx += 1
            if idx < len(tokens):
                app_ref = tokens[idx].lower()
                _append_linux_hint(hints, app_ref)
                if "." in app_ref:
                    _append_linux_hint(hints, app_ref.rsplit(".", 1)[-1])
            return hints

        if cmd_name == "gtk-launch" and idx + 1 < len(tokens):
            _append_linux_hint(hints, tokens[idx + 1].removesuffix(".desktop"))
            return hints

        if cmd_name == "gio" and idx + 2 < len(tokens) and tokens[idx + 1] == "launch":
            _append_linux_hint(hints, Path(tokens[idx + 2]).stem)
            return hints

        _append_linux_hint(hints, cmd_name)
        return hints

    return hints


def _get_linux_default_desktop_name() -> Optional[str]:
    """xdg-settingsまたはxdg-mimeからデフォルトブラウザの.desktopファイル名を取得する。"""
    commands = (
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "x-scheme-handler/https"],
        ["xdg-mime", "query", "default", "x-scheme-handler/http"],
    )
    for cmd in commands:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            continue
        name = res.stdout.strip()
        if res.returncode == 0 and name:
            return name
    return None


def _get_linux_browser_wm_hints() -> List[str]:
    """Linuxのデフォルトブラウザ設定(.desktop)の[Desktop Entry]からWM_CLASS・コマンド名ヒントを抽出する。"""
    desktop_name = _get_linux_default_desktop_name()
    if not desktop_name:
        return []

    hints: List[str] = []
    _append_linux_hint(hints, desktop_name.removesuffix(".desktop"))
    search_dirs = [
        Path.home() / ".local/share/applications",
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
    ]
    for app_dir in search_dirs:
        desktop_file = app_dir / desktop_name
        if not desktop_file.is_file():
            continue
        try:
            content = desktop_file.read_text(encoding="utf-8", errors="ignore")
            in_desktop_entry = False
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    if in_desktop_entry:
                        break
                    in_desktop_entry = line == "[Desktop Entry]"
                    continue
                if not in_desktop_entry:
                    continue

                if line.startswith("StartupWMClass="):
                    wm_class = line.split("=", 1)[1].strip().lower()
                    if wm_class and wm_class not in _IGNORED_LINUX_HINTS and wm_class not in hints:
                        hints.insert(0, wm_class)
                elif line.startswith("Exec="):
                    for exec_hint in _extract_linux_exec_hints(line.split("=", 1)[1].strip()):
                        _append_linux_hint(hints, exec_hint)
            break
        except OSError:
            continue
    return hints


def _to_case_insensitive_ere(text: str) -> str:
    """POSIX拡張正規表現向けに大文字小文字非依存のパターン文字列を生成する。"""
    parts: List[str] = []
    for ch in text:
        if ch.isalpha() and ch.lower() != ch.upper():
            parts.append(f"[{ch.lower()}{ch.upper()}]")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def _focus_linux_default_browser() -> bool:
    """起動済みのLinuxデフォルトブラウザウィンドウをwmctrlまたはxdotoolでフォーカスする。"""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False

    hints = _get_linux_browser_wm_hints()
    if not hints:
        return False

    try:
        res = subprocess.run(["wmctrl", "-lx"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 3:
                    continue
                win_id, wm_class = parts[0], parts[2].lower()
                # instance.Class をトークン分割し完全一致のみで判定して誤爆を防ぐ
                wm_tokens = {wm_class} | {tok for tok in wm_class.split(".") if tok}
                if any(h in wm_tokens for h in hints):
                    act = subprocess.run(["wmctrl", "-i", "-a", win_id], capture_output=True, timeout=2)
                    if act.returncode == 0:
                        return True
    except (OSError, subprocess.SubprocessError):
        pass

    for hint in hints:
        pattern = f"^{_to_case_insensitive_ere(hint)}$"
        for search_flag in ("--class", "--classname"):
            try:
                search = subprocess.run(
                    ["xdotool", "search", "--onlyvisible", search_flag, pattern],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                win_ids = [w.strip() for w in search.stdout.splitlines() if w.strip()]
                if win_ids:
                    act = subprocess.run(["xdotool", "windowactivate", win_ids[-1]], capture_output=True, timeout=2)
                    if act.returncode == 0:
                        return True
            except (OSError, subprocess.SubprocessError):
                break

    return False


def focus_default_browser(interactive_only: bool = False) -> bool:
    """起動済みのデフォルトブラウザを検出し、最上位表示（フォーカス状態）にする。"""
    # 非TTY（テスト出力キャプチャやpythonw等のstdout=None環境）やCIでのフォーカス奪取を防ぐ
    if interactive_only and (not (sys.stdout is not None and sys.stdout.isatty()) or bool(os.environ.get("CI"))):
        return False

    system = platform.system()
    try:
        if system == "Windows":
            return _focus_windows_default_browser()
        if system == "Darwin":
            return _focus_macos_default_browser()
        if system == "Linux":
            return _focus_linux_default_browser()
        return False
    except (OSError, subprocess.SubprocessError):
        return False
