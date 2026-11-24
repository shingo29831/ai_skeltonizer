"""Module: @role: Google AI Studioの推論完了監視およびローカルブリッジ連携用のブラウザ拡張機能(Chrome/Edge/Firefox)・Userscript資産を生成する。"""
import ipaddress
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .aistudio_extension_ui import (
    build_extension_binding_transport_js,
    build_extension_handoff_transport_js,
    build_popup_html,
    build_popup_js,
    build_userscript_binding_transport_js,
    build_userscript_handoff_transport_js,
)
from .aistudio_monitor_script import build_monitor_core_js

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 18765
EXTENSION_DIR_NAME = "aistudio_bridge"
EXTENSION_VERSION = "1.3.0"
FIREFOX_DIR_SUFFIX = "_firefox"
FIREFOX_GECKO_ID_PREFIX = "aiskel-aistudio-bridge"
FIREFOX_MIN_VERSION = "109.0"
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


def get_firefox_extension_dir(extension_dir: Path) -> Path:
    """Chrome/Edge用拡張ディレクトリに対応するFirefox用拡張ディレクトリパスを返す。"""
    resolved = extension_dir.resolve()
    if not resolved.name:
        return resolved / "firefox"
    return resolved.parent / f"{resolved.name}{FIREFOX_DIR_SUFFIX}"


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


