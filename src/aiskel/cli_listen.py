"""Module: @role: Google AI Studio推論監視ブリッジ(aiskel listen)の起動、拡張機能出力およびライフサイクル管理を担当する。"""
import argparse
import inspect
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .core.aistudio_bridge import MAX_PATCH_HANDOFF_MAX_TABS, run_bridge_server
from .core.aistudio_extension import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    build_userscript_js,
    export_browser_extension,
    get_default_extension_dir,
    get_firefox_extension_dir,
    load_or_create_bridge_token,
)


def handle_listen_command(
    parsed_args: argparse.Namespace,
    on_run_command: Callable[[List[str]], int],
    on_run_structured: Optional[Callable[..., Any]] = None,
) -> int:
    """ローカルブリッジサーバーを起動し、Google AI Studio推論完了時のaiskel c自動実行を待機する。"""
    launch_cwd: Path = Path.cwd().resolve()
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
                browser="chrome",
            )
            firefox_dir, firefox_updated = export_browser_extension(
                get_firefox_extension_dir(extension_dir),
                host=host,
                port=port,
                token=active_token,
                browser="firefox",
            )
        except OSError as e:
            print(f"エラー: 拡張機能または認証トークンファイルの出力に失敗しました: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"エラー: ホスト設定が不正です: {e}", file=sys.stderr)
            return 1

        print("✔ ブラウザ拡張機能ファイルを生成しました:")
        print(f"   - Chrome/Edge拡張フォルダ: {exported_dir}")
        print(f"   - Firefox拡張マニフェスト: {firefox_dir / 'manifest.json'}")
        print("   [Chrome/Edge]")
        print("   1. chrome://extensions を開く")
        print("   2. 右上の「デベロッパーモード」をONにする")
        print("   3.「パッケージ化されていない拡張機能を読み込む」から上記 Chrome/Edge拡張フォルダを選択する")
        print("   [Firefox]")
        print("   1. about:debugging#/runtime/this-firefox を開く")
        print("   2.「一時的なアドオンを読み込む」から上記 Firefox拡張マニフェスト (manifest.json) を選択する")
        print("      (再起動後も常駐させたい場合は Violentmonkey/Tampermonkey 等で `aiskel listen --print-userscript` を利用してください)")
        if extension_updated or firefox_updated:
            print("   ※ 既に読み込み済みの場合は各ブラウザの拡張機能画面で更新(リロード)をクリックしてください")
        return 0

    no_focus = getattr(parsed_args, "no_focus", False)
    no_paste = getattr(parsed_args, "no_paste", False)
    auto_run = getattr(parsed_args, "auto_run", None)
    if auto_run is True and (no_focus or no_paste):
        print("⚠ --no-focus または --no-paste 指定時は --auto-run を無効化します。", file=sys.stderr)
        auto_run = False

    patch_handoff_max_tabs = getattr(parsed_args, "patch_handoff_max_tabs", None)
    if patch_handoff_max_tabs is not None and (
        isinstance(patch_handoff_max_tabs, bool)
        or not isinstance(patch_handoff_max_tabs, int)
        or not (0 <= patch_handoff_max_tabs <= MAX_PATCH_HANDOFF_MAX_TABS)
    ):
        print(
            f"エラー: --max-auto-tabs は 0〜{MAX_PATCH_HANDOFF_MAX_TABS} の整数で指定してください: {patch_handoff_max_tabs}",
            file=sys.stderr,
        )
        return 1

    project_root: Path = parsed_args.project_dir.resolve()
    bridge_kwargs: Dict[str, Any] = {
        "project_root": project_root,
        "on_run_command": on_run_command,
        "host": host,
        "port": port,
        "explicit_token": explicit_token,
        "extension_dir_arg": ext_dir_arg,
        "no_focus": no_focus,
        "no_paste": no_paste,
        "auto_run": auto_run,
        "structured_command_runner": on_run_structured,
        "patch_handoff_max_tabs": patch_handoff_max_tabs,
    }
    try:
        sig = inspect.signature(run_bridge_server)
        if "command_cwd" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            bridge_kwargs["command_cwd"] = launch_cwd
    except (TypeError, ValueError):
        pass

    return run_bridge_server(**bridge_kwargs)
