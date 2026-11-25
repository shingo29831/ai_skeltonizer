"""Module: @role: 開いたAI Studioタブ上に表示するVSCode紐づけ制御UIのクライアントJavaScriptコードを生成する。"""
from .aistudio_error_notice_script import build_error_notice_ui_js
from .aistudio_tab_binding_dom_script import build_tab_binding_ui_dom_js
from .aistudio_tab_binding_state_script import build_tab_binding_ui_render_js



def build_tab_binding_ui_js() -> str:
    """Trusted Types安全かつShadow DOM隔離されたページ内VSCode紐づけUIのスクリプトを返す。"""
    return build_tab_binding_ui_dom_js() + build_tab_binding_ui_render_js()

__all__ = [
    "build_tab_binding_ui_js",
    "build_error_notice_ui_js",
]
