"""Module: @role: 起動済みのデフォルトブラウザを検出し、ウィンドウを最上位表示（フォーカス）するOS別制御のディスパッチャを提供する。"""
import os
import platform
import subprocess
import sys
from typing import Optional, Union

from .linux import focus_linux_default_browser_target, focus_linux_window_id
from .macos import focus_macos_bundle_id, focus_macos_default_browser_target
from .windows import focus_windows_default_browser_hwnd, focus_windows_hwnd


def focus_default_browser_target(interactive_only: bool = False) -> Optional[Union[int, str]]:
    """起動済みのデフォルトブラウザを最上位表示し、成功時は対象ウィンドウ識別子を返す。"""
    # 非TTY（テスト出力キャプチャやpythonw等のstdout=None環境）やCIでのフォーカス奪取を防ぐ
    if interactive_only and (not (sys.stdout is not None and sys.stdout.isatty()) or bool(os.environ.get("CI"))):
        return None

    system = platform.system()
    try:
        if system == "Windows":
            return focus_windows_default_browser_hwnd()
        if system == "Darwin":
            return focus_macos_default_browser_target()
        if system == "Linux":
            return focus_linux_default_browser_target()
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def focus_browser_target(
    target_window: Optional[Union[int, str]],
    interactive_only: bool = False,
) -> Optional[Union[int, str]]:
    """指定されたウィンドウ識別子に対応する起動済みブラウザを再フォーカスする。"""
    if target_window is None or isinstance(target_window, bool):
        return None
    if interactive_only and (not (sys.stdout is not None and sys.stdout.isatty()) or bool(os.environ.get("CI"))):
        return None

    system = platform.system()
    try:
        if system == "Windows" and isinstance(target_window, int):
            return focus_windows_hwnd(target_window)
        if system == "Darwin" and isinstance(target_window, str):
            return focus_macos_bundle_id(target_window)
        if system == "Linux" and isinstance(target_window, str):
            return focus_linux_window_id(target_window)
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def focus_default_browser(interactive_only: bool = False) -> bool:
    """起動済みのデフォルトブラウザを検出し、最上位表示（フォーカス状態）にする。"""
    return focus_default_browser_target(interactive_only=interactive_only) is not None
