"""Module: @role: OSネイティブのクリップボードとのテキスト送受信インターフェースを提供する。"""
import ctypes
import platform
import subprocess
import sys
import time


def _set_windows_clipboard_powershell(text: str) -> None:
    # なぜ必要か: Win32 API失敗時のフォールバック。UTF-8入力明示で文字化けを防止
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; Set-Clipboard"
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    proc.communicate(text.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(f"PowerShell Set-Clipboard failed with code {proc.returncode}")


def _get_windows_clipboard() -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13

    # 64-bit環境でのポインタ切り捨てを防止する型シグネチャ定義
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_int

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int

    if not user32.OpenClipboard(None):
        return ""
    try:
        h_mem = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_mem:
            return ""
        p_mem = kernel32.GlobalLock(h_mem)
        if not p_mem:
            return ""
        try:
            return ctypes.wstring_at(p_mem)
        finally:
            kernel32.GlobalUnlock(h_mem)
    finally:
        user32.CloseClipboard()


def _set_windows_clipboard(text: str) -> None:
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        GMEM_MOVEABLE = 0x0002
        CF_UNICODETEXT = 13

        # 64-bit環境でのポインタ切り捨てを防止する型シグネチャ定義
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_int
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p

        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_int
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = ctypes.c_int
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_int

        # 他プロセスのクリップボード排他ロックを考慮し最大5回リトライ
        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.02)

        if not opened:
            raise RuntimeError("OpenClipboard failed")

        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16le") + b"\x00\x00"
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h_mem:
                raise RuntimeError("GlobalAlloc failed")

            p_mem = kernel32.GlobalLock(h_mem)
            if not p_mem:
                kernel32.GlobalFree(h_mem)
                raise RuntimeError("GlobalLock failed")

            ctypes.memmove(p_mem, data, len(data))
            kernel32.GlobalUnlock(h_mem)

            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                kernel32.GlobalFree(h_mem)
                raise RuntimeError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()

    except Exception:
        _set_windows_clipboard_powershell(text)


def get_clipboard_text() -> str:
    """OSのクリップボードからテキストを取得する"""
    system = platform.system()
    try:
        if system == "Windows":
            return _get_windows_clipboard().replace("\r\n", "\n").replace("\r", "\n")
        elif system == "Darwin":
            text = subprocess.check_output(["pbpaste"], encoding="utf-8")
            return text.replace("\r\n", "\n").replace("\r", "\n")
        elif system == "Linux":
            try:
                text = subprocess.check_output(["xclip", "-selection", "clipboard", "-o"], encoding="utf-8")
            except FileNotFoundError:
                text = subprocess.check_output(["xsel", "--clipboard", "--output"], encoding="utf-8")
            return text.replace("\r\n", "\n").replace("\r", "\n")
        else:
            print(f"⚠ OS {system} のクリップボード自動取得は未対応です。", file=sys.stderr)
            return ""
    except Exception as e:
        print(f"⚠ クリップボードの読み込みに失敗しました: {e}", file=sys.stderr)
        return ""


def set_clipboard_text(text: str) -> None:
    """OSのクリップボードにテキストを書き込む"""
    system = platform.system()
    try:
        if system == "Windows":
            _set_windows_clipboard(text)
        elif system == "Darwin":
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(text.encode("utf-8"))
        elif system == "Linux":
            try:
                proc = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
                proc.communicate(text.encode("utf-8"))
            except FileNotFoundError:
                proc = subprocess.Popen(["xsel", "--clipboard", "--input"], stdin=subprocess.PIPE)
                proc.communicate(text.encode("utf-8"))
        else:
            raise NotImplementedError(f"OS {system} へのクリップボード書き込みは未対応です。")
    except Exception as e:
        raise RuntimeError(f"クリップボードへの書き込みに失敗しました: {e}")
