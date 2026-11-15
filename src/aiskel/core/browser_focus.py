"""Module: @role: 起動済みのデフォルトブラウザを検出し、ウィンドウを最上位表示（フォーカス）するOS別制御のディスパッチャを提供する。"""
import os
import platform
import subprocess
import sys

from .browser_focus_linux import focus_linux_default_browser
from .browser_focus_macos import focus_macos_default_browser
from .browser_focus_windows import focus_windows_default_browser


def focus_default_browser(interactive_only: bool = False) -> bool:
    """起動済みのデフォルトブラウザを検出し、最上位表示（フォーカス状態）にする。"""
    # 非TTY（テスト出力キャプチャやpythonw等のstdout=None環境）やCIでのフォーカス奪取を防ぐ
    if interactive_only and (not (sys.stdout is not None and sys.stdout.isatty()) or bool(os.environ.get("CI"))):
        return False

    system = platform.system()
    try:
        if system == "Windows":
            return focus_windows_default_browser()
        if system == "Darwin":
            return focus_macos_default_browser()
        if system == "Linux":
            return focus_linux_default_browser()
        return False
    except (OSError, subprocess.SubprocessError):
        return False
