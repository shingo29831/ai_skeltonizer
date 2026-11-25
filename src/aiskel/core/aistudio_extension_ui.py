"""Module: @role: Google AI Studioブリッジ連携用のブラウザ拡張機能ポップアップUI(HTML/JS)および通信アダプタの公開ファサードを提供する。"""
from .aistudio_extension_popup import build_popup_html, build_popup_js
from .aistudio_extension_transport import (
    build_extension_binding_transport_js,
    build_extension_handoff_transport_js,
)
from .aistudio_userscript_transport import (
    build_userscript_binding_transport_js,
    build_userscript_handoff_transport_js,
)

__all__ = [
    "build_extension_binding_transport_js",
    "build_extension_handoff_transport_js",
    "build_userscript_binding_transport_js",
    "build_userscript_handoff_transport_js",
    "build_popup_html",
    "build_popup_js",
]

