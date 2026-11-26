"""Module: @role: ローカルブリッジHTTPサーバーのCORS・認証・タブ紐づけおよび実行APIハンドラを生成する。"""
import hmac
import json
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from .aistudio_bridge_state import sanitize_console_text

_BRIDGE_LANDING_HTML = (
    "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
    "<title>aiskel AI Studio Local Bridge</title>"
    "<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 20px;line-height:1.6;color:#1e293b;}"
    "code{background:#f1f5f9;padding:2px 6px;border-radius:4px;}"
    ".card{border:1px solid #e2e8f0;border-radius:10px;padding:20px;background:#f8fafc;}"
    "a{color:#2563eb;}</style></head><body>"
    "<div class='card'>"
    "<h2>🎧 aiskel AI Studio ローカルブリッジ稼働中</h2>"
    "<p>このローカルサーバーは Chrome 拡張機能 / Userscript からの通知を受信する専用APIです（Web画面ではありません）。</p>"
    "<p>👉 <a href='https://aistudio.google.com/' target='_blank' rel='noopener noreferrer'>Google AI Studio (https://aistudio.google.com/)</a> を開いてご利用ください。</p>"
    "<p>API: <code>GET /api/health</code> / <code>GET /api/binding</code> / <code>POST /api/bind</code> / <code>POST /api/execute</code></p>"
    "</div></body></html>"
).encode("utf-8")


