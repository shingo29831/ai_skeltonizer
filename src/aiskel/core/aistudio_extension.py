"""Module: @role: Google AI Studioの推論完了監視およびローカルブリッジ連携用のChrome拡張機能・Userscript資産を生成する。"""
import ipaddress
import json
import os
import secrets
from pathlib import Path
from typing import Optional, Tuple

from .aistudio_monitor_script import build_monitor_core_js

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 18765
EXTENSION_DIR_NAME = "aistudio_bridge"
TOKEN_FILENAME = ".bridge_token"


def _parse_ip_without_zone(raw_host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """IPリテラルを解析し、ブラウザURLで非対応のIPv6ゾーンID(%...)を拒否する。"""
    try:
        addr = ipaddress.ip_address(raw_host)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and getattr(addr, "scope_id", None):
        raise ValueError(f"IPv6ゾーンID付きホストはブラウザ連携URLでサポートされていません: {raw_host}")
    return addr


def normalize_bind_host(host: str) -> str:
    """ソケットbind用にIPv6角括弧を除去し、IPリテラルの表記ゆれを正規化する。"""
    normalized = (host or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("[") and normalized.endswith("]"):
        inner = normalized[1:-1].strip()
        addr = _parse_ip_without_zone(inner)
        if isinstance(addr, ipaddress.IPv6Address):
            return addr.compressed
        return normalized
    addr = _parse_ip_without_zone(normalized)
    if addr is not None:
        return addr.compressed
    return normalized


def format_host_for_url(host: str) -> str:
    """URLホスト部用にIPv6リテラルをRFC 3986準拠の角括弧付きへ整形する。"""
    unwrapped = normalize_bind_host(host)
    if not unwrapped:
        return unwrapped
    addr = _parse_ip_without_zone(unwrapped)
    if isinstance(addr, ipaddress.IPv6Address):
        return f"[{addr.compressed}]"
    return unwrapped


def resolve_client_host(bind_host: str) -> str:
    """ワイルドカード待受ホストをブラウザ接続可能なループバックIPへ変換する。"""
    normalized = normalize_bind_host(bind_host)
    if not normalized:
        return "127.0.0.1"
    addr = _parse_ip_without_zone(normalized)
    if addr is not None:
        if addr.is_unspecified:
            # IPV6_V6ONLY環境でのlocalhost→127.0.0.1解決失敗を防ぐためIPv6は::1へ変換
            return "::1" if isinstance(addr, ipaddress.IPv6Address) else "127.0.0.1"
        return addr.compressed
    return normalized


def is_loopback_host(host: str) -> bool:
    """指定ホストがIPv4/IPv6の表記ゆれを含めループバックに該当するか判定する。"""
    normalized = normalize_bind_host(host)
    if normalized.lower() == "localhost":
        return True
    addr = _parse_ip_without_zone(normalized)
    return bool(addr is not None and addr.is_loopback)


def get_default_extension_dir(
    port: int = DEFAULT_BRIDGE_PORT,
    home_dir: Optional[Path] = None,
) -> Path:
    """ポート競合を避ける拡張機能ファイルのデフォルト出力先パスを返す。"""
    base = home_dir if home_dir is not None else Path.home()
    dir_name = EXTENSION_DIR_NAME if port == DEFAULT_BRIDGE_PORT else f"{EXTENSION_DIR_NAME}_{port}"
    return base / ".aiskel" / "extensions" / dir_name


def _write_token_file(token_path: Path, token_value: str) -> None:
    content = token_value + "\n"
    if token_path.is_file():
        try:
            if token_path.read_text(encoding="utf-8") == content:
                return
        except OSError:
            pass
    token_path.write_text(content, encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass


def load_or_create_bridge_token(
    extension_dir: Path,
    explicit_token: Optional[str] = None,
) -> str:
    """明示トークンまたは拡張ディレクトリに永続化されたランダム認証トークンを返す。"""
    resolved_dir = extension_dir.resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    token_path = resolved_dir / TOKEN_FILENAME

    # 次回--token省略起動時の不整合を防ぐため明示トークンも永続化
    if explicit_token is not None and explicit_token.strip():
        cleaned = explicit_token.strip()
        _write_token_file(token_path, cleaned)
        return cleaned

    if token_path.is_file():
        try:
            existing = token_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass

    generated = secrets.token_urlsafe(24)
    _write_token_file(token_path, generated)
    return generated


def build_manifest_json(host: str = DEFAULT_BRIDGE_HOST, port: int = DEFAULT_BRIDGE_PORT) -> str:
    """Google AI Studio監視用のManifest V3マニフェストを生成する。"""
    url_host = format_host_for_url(resolve_client_host(host))
    name_suffix = "" if port == DEFAULT_BRIDGE_PORT else f" (:{port})"
    manifest = {
        "manifest_version": 3,
        "name": f"aiskel AI Studio Auto-Runner{name_suffix}",
        "version": "1.2.2",
        "description": "Google AI Studioの推論完了を検知し、応答内の aiskel c コマンドをローカルのaiskelブリッジへ送信して自動実行します。",
        "host_permissions": [
            f"http://{url_host}:{port}/*",
        ],
        "background": {
            "service_worker": "background.js",
        },
        "content_scripts": [
            {
                "matches": ["https://aistudio.google.com/*"],
                "js": ["content.js"],
                "run_at": "document_idle",
            }
        ],
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def build_background_js(
    host: str = DEFAULT_BRIDGE_HOST,
    port: int = DEFAULT_BRIDGE_PORT,
    token: Optional[str] = None,
) -> str:
    """ページCSPやMixed Contentを回避してローカルブリッジへPOST中継するService Workerを生成する。"""
    url_host = format_host_for_url(resolve_client_host(host))
    endpoint = f"http://{url_host}:{port}/api/execute"
    token_literal = json.dumps(token or "")
    endpoint_literal = json.dumps(endpoint)
    return (
        "'use strict';\n\n"
        f"const BRIDGE_ENDPOINT = {endpoint_literal};\n"
        f"const BRIDGE_TOKEN = {token_literal};\n\n"
        "chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {\n"
        "  if (!message || message.type !== 'AISKEL_EXECUTE') {\n"
        "    return false;\n"
        "  }\n"
        "  const headers = { 'Content-Type': 'application/json' };\n"
        "  if (BRIDGE_TOKEN) {\n"
        "    headers['X-Aiskel-Token'] = BRIDGE_TOKEN;\n"
        "  }\n"
        "  fetch(BRIDGE_ENDPOINT, {\n"
        "    method: 'POST',\n"
        "    headers,\n"
        "    body: JSON.stringify(message.payload || {}),\n"
        "  })\n"
        "    .then(async (res) => {\n"
        "      const data = await res.json().catch(() => ({}));\n"
        "      sendResponse({ connected: true, status: res.status, data });\n"
        "    })\n"
        "    .catch((err) => {\n"
        "      sendResponse({ connected: false, error: String(err) });\n"
        "    });\n"
        "  return true;\n"
        "});\n"
    )


def build_content_js() -> str:
    """Chrome拡張機能のContent Script用DOM監視スクリプトを生成する。"""
    send_snippet = (
        "    let settled = false;\n"
        "    const finish = (ok) => {\n"
        "      if (settled) return;\n"
        "      settled = true;\n"
        "      if (onDone) onDone(ok);\n"
        "    };\n"
        "    try {\n"
        "      chrome.runtime.sendMessage({ type: 'AISKEL_EXECUTE', payload }, (res) => {\n"
        "        if (chrome.runtime.lastError || !res || !res.connected) {\n"
        "          console.debug('[aiskel] ローカルブリッジ未接続のためスキップしました');\n"
        "          finish(false);\n"
        "          return;\n"
        "        }\n"
        "        const data = res.data || {};\n"
        "        const succeeded = Boolean(data.ok || data.skipped);\n"
        "        finish(succeeded);\n"
        "        if (data.skipped) return;\n"
        "        if (data.ok) {\n"
        "          showToast('aiskel 自動実行完了: ' + (data.command || ''), false);\n"
        "        } else {\n"
        "          showToast('aiskel 実行エラー: ' + (data.message || res.status), true);\n"
        "        }\n"
        "      });\n"
        "    } catch (err) {\n"
        "      // 拡張機能リロード後の未リロードタブでの同期例外を捕捉しinFlight残存を防止\n"
        "      console.debug('[aiskel] 拡張機能コンテキストが無効化されました。タブを再読み込みしてください:', err);\n"
        "      finish(false);\n"
        "    }\n"
    )
    return build_monitor_core_js(send_snippet)


def build_userscript_js(
    host: str = DEFAULT_BRIDGE_HOST,
    port: int = DEFAULT_BRIDGE_PORT,
    token: Optional[str] = None,
) -> str:
    """Tampermonkey/Violentmonkey専用の監視スクリプトを生成する(CSP回避のためGM_xmlhttpRequest必須)。"""
    client_host = resolve_client_host(host)
    url_host = format_host_for_url(client_host)
    endpoint = f"http://{url_host}:{port}/api/execute"
    endpoint_literal = json.dumps(endpoint)
    token_literal = json.dumps(token or "")
    header = (
        "// ==UserScript==\n"
        "// @name         aiskel AI Studio Auto-Runner\n"
        "// @namespace    https://github.com/aiskel\n"
        "// @version      1.2.2\n"
        "// @description  Google AI Studio推論完了時に応答内の aiskel c コマンドを自動実行 (Tampermonkey/Violentmonkey専用)\n"
        "// @match        https://aistudio.google.com/*\n"
        "// @grant        GM_xmlhttpRequest\n"
        # Tampermonkey等の@connectは裸のドメイン・IPを要求するため角括弧なしのclient_hostを指定
        f"// @connect      {client_host}\n"
        "// ==/UserScript==\n\n"
    )
    send_snippet = (
        f"    const endpoint = {endpoint_literal};\n"
        f"    const token = {token_literal};\n"
        "    let settled = false;\n"
        "    const finish = (ok) => {\n"
        "      if (settled) return;\n"
        "      settled = true;\n"
        "      if (onDone) onDone(ok);\n"
        "    };\n"
        "    if (typeof GM_xmlhttpRequest !== 'function') {\n"
        "      console.warn('[aiskel] GM_xmlhttpRequest が利用できません。Tampermonkey 等の Userscript マネージャまたは Chrome 拡張機能を使用してください。');\n"
        "      finish(false);\n"
        "      return;\n"
        "    }\n"
        "    const headers = { 'Content-Type': 'application/json' };\n"
        "    if (token) headers['X-Aiskel-Token'] = token;\n"
        "    try {\n"
        "      GM_xmlhttpRequest({\n"
        "        method: 'POST',\n"
        "        url: endpoint,\n"
        "        headers,\n"
        "        data: JSON.stringify(payload),\n"
        "        onload: (resp) => {\n"
        "          try {\n"
        "            const data = JSON.parse(resp.responseText || '{}');\n"
        "            finish(Boolean(data.ok || data.skipped));\n"
        "            if (data.skipped) return;\n"
        "            showToast(data.ok ? 'aiskel 自動実行完了: ' + (data.command || '') : 'aiskel 実行エラー: ' + (data.message || resp.status), !data.ok);\n"
        "          } catch (_e) {\n"
        "            finish(false);\n"
        "          }\n"
        "        },\n"
        "        onerror: () => {\n"
        "          console.debug('[aiskel] ローカルブリッジ未接続のためスキップしました');\n"
        "          finish(false);\n"
        "        },\n"
        "        ontimeout: () => {\n"
        "          finish(false);\n"
        "        },\n"
        "        onabort: () => {\n"
        "          finish(false);\n"
        "        },\n"
        "      });\n"
        "    } catch (err) {\n"
        "      console.debug('[aiskel] ブリッジ送信に失敗しました:', err);\n"
        "      finish(false);\n"
        "    }\n"
    )
    return header + build_monitor_core_js(send_snippet)


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
) -> Tuple[Path, bool]:
    """Chrome/Edge用Manifest V3拡張機能一式を出力し、(出力ディレクトリ, 内容変更有無)を返す。"""
    resolved_dir = target_dir.resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    changed |= _write_if_changed(resolved_dir / "manifest.json", build_manifest_json(host=host, port=port))
    changed |= _write_if_changed(resolved_dir / "background.js", build_background_js(host=host, port=port, token=token))
    changed |= _write_if_changed(resolved_dir / "content.js", build_content_js())
    return resolved_dir, changed