def build_background_js(
    host: str = DEFAULT_BRIDGE_HOST,
    port: int = DEFAULT_BRIDGE_PORT,
    token: Optional[str] = None,
) -> str:
    """ページCSPやMixed Contentを回避してローカルブリッジへAPI中継およびタブ紐づけ管理を行うService Workerを生成する。"""
    url_host = format_host_for_url(resolve_client_host(host))
    base_url = f"http://{url_host}:{port}"
    token_literal = json.dumps(token or "")
    base_url_literal = json.dumps(base_url)
    return (
        "'use strict';\n\n"
        f"const BRIDGE_BASE_URL = {base_url_literal};\n"
        f"const BRIDGE_TOKEN = {token_literal};\n"
        "const AISTUDIO_TAB_PATTERNS = ['https://aistudio.google.com/*'];\n"
        "const pendingHandoffsByTab = new Map();\n\n"
        "function appendHandoffHash(urlStr, handoffId) {\n"
        "  try {\n"
        "    const u = new URL(urlStr);\n"
        "    const raw = (u.hash || '').replace(/^#/, '');\n"
        "    const parts = raw ? raw.split('&').filter((p) => p && !p.startsWith('aiskel_handoff=')) : [];\n"
        "    parts.push('aiskel_handoff=' + encodeURIComponent(handoffId));\n"
        "    u.hash = parts.join('&');\n"
        "    return u.toString();\n"
        "  } catch (_e) {\n"
        "    const sep = urlStr.indexOf('#') === -1 ? '#' : '&';\n"
        "    return urlStr + sep + 'aiskel_handoff=' + encodeURIComponent(handoffId);\n"
        "  }\n"
        "}\n\n"
        "function createTabPreservingContext(baseProps, senderTab, cb) {\n"
        "  const fullProps = Object.assign({}, baseProps);\n"
        "  if (senderTab) {\n"
        "    if (senderTab.windowId != null) fullProps.windowId = senderTab.windowId;\n"
        "    if (typeof senderTab.index === 'number') fullProps.index = senderTab.index + 1;\n"
        "    if (senderTab.id != null) fullProps.openerTabId = senderTab.id;\n"
        "    if (senderTab.cookieStoreId) fullProps.cookieStoreId = senderTab.cookieStoreId;\n"
        "  }\n"
        "  chrome.tabs.create(fullProps, (tab) => {\n"
        "    if (!chrome.runtime.lastError && tab && tab.id != null) {\n"
        "      cb(tab);\n"
        "      return;\n"
        "    }\n"
        "    // cookieStoreId/openerTabId非対応や権限制約時は基本プロパティでフォールバック\n"
        "    chrome.tabs.create(baseProps, (fallbackTab) => {\n"
        "      if (chrome.runtime.lastError || !fallbackTab || fallbackTab.id == null) {\n"
        "        cb(null);\n"
        "        return;\n"
        "      }\n"
        "      cb(fallbackTab);\n"
        "    });\n"
        "  });\n"
        "}\n\n"
        "function formatTabId(tabId) {\n"
        "  return 'chrome-tab:' + String(tabId);\n"
        "}\n\n"
        "function getSenderTabId(sender) {\n"
        "  if (sender && sender.tab && sender.tab.id != null) {\n"
        "    return formatTabId(sender.tab.id);\n"
        "  }\n"
        "  return '';\n"
        "}\n\n"
        "async function bridgeRequest(path, method, bodyObj) {\n"
        "  const headers = {};\n"
        "  if (BRIDGE_TOKEN) {\n"
        "    headers['X-Aiskel-Token'] = BRIDGE_TOKEN;\n"
        "  }\n"
        "  const init = { method, headers };\n"
        "  if (bodyObj !== undefined) {\n"
        "    headers['Content-Type'] = 'application/json';\n"
        "    init.body = JSON.stringify(bodyObj);\n"
        "  }\n"
        "  const res = await fetch(BRIDGE_BASE_URL + path, init);\n"
        "  const data = await res.json().catch(() => ({}));\n"
        "  const apiOk = res.status >= 200 && res.status < 300 && Boolean(data && data.ok === true);\n"
        "  return {\n"
        "    reachable: true,\n"
        "    connected: apiOk,\n"
        "    status: res.status,\n"
        "    data,\n"
        "    error: apiOk ? '' : String((data && data.message) || ('HTTP ' + res.status)),\n"
        "  };\n"
        "}\n\n"
        "function enrichBindingData(data, currentTabId, isConnected) {\n"
        "  const copy = Object.assign({}, data || {});\n"
        "  copy.current_tab_id = currentTabId || '';\n"
        "  copy.is_current_tab_bound = Boolean(isConnected && copy.bound && currentTabId && copy.tab_id === currentTabId);\n"
        "  return copy;\n"
        "}\n\n"
        "function broadcastBindingUpdated(bindingData) {\n"
        "  try {\n"
        "    chrome.tabs.query({ url: AISTUDIO_TAB_PATTERNS }, (tabs) => {\n"
        "      if (chrome.runtime.lastError || !Array.isArray(tabs)) return;\n"
        "      for (const tab of tabs) {\n"
        "        if (tab.id == null) continue;\n"
        "        try {\n"
        "          chrome.tabs.sendMessage(\n"
        "            tab.id,\n"
        "            { type: 'AISKEL_BINDING_UPDATED', binding: bindingData, tab_id: formatTabId(tab.id) },\n"
        "            () => { void chrome.runtime.lastError; }\n"
        "          );\n"
        "        } catch (_e) {}\n"
        "      }\n"
        "    });\n"
        "  } catch (_e) {}\n"
        "}\n\n"
        "function resolveTargetTab(message, sender, cb) {\n"
        "  const explicitId = message && message.tabId != null ? message.tabId : (sender && sender.tab ? sender.tab.id : null);\n"
        "  if (explicitId == null) {\n"
        "    cb(null);\n"
        "    return;\n"
        "  }\n"
        "  try {\n"
        "    chrome.tabs.get(explicitId, (tab) => {\n"
        "      if (chrome.runtime.lastError || !tab) {\n"
        "        cb(sender && sender.tab ? sender.tab : { id: explicitId, title: message.tab_title || '', url: message.tab_url || '' });\n"
        "        return;\n"
        "      }\n"
        "      cb(tab);\n"
        "    });\n"
        "  } catch (_e) {\n"
        "    cb(sender && sender.tab ? sender.tab : { id: explicitId, title: message.tab_title || '', url: message.tab_url || '' });\n"
        "  }\n"
        "}\n\n"
        "chrome.tabs.onRemoved.addListener((tabId) => {\n"
        "  if (tabId == null) return;\n"
        "  pendingHandoffsByTab.delete(formatTabId(tabId));\n"
        "  bridgeRequest('/api/unbind', 'POST', { tab_id: formatTabId(tabId) })\n"
        "    .then((res) => {\n"
        "      if (res.connected && res.data) {\n"
        "        broadcastBindingUpdated(res.data);\n"
        "      }\n"
        "    })\n"
        "    .catch(() => {});\n"
        "});\n\n"
        "chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {\n"
        "  if (!message || !message.type) {\n"
        "    return false;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_EXECUTE') {\n"
        "    const payload = Object.assign({}, message.payload || {});\n"
        "    const senderTabId = getSenderTabId(sender);\n"
        "    if (senderTabId) {\n"
        "      payload.tab_id = senderTabId;\n"
        "    } else if (!payload.tab_id) {\n"
        "      payload.tab_id = 'unknown-tab';\n"
        "    }\n"
        "    bridgeRequest('/api/execute', 'POST', payload)\n"
        "      .then((res) => sendResponse(res))\n"
        "      .catch((err) => sendResponse({ reachable: false, connected: false, status: 0, data: {}, error: String(err) }));\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_GET_STATUS') {\n"
        "    const currentTabId = getSenderTabId(sender) || (message.tab_id || '');\n"
        "    bridgeRequest('/api/binding', 'GET')\n"
        "      .then((res) => {\n"
        "        res.data = enrichBindingData(res.data, currentTabId, res.connected);\n"
        "        sendResponse(res);\n"
        "      })\n"
        "      .catch((err) => sendResponse({ reachable: false, connected: false, status: 0, data: {}, error: String(err) }));\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_BIND_TAB') {\n"
        "    resolveTargetTab(message, sender, (tab) => {\n"
        "      if (!tab || tab.id == null) {\n"
        "        sendResponse({ reachable: false, connected: false, status: 0, data: {}, error: '対象タブを特定できませんでした' });\n"
        "        return;\n"
        "      }\n"
        "      const tabIdStr = formatTabId(tab.id);\n"
        "      bridgeRequest('/api/bind', 'POST', {\n"
        "        tab_id: tabIdStr,\n"
        "        tab_title: tab.title || message.tab_title || '',\n"
        "        tab_url: tab.url || message.tab_url || '',\n"
        "      })\n"
        "        .then((res) => {\n"
        "          if (res.connected && res.data) {\n"
        "            broadcastBindingUpdated(res.data);\n"
        "          }\n"
        "          res.data = enrichBindingData(res.data, tabIdStr, res.connected);\n"
        "          sendResponse(res);\n"
        "        })\n"
        "        .catch((err) => sendResponse({ reachable: false, connected: false, status: 0, data: {}, error: String(err) }));\n"
        "    });\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_UNBIND_TAB') {\n"
        "    const currentTabId = message.tabId != null ? formatTabId(message.tabId) : (getSenderTabId(sender) || '');\n"
        "    const body = message.force ? {} : (currentTabId ? { tab_id: currentTabId } : {});\n"
        "    bridgeRequest('/api/unbind', 'POST', body)\n"
        "      .then((res) => {\n"
        "        if (res.connected && res.data) {\n"
        "          broadcastBindingUpdated(res.data);\n"
        "        }\n"
        "        res.data = enrichBindingData(res.data, currentTabId, res.connected);\n"
        "        sendResponse(res);\n"
        "      })\n"
        "      .catch((err) => sendResponse({ reachable: false, connected: false, status: 0, data: {}, error: String(err) }));\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_LIST_TABS') {\n"
        "    chrome.tabs.query({ url: AISTUDIO_TAB_PATTERNS }, (tabs) => {\n"
        "      const safeTabs = Array.isArray(tabs) ? tabs : [];\n"
        "      bridgeRequest('/api/binding', 'GET')\n"
        "        .then((res) => {\n"
        "          const connected = Boolean(res.connected);\n"
        "          const binding = connected ? (res.data || {}) : {};\n"
        "          const mapped = safeTabs.map((t) => {\n"
        "            const tabIdStr = t.id != null ? formatTabId(t.id) : '';\n"
        "            return {\n"
        "              id: t.id,\n"
        "              tab_id: tabIdStr,\n"
        "              title: t.title || 'Google AI Studio',\n"
        "              url: t.url || '',\n"
        "              active: Boolean(t.active),\n"
        "              is_bound: Boolean(connected && binding.bound && tabIdStr && binding.tab_id === tabIdStr),\n"
        "            };\n"
        "          });\n"
        "          sendResponse({\n"
        "            reachable: Boolean(res.reachable),\n"
        "            connected,\n"
        "            status: res.status || 0,\n"
        "            error: res.error || '',\n"
        "            binding,\n"
        "            tabs: mapped,\n"
        "          });\n"
        "        })\n"
        "        .catch((err) => {\n"
        "          const mapped = safeTabs.map((t) => ({\n"
        "            id: t.id,\n"
        "            tab_id: t.id != null ? formatTabId(t.id) : '',\n"
        "            title: t.title || 'Google AI Studio',\n"
        "            url: t.url || '',\n"
        "            active: Boolean(t.active),\n"
        "            is_bound: false,\n"
        "          }));\n"
        "          sendResponse({ reachable: false, connected: false, status: 0, error: String(err), binding: {}, tabs: mapped });\n"
        "        });\n"
        "    });\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_FOCUS_TAB' && message.tabId != null) {\n"
        "    chrome.tabs.update(message.tabId, { active: true }, (tab) => {\n"
        "      if (chrome.runtime.lastError || !tab) {\n"
        "        sendResponse({ ok: false });\n"
        "        return;\n"
        "      }\n"
        "      if (tab.windowId != null) {\n"
        "        chrome.windows.update(tab.windowId, { focused: true }, () => sendResponse({ ok: true }));\n"
        "      } else {\n"
        "        sendResponse({ ok: true });\n"
        "      }\n"
        "    });\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_OPEN_AISTUDIO') {\n"
        "    chrome.tabs.create({ url: 'https://aistudio.google.com/', active: true }, (tab) => {\n"
        "      sendResponse({ ok: Boolean(tab && !chrome.runtime.lastError) });\n"
        "    });\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_PATCH_HANDOFF') {\n"
        "    const payload = Object.assign({}, message.payload || {});\n"
        "    const senderTabId = getSenderTabId(sender);\n"
        "    if (senderTabId) payload.tab_id = senderTabId;\n"
        "    bridgeRequest('/api/handoff', 'POST', payload)\n"
        "      .then((res) => {\n"
        "        const data = res.data || {};\n"
        "        if (!res.connected || !data.ok || data.skipped || !data.handoff_id) {\n"
        "          sendResponse(res);\n"
        "          return;\n"
        "        }\n"
        "        const baseUrl = data.new_chat_url || payload.new_chat_url || 'https://aistudio.google.com/prompts/new_chat';\n"
        "        const targetUrl = appendHandoffHash(baseUrl, data.handoff_id);\n"
        "        createTabPreservingContext({ url: targetUrl, active: true }, sender && sender.tab ? sender.tab : null, (newTab) => {\n"
        "          if (!newTab || newTab.id == null) {\n"
        "            sendResponse({ reachable: true, connected: false, status: 500, data: { ok: false, message: '新しいタブの生成に失敗しました' }, error: '新しいタブの生成に失敗しました' });\n"
        "            return;\n"
        "          }\n"
        "          const newTabIdStr = formatTabId(newTab.id);\n"
        "          // 新規タブ側の早期AISKEL_TAKE_HANDOFFが/api/bind完了を待機できるよう先にPromiseを登録\n"
        "          const bindPromise = bridgeRequest('/api/bind', 'POST', {\n"
        "            tab_id: newTabIdStr,\n"
        "            tab_title: newTab.title || 'Google AI Studio',\n"
        "            tab_url: newTab.url || newTab.pendingUrl || targetUrl,\n"
        "            handoff_id: data.handoff_id,\n"
        "          });\n"
        "          pendingHandoffsByTab.set(newTabIdStr, { handoffId: data.handoff_id, bindPromise });\n"
        "          bindPromise\n"
        "            .then((bindRes) => {\n"
        "              if (!bindRes || !bindRes.connected) {\n"
        "                pendingHandoffsByTab.delete(newTabIdStr);\n"
        "                sendResponse({\n"
        "                  reachable: Boolean(bindRes && bindRes.reachable),\n"
        "                  connected: false,\n"
        "                  status: (bindRes && bindRes.status) || 500,\n"
        "                  data: { ok: false, message: (bindRes && bindRes.error) || '新規タブの紐づけに失敗しました' },\n"
        "                  error: (bindRes && bindRes.error) || '新規タブの紐づけに失敗しました',\n"
        "                });\n"
        "                return;\n"
        "              }\n"
        "              if (bindRes.data) {\n"
        "                broadcastBindingUpdated(bindRes.data);\n"
        "              }\n"
        "              sendResponse({\n"
        "                reachable: true,\n"
        "                connected: true,\n"
        "                status: 200,\n"
        "                data: Object.assign({}, data, { new_tab_id: newTabIdStr, binding: bindRes.data || {} }),\n"
        "              });\n"
        "            })\n"
        "            .catch((err) => {\n"
        "              pendingHandoffsByTab.delete(newTabIdStr);\n"
        "              sendResponse({ reachable: false, connected: false, status: 0, data: { ok: false, message: String(err) }, error: String(err) });\n"
        "            });\n"
        "        });\n"
        "      })\n"
        "      .catch((err) => sendResponse({ reachable: false, connected: false, status: 0, data: {}, error: String(err) }));\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_TAKE_HANDOFF') {\n"
        "    const senderTabId = getSenderTabId(sender);\n"
        "    if (!senderTabId) {\n"
        "      sendResponse({ reachable: true, connected: true, status: 200, data: { ok: true, found: false } });\n"
        "      return false;\n"
        "    }\n"
        "    (async () => {\n"
        "      const pending = pendingHandoffsByTab.get(senderTabId);\n"
        "      let handoffId = message.handoff_id || '';\n"
        "      if (pending) {\n"
        "        if (!handoffId && pending.handoffId) handoffId = pending.handoffId;\n"
        "        await pending.bindPromise.catch(() => null);\n"
        "      }\n"
        "      const claimPayload = {\n"
        "        tab_id: senderTabId,\n"
        "        handoff_id: handoffId,\n"
        "        tab_title: message.tab_title || (sender && sender.tab ? sender.tab.title : '') || '',\n"
        "        tab_url: message.tab_url || (sender && sender.tab ? sender.tab.url : '') || '',\n"
        "      };\n"
        "      const res = await bridgeRequest('/api/handoff/claim', 'POST', claimPayload);\n"
        "      sendResponse(res);\n"
        "    })().catch((err) => sendResponse({ reachable: false, connected: false, status: 0, data: {}, error: String(err) }));\n"
        "    return true;\n"
        "  }\n"
        "  if (message.type === 'AISKEL_ACK_HANDOFF') {\n"
        "    const senderTabId = getSenderTabId(sender);\n"
        "    if (senderTabId) pendingHandoffsByTab.delete(senderTabId);\n"
        "    bridgeRequest('/api/handoff/ack', 'POST', { tab_id: senderTabId, handoff_id: message.handoff_id || '' })\n"
        "      .then((res) => sendResponse(res))\n"
        "      .catch((err) => sendResponse({ reachable: false, connected: false, status: 0, data: {}, error: String(err) }));\n"
        "    return true;\n"
        "  }\n"
        "  return false;\n"
        "});\n"
    )


