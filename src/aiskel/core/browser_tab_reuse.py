"""Module: @role: 起動済みブラウザ内から対象サイトの未会話（空チャット）タブを検出・最前面化するOS別ディスパッチャを提供する。"""
import os
import platform
import subprocess
import sys
from typing import Optional, Sequence, Union

from .browser_paste import DEFAULT_PROMPT_LABELS
from .browser_tab_reuse_linux import focus_linux_empty_chat_tab
from .browser_tab_reuse_macos import focus_macos_empty_chat_tab
from .browser_tab_reuse_types import is_aistudio_target_url, is_new_chat_target_url
from .browser_tab_reuse_windows import focus_windows_empty_chat_tab


def focus_existing_empty_chat_tab(
    target_url: str,
    allowed_title_keywords: Optional[Sequence[str]] = None,
    prompt_labels: Optional[Sequence[str]] = None,
    interactive_only: bool = True,
) -> Optional[Union[int, str]]:
    """起動済みブラウザから対象サイトの空チャットタブを検出し、最前面化・選択して識別子を返す。"""
    if interactive_only and (not (sys.stdout is not None and sys.stdout.isatty()) or bool(os.environ.get("CI"))):
        return None
    if not is_new_chat_target_url(target_url):
        return None

    labels = tuple(
        lbl.strip() for lbl in (DEFAULT_PROMPT_LABELS if prompt_labels is None else prompt_labels) if lbl and lbl.strip()
    )
    keywords = tuple(kw.strip().lower() for kw in (allowed_title_keywords or ()) if kw and kw.strip())
    is_aistudio = is_aistudio_target_url(target_url)

    system = platform.system()
    try:
        if system == "Windows":
            return focus_windows_empty_chat_tab(target_url, labels, keywords, is_aistudio)
        if system == "Darwin":
            return focus_macos_empty_chat_tab(target_url, labels, keywords, is_aistudio)
        if system == "Linux":
            return focus_linux_empty_chat_tab(target_url, labels, keywords, is_aistudio)
        return None
    except (OSError, subprocess.SubprocessError):
        return None
