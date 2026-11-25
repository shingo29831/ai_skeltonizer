"""Module: @role: Google AI Studio監視用Manifest V3生成・拡張機能出力および関連モジュールの公開ファサードを提供する。"""
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .background import build_background_js
from .network import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    EXTENSION_DIR_NAME,
    EXTENSION_VERSION,
    FIREFOX_DIR_SUFFIX,
    TOKEN_FILENAME,
    format_host_for_url,
    get_default_extension_dir,
    get_firefox_extension_dir,
    is_loopback_host,
    load_or_create_bridge_token,
    normalize_bind_host,
    resolve_client_host,
)
from .scripts import build_content_js, build_userscript_js
from .ui import (
    build_extension_binding_transport_js,
    build_extension_handoff_transport_js,
    build_popup_html,
    build_popup_js,
    build_userscript_binding_transport_js,
    build_userscript_handoff_transport_js,
)
from ..aistudio_monitor_script import build_monitor_core_js

FIREFOX_GECKO_ID_PREFIX = "aiskel-aistudio-bridge"
FIREFOX_MIN_VERSION = "109.0"

__all__ = [
    "DEFAULT_BRIDGE_HOST",
    "DEFAULT_BRIDGE_PORT",
    "EXTENSION_DIR_NAME",
    "EXTENSION_VERSION",
    "FIREFOX_DIR_SUFFIX",
    "FIREFOX_GECKO_ID_PREFIX",
    "FIREFOX_MIN_VERSION",
    "TOKEN_FILENAME",
    "build_background_js",
    "build_content_js",
    "build_extension_binding_transport_js",
    "build_extension_handoff_transport_js",
    "build_manifest_json",
    "build_monitor_core_js",
    "build_popup_html",
    "build_popup_js",
    "build_userscript_binding_transport_js",
    "build_userscript_handoff_transport_js",
    "build_userscript_js",
    "export_browser_extension",
    "format_host_for_url",
    "get_default_extension_dir",
    "get_firefox_extension_dir",
    "is_loopback_host",
    "load_or_create_bridge_token",
    "normalize_bind_host",
    "resolve_client_host",
]

def _normalize_browser_target(browser: str) -> str:
    normalized = (browser or "").strip().lower()
    if normalized in ("chrome", "chromium", "edge"):
        return "chrome"
    if normalized == "firefox":
        return "firefox"
    raise ValueError(f"未対応のブラウザ種別です: {browser}")


def build_manifest_json(
    host: str = DEFAULT_BRIDGE_HOST,
    port: int = DEFAULT_BRIDGE_PORT,
    browser: str = "chrome",
) -> str:
    """Google AI Studio監視用のManifest V3マニフェストをブラウザ種別に応じて生成する。"""
    target_browser = _normalize_browser_target(browser)
    url_host = format_host_for_url(resolve_client_host(host))
    name_suffix = "" if port == DEFAULT_BRIDGE_PORT else f" (:{port})"
    manifest: Dict[str, Any] = {
        "manifest_version": 3,
        "name": f"aiskel AI Studio Auto-Runner{name_suffix}",
        "version": EXTENSION_VERSION,
        "description": "Google AI Studioの対象タブをVSCodeと紐づけ、応答内の aiskel c コマンドをローカルのaiskelブリッジへ送信して自動実行します。",
        "permissions": ["tabs"],
        "host_permissions": [
            f"http://{url_host}:{port}/*",
        ],
        "action": {
            "default_title": f"aiskel AI Studio Bridge{name_suffix}",
            "default_popup": "popup.html",
        },
    }
    if target_browser == "firefox":
        # Firefoxでtabs.create({ cookieStoreId })を維持するためcookies権限を付与
        manifest["permissions"] = ["tabs", "cookies"]
        addon_id = (
            f"{FIREFOX_GECKO_ID_PREFIX}@aiskel.local"
            if port == DEFAULT_BRIDGE_PORT
            else f"{FIREFOX_GECKO_ID_PREFIX}-{port}@aiskel.local"
        )
        # Firefox 140未満のエラー回避のためdata_collection_permissionsは含めず、IDと最小バージョンのみ宣言
        manifest["browser_specific_settings"] = {
            "gecko": {
                "id": addon_id,
                "strict_min_version": FIREFOX_MIN_VERSION,
            }
        }
        # MV3デフォルトCSPのupgrade-insecure-requestsによるローカルhttp://通信のhttps://昇格を防止
        manifest["content_security_policy"] = {
            "extension_pages": "script-src 'self'; object-src 'self';"
        }
        manifest["background"] = {
            "scripts": ["background.js"],
        }
    else:
        manifest["background"] = {
            "service_worker": "background.js",
        }
    manifest["content_scripts"] = [
        {
            "matches": ["https://aistudio.google.com/*"],
            "js": ["content.js"],
            "run_at": "document_idle",
        }
    ]
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

def _write_if_changed(file_path: Path, content: str) -> bool:
    if file_path.exists():
        try:
            if file_path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
    file_path.write_text(content, encoding="utf-8")
    return True


def export_browser_extension(
    target_dir: Path,
    host: str = DEFAULT_BRIDGE_HOST,
    port: int = DEFAULT_BRIDGE_PORT,
    token: Optional[str] = None,
    browser: str = "chrome",
) -> Tuple[Path, bool]:
    """指定ブラウザ向けManifest V3拡張機能一式を出力し、(出力ディレクトリ, 内容変更有無)を返す。"""
    target_browser = _normalize_browser_target(browser)
    resolved_dir = target_dir.resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    changed |= _write_if_changed(
        resolved_dir / "manifest.json",
        build_manifest_json(host=host, port=port, browser=target_browser),
    )
    changed |= _write_if_changed(resolved_dir / "background.js", build_background_js(host=host, port=port, token=token))
    changed |= _write_if_changed(resolved_dir / "content.js", build_content_js())
    changed |= _write_if_changed(resolved_dir / "popup.html", build_popup_html())
    changed |= _write_if_changed(resolved_dir / "popup.js", build_popup_js())
    return resolved_dir, changed
