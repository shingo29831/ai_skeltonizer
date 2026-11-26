"""Module: @role: ローカルブリッジサーバーのソケットバインド・拡張機能出力・認証トークン準備および実行ライフサイクル管理を担当する。"""
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .aistudio_extension import (
    export_browser_extension,
    format_host_for_url,
    get_default_extension_dir,
    get_firefox_extension_dir,
    is_loopback_host,
    load_or_create_bridge_token,
    resolve_client_host,
)
from .aistudio_handoff.task_queue import register_bridge_session, unregister_bridge_session

def run_bridge_lifecycle(
    server: Any,
    *,
    host: str,
    port: int,
    display_project_root: Optional[Path] = None,
    explicit_token: Optional[str] = None,
    extension_dir_arg: Optional[Path] = None,
    on_ready: Optional[Callable[[threading.Event], None]] = None,
) -> int:
    """構築済みブリッジサーバーをバインドし、拡張機能出力・起動案内・待受ループおよび終了処理を実行する。"""
    # --port 0(動的割当)でも実ポートを拡張機能へ反映させるため先にbindを実行
    try:
        bound_host, bound_port = server.bind()
    except (OSError, ValueError, OverflowError) as e:
        print(f"エラー: ブリッジサーバーの起動に失敗しました ({host}:{port}): {e}", file=sys.stderr)
        return 1

    registered_session_url: Optional[str] = None
    try:
        try:
            extension_dir = (
                extension_dir_arg.resolve()
                if extension_dir_arg is not None
                else get_default_extension_dir(port=bound_port)
            )
            active_token = load_or_create_bridge_token(extension_dir, explicit_token=explicit_token)
            server.token = active_token
            exported_dir, extension_updated = export_browser_extension(
                extension_dir,
                host=bound_host,
                port=bound_port,
                token=active_token,
                browser="chrome",
            )
            firefox_dir, firefox_updated = export_browser_extension(
                get_firefox_extension_dir(extension_dir),
                host=bound_host,
                port=bound_port,
                token=active_token,
                browser="firefox",
            )
        except OSError as e:
            print(f"エラー: 拡張機能または認証トークンファイルの出力に失敗しました: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"エラー: 拡張機能またはホスト設定が不正です: {e}", file=sys.stderr)
            return 1

        try:
            client_host = resolve_client_host(bound_host)
            bound_url_host = format_host_for_url(bound_host)
            client_url_host = format_host_for_url(client_host)
            loopback = is_loopback_host(bound_host)
        except ValueError as e:
            print(f"エラー: ホスト設定が不正です: {e}", file=sys.stderr)
            return 1

        shown_project_root = display_project_root if display_project_root is not None else server.project_root
        print(f"🎧 Google AI Studio 自動実行ブリッジを起動しました: http://{bound_url_host}:{bound_port} (拡張機能連携用API)")
        if client_url_host != bound_url_host:
            print(f"   - ブラウザ接続先         : http://{client_url_host}:{bound_port}")
        if not loopback:
            print("   ⚠ ループバック以外のインターフェースで待受中です。認証トークンが有効化されています。")
        print(f"   - 対象プロジェクト       : {shown_project_root}")
        print(f"   - パッチ自動新タブ上限   : {server.get_patch_handoff_max_tabs()} 回")
        print(f"   - Chrome/Edge拡張フォルダ: {exported_dir}")
        print(f"   - Firefox拡張マニフェスト: {firefox_dir / 'manifest.json'}")
        if extension_updated or firefox_updated:
            print("   ⚠ 拡張機能設定が更新されました。既に読み込み済みの場合はブラウザの拡張機能画面で「更新(リロード)」してください。")
        print("   ※ Chrome/Edge: chrome://extensions から上記拡張フォルダを読み込み")
        print("   ※ Firefox    : about:debugging#/runtime/this-firefox の「一時的なアドオンを読み込む」から上記 manifest.json を選択")
        print("   ※ 常駐利用   : Tampermonkey / Violentmonkey 用スクリプトは `aiskel listen --print-userscript` で出力できます")
        print("   ※ タブ紐づけ : AI Studioタブを開くと自動で紐づきます（解除・別タブへの切替はタブ上UIまたは拡張機能アイコンから操作）")
        print("   推論完了時の `aiskel c` コマンドを待機中... (終了するには Ctrl+C)")

        registered_session_url = f"http://{client_url_host}:{bound_port}"
        register_bridge_session(
            server.project_root,
            registered_session_url,
            active_token,
            output_dir=getattr(server, "output_dir", None),
        )
        server.serve_forever(on_ready=on_ready)
    except (OSError, ValueError) as e:
        print(f"エラー: ブリッジサーバーの実行中にエラーが発生しました: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n⏹ ブリッジサーバーを終了しました。")
    finally:
        if registered_session_url is not None:
            unregister_bridge_session(server.project_root, expected_url=registered_session_url)
        server.shutdown()
    return 0
