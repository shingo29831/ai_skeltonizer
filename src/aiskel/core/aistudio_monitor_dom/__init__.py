"""Module: @role: Google AI StudioのDOM要素解析・コマンド抽出・推論中状態判定のクライアントJavaScriptヘルパー群および公開ファサードを提供する。"""

from .parser import build_monitor_dom_parser_js
from .prompt import build_monitor_dom_prompt_js
from .response import build_monitor_dom_response_js
from .state import build_monitor_dom_state_js

def build_monitor_dom_helpers_js() -> str:
    """DOM可視性判定・ターンコンテキスト取得・コマンド抽出を行うJSヘルパー関数群を返す。"""
    return (
        build_monitor_dom_parser_js()
        + build_monitor_dom_state_js()
        + build_monitor_dom_prompt_js()
        + build_monitor_dom_response_js()
    )
