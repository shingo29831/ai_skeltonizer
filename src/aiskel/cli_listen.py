"""Module: @role: Google AI Studio推論監視ブリッジ(aiskel listen)の起動、拡張機能出力およびライフサイクル管理を担当する。"""
import argparse
import sys
from pathlib import Path
from typing import Callable, List

from .core.aistudio_bridge import run_bridge_server
from .core.aistudio_extension import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    build_userscript_js,
    export_browser_extension,
    get_default_extension_dir,
    load_or_create_bridge_token,
)


def handle_listen_command(
    parsed_args: argparse.Namespace,
    on_run_command: Callable[[List[str]], int],
) -> int:
    """ローカルブリッジサーバーを起動し、Google AI Studio推論完了時のaiskel c自動実行を待機する。"""
    host: str = getattr(parsed_args, "host", DEFAULT_BRIDGE_HOST)
    port: int = getattr(parsed_args, "port", DEFAULT_BRIDGE_PORT)
    explicit_token = getattr(parsed_args, "token", None)
    ext_dir_arg = getattr(parsed_args, "extension_dir", None)
    is_print_userscript = getattr(parsed_args, "print_userscript", False)
    is_export_extension = getattr(parsed_args, "export_extension", False)

    if is_print_userscript or is_export_extension:
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            print("エラー: --export-extension / --print-userscript では 1〜65535 のポート番号を指定してください。", file=sys.stderr)
            return 1
        extension_dir = ext_dir_arg.resolve() if ext_dir_arg else get_default_extension_dir(port=port)

        try:
            active_token = load_or_create_bridge_token(extension_dir, explicit_token=explicit_token)
            if is_print_userscript:
                print(build_userscript_js(host=host, port=port, token=active_token))
                return 0

            exported_dir, extension_updated = export_browser_extension(
                extension_dir,
                host=host,
                port=port,
                token=active_token,
            )
        except OSError as e:
            print(f"エラー: 拡張機能または認証トークンファイルの出力に失敗しました: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"エラー: ホスト設定が不正です: {e}", file=sys.stderr)
            return 1

        print(f"✔ Chrome拡張機能ファイルを生成しました: {exported_dir}")
        print("   1. Chrome/Edge で chrome://extensions を開く")
        print("   2. 右上の「デベロッパーモード」をONにする")
        print("   3.「パッケージ化されていない拡張機能を読み込む」から上記フォルダを選択する")
        if extension_updated:
            print("   ※ 既に読み込み済みの場合は chrome://extensions で拡張機能の更新(リロード)をクリックしてください")
        return 0

    project_root: Path = parsed_args.project_dir.resolve()
    return run_bridge_server(
        project_root=project_root,
        on_run_command=on_run_command,
        host=host,
        port=port,
        explicit_token=explicit_token,
        extension_dir_arg=ext_dir_arg,
        no_focus=getattr(parsed_args, "no_focus", False),
        no_paste=getattr(parsed_args, "no_paste", False),
    )