def build_bridge_request_handler_class(
    bridge: Any,
    max_payload_bytes: int,
    socket_timeout_sec: float,
    is_allowed_origin_fn: Callable[[Optional[str], bool], bool],
    max_handoff_payload_bytes: int = 4 * 1024 * 1024,
) -> type[BaseHTTPRequestHandler]:
    """認証・タブ紐づけ・コマンド実行APIを提供するHTTPリクエストハンドラクラスを生成する。"""

    class _BridgeRequestHandler(BaseHTTPRequestHandler):
        # 不完全ボディ送信によるrfile.read永久ブロックを防止
        timeout = socket_timeout_sec

        def log_message(self, format: str, *args) -> None:
            return

        def _send_cors_headers(self, origin: Optional[str]) -> None:
            normalized_origin = (origin or "").strip()
            if normalized_origin and normalized_origin.lower() != "null" and is_allowed_origin_fn(normalized_origin, False):
                self.send_header("Access-Control-Allow-Origin", normalized_origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Aiskel-Token")
            # ChromeのPrivate Network Accessプリフライトを通過させるため付与
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _send_bytes(
            self,
            status_code: int,
            raw: bytes,
            content_type: str,
            include_cors: bool = True,
        ) -> None:
            try:
                self.send_response(status_code)
                if include_cors:
                    self._send_cors_headers(self.headers.get("Origin"))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                return

        def _send_json(self, status_code: int, body: Dict[str, object]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status_code, raw, "application/json; charset=utf-8", include_cors=True)

        def _verify_access(self) -> bool:
            origin = self.headers.get("Origin")
            token_verified = False
            if bridge.token:
                provided = (self.headers.get("X-Aiskel-Token") or "").strip()
                if not provided or not hmac.compare_digest(provided, bridge.token):
                    self._send_json(int(HTTPStatus.UNAUTHORIZED), {"ok": False, "message": "認証トークンが一致しません"})
                    return False
                token_verified = True

            # トークン未設定時はOrigin欠落・Origin: nullによる無認証ローカル実行を防止
            if not is_allowed_origin_fn(origin, token_verified):
                self._send_json(int(HTTPStatus.FORBIDDEN), {"ok": False, "message": "許可されていないOriginです"})
                return False
            return True

        def _read_json_body(
            self,
            allow_empty: bool = False,
            max_bytes: Optional[int] = None,
        ) -> Optional[Dict[str, object]]:
            limit_bytes = max_bytes if max_bytes is not None else max_payload_bytes
            content_length_raw = self.headers.get("Content-Length")
            if content_length_raw is None:
                if allow_empty:
                    return {}
                self._send_json(int(HTTPStatus.LENGTH_REQUIRED), {"ok": False, "message": "Content-Lengthヘッダが必要です"})
                return None
            try:
                content_length = int(content_length_raw)
            except ValueError:
                self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "不正なContent-Lengthです"})
                return None

            if content_length == 0 and allow_empty:
                return {}
            if content_length <= 0:
                self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "リクエストボディが空です"})
                return None
            if content_length > limit_bytes:
                self._send_json(int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE), {"ok": False, "message": "ペイロードサイズが上限を超えています"})
                return None

            try:
                raw_body = self.rfile.read(content_length)
            except (socket.timeout, TimeoutError):
                self._send_json(int(HTTPStatus.REQUEST_TIMEOUT), {"ok": False, "message": "リクエストボディの受信がタイムアウトしました"})
                return None
            except OSError:
                return None

            if len(raw_body) != content_length:
                self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "リクエストボディが不完全です"})
                return None
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "JSONの解析に失敗しました"})
                return None

            if not isinstance(payload, dict):
                self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "JSONオブジェクトを指定してください"})
                return None
            return payload

        def do_OPTIONS(self) -> None:
            origin = self.headers.get("Origin")
            if not is_allowed_origin_fn(origin, False):
                self._send_json(int(HTTPStatus.FORBIDDEN), {"ok": False, "message": "許可されていないOriginです"})
                return
            self.send_response(int(HTTPStatus.NO_CONTENT))
            self._send_cors_headers(origin)
            self.end_headers()

        def do_GET(self) -> None:
            req_path = urlsplit(self.path).path
            # コンソール上のURLを直接開いた際に404誤認を防ぐため非機密の案内ページを返す
            if req_path == "/":
                accept = (self.headers.get("Accept") or "").lower()
                if "text/html" in accept:
                    self._send_bytes(int(HTTPStatus.OK), _BRIDGE_LANDING_HTML, "text/html; charset=utf-8", include_cors=False)
                else:
                    self._send_json(
                        int(HTTPStatus.OK),
                        {
                            "ok": True,
                            "service": "aiskel-aistudio-bridge",
                            "message": "aiskel AI Studio ローカルブリッジは稼働中です。https://aistudio.google.com/ から拡張機能経由で利用してください。",
                            "endpoints": {
                                "health": "/api/health",
                                "binding": "/api/binding",
                                "bind": "/api/bind",
                                "unbind": "/api/unbind",
                                "prompt": "/api/prompt",
                                "summary_prompt": "/api/summary_prompt",
                                "handoff_max_tabs": "/api/handoff/max_tabs",
                                "bloat_targets": "/api/bloat/targets",
                                "split_prompt": "/api/split/prompt",
                                "split_handoff": "/api/split/handoff",
                                "organize_targets": "/api/organize/targets",
                                "organize_prompt": "/api/organize/prompt",
                                "organize_handoff": "/api/organize/handoff",
                                "config_prompt": "/api/config/prompt",
                                "config_handoff": "/api/config/handoff",
                                "tasks_advance": "/api/tasks/advance",
                                "tasks_consume_open": "/api/tasks/consume_open",
                                "tasks_clear": "/api/tasks/clear",
                                "execute": "/api/execute",
                                "run_command": "/api/run_command",
                            },
                        },
                    )
                return
            if req_path == "/favicon.ico":
                try:
                    self.send_response(int(HTTPStatus.NO_CONTENT))
                    self.end_headers()
                except OSError:
                    return
                return
            if req_path == "/api/binding":
                if not self._verify_access():
                    return
                self._send_json(int(HTTPStatus.OK), bridge.get_binding_info())
                return
            if req_path == "/api/prompt":
                if not self._verify_access():
                    return
                query = parse_qs(urlsplit(self.path).query)
                pattern = (query.get("pattern") or ["default"])[0]
                try:
                    status_code, body = bridge.get_prompt_payload(pattern)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ プロンプト取得中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return
            if req_path != "/api/health":
                self._send_json(int(HTTPStatus.NOT_FOUND), {"ok": False, "message": "Not Found"})
                return
            if not self._verify_access():
                return
            binding_info = bridge.get_binding_info()
            self._send_json(
                int(HTTPStatus.OK),
                {
                    "ok": True,
                    "project_root": str(bridge.project_root),
                    "command_cwd": str(getattr(bridge, "command_cwd", bridge.project_root)),
                    "project_name": bridge.project_root.name,
                    "binding": binding_info,
                },
            )

        def do_POST(self) -> None:
            req_path = urlsplit(self.path).path
            if req_path == "/api/bind":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=False)
                if payload is None:
                    return
                raw_tab_id = payload.get("tab_id")
                if not isinstance(raw_tab_id, str):
                    self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "tab_id を文字列で指定してください"})
                    return
                raw_handoff_id = payload.get("handoff_id")
                handoff_id = raw_handoff_id.strip() if isinstance(raw_handoff_id, str) and raw_handoff_id.strip() else None
                raw_only_if_unbound = payload.get("only_if_unbound", False)
                if not isinstance(raw_only_if_unbound, bool):
                    self._send_json(
                        int(HTTPStatus.BAD_REQUEST),
                        {"ok": False, "message": "only_if_unbound を真偽値で指定してください"},
                    )
                    return
                only_if_unbound = raw_only_if_unbound
                newly_bound = True
                try:
                    if only_if_unbound:
                        newly_bound, info = bridge.bind_tab_if_unbound(
                            tab_id=raw_tab_id,
                            tab_title=str(payload.get("tab_title") or ""),
                            tab_url=str(payload.get("tab_url") or ""),
                            handoff_id=handoff_id,
                        )
                    elif handoff_id is not None:
                        info = bridge.bind_tab(
                            tab_id=raw_tab_id,
                            tab_title=str(payload.get("tab_title") or ""),
                            tab_url=str(payload.get("tab_url") or ""),
                            handoff_id=handoff_id,
                        )
                    else:
                        info = bridge.bind_tab(
                            tab_id=raw_tab_id,
                            tab_title=str(payload.get("tab_title") or ""),
                            tab_url=str(payload.get("tab_url") or ""),
                        )
                except ValueError as e:
                    self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": str(e)})
                    return
                if newly_bound:
                    label = sanitize_console_text(str(info.get("tab_title") or info.get("tab_url") or info.get("tab_id")))
                    print(f"\n🔗 ブラウザタブをVSCodeプロジェクトと紐づけました: {label}", flush=True)
                self._send_json(int(HTTPStatus.OK), info)
                return

            if req_path == "/api/unbind":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                if "tab_id" in payload:
                    raw_tab_id = payload.get("tab_id")
                    if not isinstance(raw_tab_id, str):
                        self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": "tab_id を文字列で指定してください"})
                        return
                    try:
                        changed, info = bridge.unbind_tab(tab_id=raw_tab_id)
                    except ValueError as e:
                        self._send_json(int(HTTPStatus.BAD_REQUEST), {"ok": False, "message": str(e)})
                        return
                else:
                    changed, info = bridge.unbind_tab(tab_id=None)
                if changed:
                    print("\n🔓 ブラウザタブの紐づけを解除しました。", flush=True)
                self._send_json(int(HTTPStatus.OK), info)
                return

            if req_path == "/api/handoff":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=False, max_bytes=max_handoff_payload_bytes)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_handoff_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ パッチ引き継ぎ処理中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/handoff/claim":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                status_code, body = bridge.handle_handoff_claim(payload)
                self._send_json(status_code, body)
                return

            if req_path == "/api/handoff/ack":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                status_code, body = bridge.handle_handoff_ack(payload)
                self._send_json(status_code, body)
                return

            if req_path == "/api/handoff/max_tabs":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=False)
                if payload is None:
                    return
                try:
                    status_code, body = bridge.handle_handoff_max_tabs_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 自動新タブ上限更新中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/trigger_run":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_trigger_run_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 自動Run送信処理中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/summary_prompt":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_summary_prompt_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 要約・プロンプト取得中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/bloat/targets":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_bloat_targets_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 自動分割対象取得中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/split/prompt":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=False)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_split_prompt_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 自動分割プロンプト取得中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/split/handoff":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=False)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_split_handoff_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 自動分割引き継ぎ処理中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/organize/targets":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_organize_targets_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 階層整理対象取得中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/organize/prompt":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=False)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_organize_prompt_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 階層整理プロンプト取得中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/organize/handoff":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=False)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_organize_handoff_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 階層整理引き継ぎ処理中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/config/prompt":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_config_prompt_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 設定推論プロンプト取得中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/config/handoff":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_config_handoff_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 設定推論引き継ぎ処理中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/tasks/advance":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_tasks_advance_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 次タスク進行処理中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/tasks/consume_open":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_tasks_consume_open_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ 保留タスク引き継ぎ取得中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/tasks/clear":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=True)
                if payload is None:
                    return
                try:
                    status_code, body = bridge._dispatch_tasks_clear_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ タスクキュー破棄中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path == "/api/run_command":
                if not self._verify_access():
                    return
                payload = self._read_json_body(allow_empty=False)
                if payload is None:
                    return
                try:
                    # 外部サブプロセス待機でメインスレッドキューを占有しないようHTTPスレッドで直接実行
                    status_code, body = bridge.handle_run_command_payload(payload)
                    self._send_json(status_code, body)
                except Exception as e:
                    print(f"✖ コマンド実行中にエラーが発生しました: {e}", file=sys.stderr, flush=True)
                    self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})
                return

            if req_path != "/api/execute":
                self._send_json(int(HTTPStatus.NOT_FOUND), {"ok": False, "message": "Not Found"})
                return
            if not self._verify_access():
                return

            payload = self._read_json_body(allow_empty=False)
            if payload is None:
                return

            try:
                status_code, result = bridge._dispatch_execute_payload(payload)
                self._send_json(status_code, result.to_dict())
            except Exception as e:
                print(f"✖ ブリッジ実行中に予期しないエラーが発生しました: {e}", file=sys.stderr, flush=True)
                self._send_json(int(HTTPStatus.INTERNAL_SERVER_ERROR), {"ok": False, "message": str(e)})

    return _BridgeRequestHandler
