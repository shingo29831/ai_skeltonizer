"""Module: @role: 起動済みブラウザ内から対象サイトの未会話（空チャット）タブを検出・最前面化するサブパッケージ。"""
from .dispatcher import focus_existing_empty_chat_tab
from .types import is_aistudio_target_url, is_new_chat_target_url

__all__ = [
    "focus_existing_empty_chat_tab",
    "is_aistudio_target_url",
    "is_new_chat_target_url",
]
