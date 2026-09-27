"""Module: @role: OSネイティブのクリップボードとのテキスト送受信インターフェースを提供する。"""
import platform
import subprocess
import sys


def _get_windows_clipboard() -> str:
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13

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
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    GMEM_MOVEABLE = 2
    CF_UNICODETEXT = 13

    if not user32.OpenClipboard(None):
        raise RuntimeError("クリップボードを開けませんでした。")
    try:
        user32.EmptyClipboard()
        # なぜ必要か: UTF-16LEヌル終端バイナリを直接配置しPowerShell経由のCP932文字化けを完全防止
        data = text.encode("utf-16le") + b"\x00\x00"
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            raise RuntimeError("クリップボード用メモリの確保に失敗しました。")
        p_mem = kernel32.GlobalLock(h_mem)
        if not p_mem:
            kernel32.GlobalFree(h_mem)
            raise RuntimeError("メモリのロックに失敗しました。")
        ctypes.memmove(p_mem, data, len(data))
        kernel32.GlobalUnlock(h_mem)
        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            kernel32.GlobalFree(h_mem)
            raise RuntimeError("クリップボードへのデータ設定に失敗しました。")
    finally:
        user32.CloseClipboard()


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
