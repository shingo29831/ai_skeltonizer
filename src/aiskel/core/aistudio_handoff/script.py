"""Module: @role: パッチ出力検知時の応答・要約引き継ぎおよび新規タブ貼り付けのクライアントJavaScript統合ファサードを提供する。"""

from .paste_script import build_handoff_paste_js
from .session_script import build_handoff_session_js

def build_patch_handoff_js() -> str:
    """パッチ検知時の新規タブ引き継ぎ送信および遷移先タブでの自動貼り付け・紐づけスクリプトを返す。"""
    return build_handoff_paste_js() + build_handoff_session_js()