def build_content_js() -> str:
    """Chrome拡張機能のContent Script用DOM監視スクリプトを生成する。"""
    send_snippet = (
        "    let settled = false;\n"
        "    const finish = (ok, data) => {\n"
        "      if (settled) return;\n"
        "      settled = true;\n"
        "      if (onDone) onDone(ok, data || null);\n"
        "    };\n"
        "    try {\n"
        "      chrome.runtime.sendMessage({ type: 'AISKEL_EXECUTE', payload }, (res) => {\n"
        "        if (chrome.runtime.lastError || !res || !res.reachable) {\n"
        "          console.debug('[aiskel] ローカルブリッジ未接続のためスキップしました');\n"
        "          finish(false, null);\n"
        "          return;\n"
        "        }\n"
        "        const data = res.data || {};\n"
        "        const succeeded = Boolean(data.ok);\n"
        "        finish(succeeded, data);\n"
        "        if (data.ok && data.skipped) return;\n"
        "        if (data.ok) {\n"
        "          showToast('aiskel 自動実行完了: ' + (data.command || ''), false);\n"
        "        } else if (res.status === 409) {\n"
        "          if (typeof refreshBindingStatus === 'function') refreshBindingStatus();\n"
        "          showToast('aiskel スキップ: ' + (data.message || 'タブがVSCodeと紐づけられていません'), true);\n"
        "        } else {\n"
        "          showToast('aiskel 実行エラー: ' + (data.message || res.error || res.status), true);\n"
        "        }\n"
        "      });\n"
        "    } catch (err) {\n"
        "      // 拡張機能リロード後の未リロードタブでの同期例外を捕捉しinFlight残存を防止\n"
        "      console.debug('[aiskel] 拡張機能コンテキストが無効化されました。タブを再読み込みしてください:', err);\n"
        "      finish(false, null);\n"
        "    }\n"
    )
    return build_monitor_core_js(
        send_snippet,
        binding_transport_snippet=build_extension_binding_transport_js(),
        handoff_transport_snippet=build_extension_handoff_transport_js(),
    )


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
        f"// @version      {EXTENSION_VERSION}\n"
        "// @description  Google AI Studio推論完了時に応答内の aiskel c コマンドを自動実行 (Tampermonkey/Violentmonkey専用)\n"
        "// @match        https://aistudio.google.com/*\n"
        "// @grant        GM_xmlhttpRequest\n"
        "// @grant        GM_openInTab\n"
        # Tampermonkey等の@connectは裸のドメイン・IPを要求するため角括弧なしのclient_hostを指定
        f"// @connect      {client_host}\n"
        "// ==/UserScript==\n\n"
    )
    send_snippet = (
        f"    const endpoint = {endpoint_literal};\n"
        f"    const token = {token_literal};\n"
        "    let settled = false;\n"
        "    const finish = (ok, data) => {\n"
        "      if (settled) return;\n"
        "      settled = true;\n"
        "      if (onDone) onDone(ok, data || null);\n"
        "    };\n"
        "    if (typeof GM_xmlhttpRequest !== 'function') {\n"
        "      console.warn('[aiskel] GM_xmlhttpRequest が利用できません。Tampermonkey 等の Userscript マネージャまたは Chrome 拡張機能を使用してください。');\n"
        "      finish(false, null);\n"
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
        "            const succeeded = Boolean(data.ok);\n"
        "            finish(succeeded, data);\n"
        "            if (data.ok && data.skipped) return;\n"
        "            if (resp.status === 409) {\n"
        "              if (typeof refreshBindingStatus === 'function') refreshBindingStatus();\n"
        "              showToast('aiskel スキップ: ' + (data.message || 'タブがVSCodeと紐づけられていません'), true);\n"
        "              return;\n"
        "            }\n"
        "            showToast(data.ok ? 'aiskel 自動実行完了: ' + (data.command || '') : 'aiskel 実行エラー: ' + (data.message || resp.status), !data.ok);\n"
        "          } catch (_e) {\n"
        "            finish(false, null);\n"
        "          }\n"
        "        },\n"
        "        onerror: () => {\n"
        "          console.debug('[aiskel] ローカルブリッジ未接続のためスキップしました');\n"
        "          finish(false, null);\n"
        "        },\n"
        "        ontimeout: () => {\n"
        "          finish(false, null);\n"
        "        },\n"
        "        onabort: () => {\n"
        "          finish(false, null);\n"
        "        },\n"
        "      });\n"
        "    } catch (err) {\n"
        "      console.debug('[aiskel] ブリッジ送信に失敗しました:', err);\n"
        "      finish(false, null);\n"
        "    }\n"
    )
    base_url = f"http://{url_host}:{port}"
    return header + build_monitor_core_js(
        send_snippet,
        binding_transport_snippet=build_userscript_binding_transport_js(base_url=base_url, token=token),
        handoff_transport_snippet=build_userscript_handoff_transport_js(),
    )


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
