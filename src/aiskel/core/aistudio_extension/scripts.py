"""Module: @role: Chrome拡張機能Content ScriptおよびUserscript用のDOM監視・通信クライアントJSを生成する。"""
import json
from typing import Optional

from .network import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    EXTENSION_VERSION,
    format_host_for_url,
    resolve_client_host,
)
from .ui import (
    build_extension_binding_transport_js,
    build_extension_handoff_transport_js,
    build_userscript_binding_transport_js,
    build_userscript_handoff_transport_js,
)
from ..aistudio_monitor_script import build_monitor_core_js

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
        "        if (!isMonitoringActive()) {\n"
        "          finish(false, null);\n"
        "          return;\n"
        "        }\n"
        "        if (chrome.runtime.lastError || !res || !res.reachable) {\n"
        "          const errDetail = (chrome.runtime.lastError && chrome.runtime.lastError.message) || 'ローカルブリッジへ接続できませんでした';\n"
        "          reportBridgeError('aiskel ブリッジ接続エラー', errDetail + '\\nコマンド: ' + (payload.commands || []).join(' ; '));\n"
        "          finish(false, null);\n"
        "          return;\n"
        "        }\n"
        "        const data = res.data || {};\n"
        "        const succeeded = Boolean(data.ok);\n"
        "        finish(succeeded, data);\n"
        "        if (data.ok && data.skipped) return;\n"
        "        if (data.ok) {\n"
        "          if (data.pasted) {\n"
        "            clearErrorPanel();\n"
        "            showToast('aiskel 自動実行完了: ' + (data.command || ''), false);\n"
        "          } else if (data.paste_reason) {\n"
        "            reportBridgeError(\n"
        "              'aiskel 自動貼り付けスキップ',\n"
        "              data.paste_reason + '\\nコマンド: ' + (data.command || '')\n"
        "            );\n"
        "          } else {\n"
        "            showToast('aiskel 自動実行完了: ' + (data.command || ''), false);\n"
        "          }\n"
        "        } else if (res.status === 409) {\n"
        "          if (typeof refreshBindingStatus === 'function') refreshBindingStatus();\n"
        "          reportBridgeError('aiskel タブ紐づけエラー', data.message || 'タブがVSCodeと紐づけられていません');\n"
        "        } else if (data.pasted) {\n"
        "          reportBridgeError(\n"
        "            'aiskel 実行失敗 (エラー内容を貼付済)',\n"
        "            (data.message || res.error || String(res.status)) + '\\nコマンド: ' + (data.command || '')\n"
        "          );\n"
        "        } else {\n"
        "          const reasonParts = [data.message || res.error || ('HTTP ' + res.status)];\n"
        "          if (data.paste_reason) reasonParts.push('貼付失敗: ' + data.paste_reason);\n"
        "          if (data.command) reasonParts.push('コマンド: ' + data.command);\n"
        "          reportBridgeError('aiskel 実行エラー', reasonParts.join('\\n'));\n"
        "        }\n"
        "      });\n"
        "    } catch (err) {\n"
        "      // 拡張機能リロード後の未リロードタブでの同期例外を捕捉しinFlight残存を防止\n"
        "      console.debug('[aiskel] 拡張機能コンテキストが無効化されました。タブを再読み込みしてください:', err);\n"
        "      reportBridgeError('aiskel 拡張機能エラー', '拡張機能コンテキストが無効化されました。タブを再読み込みしてください: ' + String(err));\n"
        "      finish(false, null);\n"
        "    }\n"
    )
    trigger_run_snippet = (
        "    try {\n"
        "      chrome.runtime.sendMessage({ type: 'AISKEL_TRIGGER_RUN', payload }, (res) => {\n"
        "        if (!isMonitoringActive()) {\n"
        "          if (onDone) onDone(false, 'タブが未紐づけのため送信を中止しました');\n"
        "          return;\n"
        "        }\n"
        "        if (chrome.runtime.lastError || !res || !res.reachable) {\n"
        "          if (onDone) onDone(false, (chrome.runtime.lastError && chrome.runtime.lastError.message) || 'ブリッジ通信失敗');\n"
        "          return;\n"
        "        }\n"
        "        const data = res.data || {};\n"
        "        if (onDone) onDone(Boolean(data.ok), String(data.message || res.error || ''));\n"
        "      });\n"
        "    } catch (err) {\n"
        "      if (onDone) onDone(false, String(err));\n"
        "    }\n"
    )
    return build_monitor_core_js(
        send_snippet,
        binding_transport_snippet=build_extension_binding_transport_js(),
        handoff_transport_snippet=build_extension_handoff_transport_js(),
        trigger_run_snippet=trigger_run_snippet,
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
        "// @run-at       document-idle\n"
        "// @grant        GM_xmlhttpRequest\n"
        "// @grant        GM_openInTab\n"
        # Tampermonkey等の@connectは裸のドメイン・IPを要求するため角括弧なしのclient_hostを指定
        f"// @connect      {client_host}\n"
        "// ==/UserScript==\n\n"
    )
    base_url = f"http://{url_host}:{port}"
    trigger_endpoint_literal = json.dumps(f"{base_url}/api/trigger_run")
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
        "          if (!isMonitoringActive()) {\n"
        "            finish(false, null);\n"
        "            return;\n"
        "          }\n"
        "          try {\n"
        "            const data = JSON.parse(resp.responseText || '{}');\n"
        "            const succeeded = Boolean(data.ok);\n"
        "            finish(succeeded, data);\n"
        "            if (data.ok && data.skipped) return;\n"
        "            if (resp.status === 409) {\n"
        "              if (typeof refreshBindingStatus === 'function') refreshBindingStatus();\n"
        "              reportBridgeError('aiskel タブ紐づけエラー', data.message || 'タブがVSCodeと紐づけられていません');\n"
        "              return;\n"
        "            }\n"
        "            if (data.ok) {\n"
        "              if (data.pasted) {\n"
        "                clearErrorPanel();\n"
        "                showToast('aiskel 自動実行完了: ' + (data.command || ''), false);\n"
        "              } else if (data.paste_reason) {\n"
        "                reportBridgeError('aiskel 自動貼り付けスキップ', data.paste_reason + '\\nコマンド: ' + (data.command || ''));\n"
        "              } else {\n"
        "                showToast('aiskel 自動実行完了: ' + (data.command || ''), false);\n"
        "              }\n"
        "            } else if (data.pasted) {\n"
        "              reportBridgeError(\n"
        "                'aiskel 実行失敗 (エラー内容を貼付済)',\n"
        "                (data.message || ('HTTP ' + resp.status)) + '\\nコマンド: ' + (data.command || '')\n"
        "              );\n"
        "            } else {\n"
        "              const reasonParts = [data.message || ('HTTP ' + resp.status)];\n"
        "              if (data.paste_reason) reasonParts.push('貼付失敗: ' + data.paste_reason);\n"
        "              if (data.command) reasonParts.push('コマンド: ' + data.command);\n"
        "              reportBridgeError('aiskel 実行エラー', reasonParts.join('\\n'));\n"
        "            }\n"
        "          } catch (_e) {\n"
        "            reportBridgeError('aiskel 応答解析エラー', 'ブリッジ応答JSONの解析に失敗しました (HTTP ' + resp.status + ')');\n"
        "            finish(false, null);\n"
        "          }\n"
        "        },\n"
        "        onerror: () => {\n"
        "          if (!isMonitoringActive()) {\n"
        "            finish(false, null);\n"
        "            return;\n"
        "          }\n"
        "          reportBridgeError('aiskel ブリッジ接続エラー', 'ローカルブリッジへ接続できませんでした');\n"
        "          finish(false, null);\n"
        "        },\n"
        "        ontimeout: () => {\n"
        "          if (!isMonitoringActive()) {\n"
        "            finish(false, null);\n"
        "            return;\n"
        "          }\n"
        "          reportBridgeError('aiskel ブリッジ通信タイムアウト', 'ローカルブリッジへの送信がタイムアウトしました');\n"
        "          finish(false, null);\n"
        "        },\n"
        "        onabort: () => {\n"
        "          finish(false, null);\n"
        "        },\n"
        "      });\n"
        "    } catch (err) {\n"
        "      reportBridgeError('aiskel 送信エラー', String(err));\n"
        "      finish(false, null);\n"
        "    }\n"
    )
    trigger_run_snippet = (
        f"    const triggerEndpoint = {trigger_endpoint_literal};\n"
        f"    const triggerToken = {token_literal};\n"
        "    if (typeof GM_xmlhttpRequest !== 'function') {\n"
        "      if (onDone) onDone(false, 'GM_xmlhttpRequestが利用できません');\n"
        "      return;\n"
        "    }\n"
        "    const headers = { 'Content-Type': 'application/json' };\n"
        "    if (triggerToken) headers['X-Aiskel-Token'] = triggerToken;\n"
        "    try {\n"
        "      GM_xmlhttpRequest({\n"
        "        method: 'POST',\n"
        "        url: triggerEndpoint,\n"
        "        headers,\n"
        "        data: JSON.stringify(payload),\n"
        "        onload: (resp) => {\n"
        "          if (!isMonitoringActive()) {\n"
        "            if (onDone) onDone(false, 'タブが未紐づけのため送信を中止しました');\n"
        "            return;\n"
        "          }\n"
        "          try {\n"
        "            const data = JSON.parse(resp.responseText || '{}');\n"
        "            if (onDone) onDone(Boolean(data.ok), String(data.message || ('HTTP ' + resp.status)));\n"
        "          } catch (_e) {\n"
        "            if (onDone) onDone(false, '応答JSON解析失敗');\n"
        "          }\n"
        "        },\n"
        "        onerror: () => {\n"
        "          if (onDone) onDone(false, isMonitoringActive() ? 'ブリッジ接続失敗' : 'タブが未紐づけのため送信を中止しました');\n"
        "        },\n"
        "        ontimeout: () => {\n"
        "          if (onDone) onDone(false, isMonitoringActive() ? 'タイムアウト' : 'タブが未紐づけのため送信を中止しました');\n"
        "        },\n"
        "      });\n"
        "    } catch (err) {\n"
        "      if (onDone) onDone(false, String(err));\n"
        "    }\n"
    )
    return header + build_monitor_core_js(
        send_snippet,
        binding_transport_snippet=build_userscript_binding_transport_js(base_url=base_url, token=token),
        handoff_transport_snippet=build_userscript_handoff_transport_js(),
        trigger_run_snippet=trigger_run_snippet,
    )
