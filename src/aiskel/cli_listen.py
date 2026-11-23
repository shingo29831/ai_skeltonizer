"""Module: @role: Google AI Studio推論監視ブリッジ(aiskel listen)の起動、拡張機能出力およびライフサイクル管理を担当する。"""
import argparse
import sys
from pathlib import Path
from typing import Callable, List

from .core.aistudio_bridge import AiskelBridgeServer
from .core.aistudio_extension import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    build_userscript_js,
    export_browser_extension,
    format_host_for_url,
    get_default_extension_dir,
    is_loopback_host,
    load_or_create_bridge_token,
    resolve_client_host,
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
        if port <= 0:
            print("エラー: --export-extension / --print-userscript では 1 以上のポート番号を指定してください。", file=sys.stderr)
            return 1
        extension_dir = ext_dir_arg.resolve() if ext_dir_arg else get_default_extension_dir(port=port)
        active_token = load_or_create_bridge_token(extension_dir, explicit_token=explicit_token)

        try:
            if is_print_userscript:
                print(build_userscript_js(host=host, port=port, token=active_token))
                return 0

            exported_dir, extension_updated = export_browser_extension(
                extension_dir,
                host=host,
                port=port,
                token=active_token,
            )
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
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたプロジェクトディレクトリが存在しません: {project_root}", file=sys.stderr)
        return 1

    server = AiskelBridgeServer(
        project_root=project_root,
        command_runner=on_run_command,
        host=host,
        port=port,
        token=None,
        no_focus=getattr(parsed_args, "no_focus", False),
        no_paste=getattr(parsed_args, "no_paste", False),
    )

    # --port 0(動的割当)でも実ポートを拡張機能へ反映させるため先にbindを実行
    try:
        bound_host, bound_port = server.bind()
    except (OSError, ValueError) as e:
        print(f"エラー: ブリッジサーバーの起動に失敗しました ({host}:{port}): {e}", file=sys.stderr)
        return 1

    try:
        extension_dir = ext_dir_arg.resolve() if ext_dir_arg else get_default_extension_dir(port=bound_port)
        active_token = load_or_create_bridge_token(extension_dir, explicit_token=explicit_token)
        server.token = active_token
        exported_dir, extension_updated = export_browser_extension(
            extension_dir,
            host=bound_host,
            port=bound_port,
            token=active_token,
        )

        client_host = resolve_client_host(bound_host)
        bound_url_host = format_host_for_url(bound_host)
        client_url_host = format_host_for_url(client_host)
        print(f"🎧 Google AI Studio 自動実行ブリッジを起動しました: http://{bound_url_host}:{bound_port}")
        if client_url_host != bound_url_host:
            print(f"   - ブラウザ接続先    : http://{client_url_host}:{bound_port}")
        if not is_loopback_host(bound_host):
            print("   ⚠ ループバック以外のインターフェースで待受中です。認証トークンが有効化されています。")
        print(f"   - 対象プロジェクト  : {project_root}")
        print(f"   - Chrome拡張フォルダ: {exported_dir}")
        if extension_updated:
            print("   ⚠ 拡張機能設定が更新されました。既に読み込み済みの場合は chrome://extensions で「更新(リロード)」してください。")
        print("   ※ 初回のみ chrome://extensions から上記拡張フォルダを読み込んでください (Tampermonkey用: aiskel listen --print-userscript)")
        print("   推論完了時の `aiskel c` コマンドを待機中... (終了するには Ctrl+C)")

        server.serve_forever()
    except ValueError as e:
        print(f"エラー: ホスト設定が不正です: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n⏹ ブリッジサーバーを終了しました。")
    finally:
        server.shutdown()
    return 0
