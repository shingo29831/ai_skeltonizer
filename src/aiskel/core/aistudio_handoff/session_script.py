"""Module: @role: パッチ出力検知時の引き継ぎ送信・新規タブ取得・自動Run制御のクライアントJavaScript統合ファサードを提供する。"""

from .session_claim_script import build_handoff_claim_js
from .session_dispatch_script import build_handoff_dispatch_js
from .session_error_script import build_handoff_error_js

__all__ = [
    "build_handoff_claim_js",
    "build_handoff_dispatch_js",
    "build_handoff_error_js",
    "build_handoff_session_js",
]

def build_handoff_session_js() -> str:
    """引き継ぎレコード生成・終端自動適用通知・新規タブ取得と自動Run起動のJSコード群を返す。"""
    return (
        "  const hasHandoffTransport = typeof handoffTransport !== 'undefined' && handoffTransport !== null;\n"
        + build_handoff_error_js()
        + build_handoff_dispatch_js()
        + build_handoff_claim_js()
    )
