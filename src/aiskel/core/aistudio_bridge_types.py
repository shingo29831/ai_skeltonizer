"""Module: @role: ローカルHTTPブリッジで共有する定数・実行結果モデル・ジョブ定義・Origin検証および入出力ヘルパーを提供する。"""
import io
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from typing import Any, Dict, List, Optional, TextIO, Tuple
from urllib.parse import urlsplit

MAX_PAYLOAD_BYTES = 65536
MAX_DEDUP_HISTORY = 100
EXECUTION_WAIT_TIMEOUT_SEC = 120.0
SOCKET_TIMEOUT_SEC = 10.0
MAX_ERROR_DETAIL_CHARS = 240
MAX_COMMAND_KIND_CHARS = 64
MAX_RUNNING_COMMAND_CHARS = 240
MAX_PATCH_HANDOFF_MAX_TABS = 999
ALLOWED_EXTENSION_SCHEMES = frozenset({"chrome-extension", "moz-extension", "edge-extension"})

class BridgeCommandKind(str, Enum):
    """ブリッジ上で実行されるコマンド種別を表す列挙型。"""

    EXECUTE = "execute"
    RUN_COMMAND = "run_command"
    AUTO_APPLY = "auto_apply"
    AUTO_CHECK = "auto_check"

@dataclass(frozen=True)
class RunningCommandInfo:
    """現在実行中のブリッジコマンド情報を保持する。"""

    execution_id: str
    kind: str
    command: str
    tab_id: str = ""
    started_at: str = ""
    started_at_ms: int = 0
    started_monotonic: Optional[float] = None

    def to_dict(self, now_monotonic: Optional[float] = None) -> Dict[str, object]:
        if self.started_monotonic is None:
            elapsed_ms = 0
        else:
            current_mono = time.monotonic() if now_monotonic is None else now_monotonic
            elapsed_ms = max(0, int((current_mono - self.started_monotonic) * 1000))
        return {
            "execution_id": self.execution_id,
            "kind": self.kind,
            "command": self.command,
            "tab_id": self.tab_id,
            "started_at": self.started_at,
            "started_at_ms": self.started_at_ms,
            "elapsed_ms": elapsed_ms,
        }

_DICT_RESPONSE_ACTIONS = frozenset(
    {
        "handoff",
        "trigger_run",
        "summary_prompt",
        "bloat_targets",
        "split_prompt",
        "split_handoff",
        "organize_targets",
        "organize_prompt",
        "organize_handoff",
        "config_prompt",
        "config_handoff",
        "tasks_advance",
        "tasks_consume_open",
        "tasks_clear",
        "tasks_no_patch",
        "run_command",
    }
)

@dataclass(frozen=True)
class BridgeExecutionResult:
    """ブリッジ経由コマンド実行の結果を保持する。"""

    ok: bool
    exit_code: int
    command: str
    argv: Tuple[str, ...]
    message: str
    skipped: bool = False
    pasted: bool = False
    auto_run: bool = False
    auto_run_allowed: bool = False
    paste_reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "command": self.command,
            "message": self.message,
            "skipped": self.skipped,
            "pasted": self.pasted,
            "auto_run": self.auto_run,
            "auto_run_allowed": self.auto_run_allowed,
            "paste_reason": self.paste_reason,
        }

@dataclass
class ExecutionJob:
    """メインスレッド上で排他実行されるブリッジ処理ジョブを保持する。"""

    payload: Dict[str, object]
    action: str = "execute"
    done_event: threading.Event = field(default_factory=threading.Event)
    status_code: int = int(HTTPStatus.INTERNAL_SERVER_ERROR)
    result: Any = None
    error: Optional[BaseException] = None

class TeeTextIO:
    """元のstderrへリアルタイム出力しつつ、ブラウザ通知用にエラー内容を同時捕捉するストリーム。"""

    def __init__(self, wrapped: TextIO) -> None:
        self._wrapped = wrapped
        self._buffer = io.StringIO()

    def write(self, text: str) -> int:
        self._buffer.write(text)
        return self._wrapped.write(text)

    def flush(self) -> None:
        self._wrapped.flush()

    def isatty(self) -> bool:
        return getattr(self._wrapped, "isatty", lambda: False)()

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)

    def getvalue(self) -> str:
        return self._buffer.getvalue()

def normalize_system_exit_code(exc: SystemExit) -> int:
    """SystemExit例外の終了コードを整数へ正規化する。"""
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return int(exc.code)
    return 1

def extract_error_detail(stderr_text: str) -> str:
    """標準エラー出力から通知用の要約エラー文字列を抽出する。"""
    lines: List[str] = []
    for raw in stderr_text.splitlines():
        cleaned = raw.strip()
        if not cleaned or cleaned.startswith("📋"):
            continue
        lines.append(cleaned)
    detail = " / ".join(lines)
    if len(detail) > MAX_ERROR_DETAIL_CHARS:
        return detail[: MAX_ERROR_DETAIL_CHARS - 3] + "..."
    return detail

def is_allowed_origin(origin: Optional[str], allow_missing: bool = False) -> bool:
    """OriginヘッダがGoogle AI Studioまたはブラウザ拡張機能に厳密一致するか判定する。"""
    normalized = (origin or "").strip()
    # Userscript等のサンドボックス環境から送られるOrigin: nullはトークン認証済み時のみ許可
    if not normalized or normalized.lower() == "null":
        return allow_missing

    parsed = urlsplit(normalized)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False

    scheme = parsed.scheme.lower()
    if scheme == "https":
        if parsed.username is not None or parsed.password is not None:
            return False
        return (parsed.hostname or "").lower() == "aistudio.google.com" and parsed.port in (None, 443)

    if scheme in ALLOWED_EXTENSION_SCHEMES:
        return bool(parsed.netloc)

    return False

def make_bridge_job_result(action: str, ok: bool, message: str, exit_code: int = 1) -> Any:
    """アクション種別に応じたジョブエラー・終了時応答オブジェクトを生成する。"""
    # dict応答APIはJSON直列化するため、actionに応じて戻り値型を厳密に揃える
    if action in _DICT_RESPONSE_ACTIONS:
        return {"ok": ok, "message": message}
    return BridgeExecutionResult(
        ok=ok,
        exit_code=exit_code,
        command="",
        argv=(),
        message=message,
    )

def validate_optional_tab_id(
    binding_manager: Any,
    payload: Dict[str, object],
    required: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """ペイロード内の任意または必須のtab_idを検証し、(正規化済みtab_id, エラー文言)を返す。"""
    if "tab_id" not in payload:
        if required:
            return None, "tab_id を指定してください"
        return "", None
    raw_tab_id = payload.get("tab_id")
    if not isinstance(raw_tab_id, str):
        return None, "tab_id を文字列で指定してください"
    try:
        return binding_manager.validate_tab_id(raw_tab_id), None
    except ValueError as e:
        return None, str(e)

def resolve_target_title_keywords(binding_manager: Any, payload: Dict[str, object]) -> Tuple[str, ...]:
    """ペイロードまたは紐づけ済みタブ情報からフォーカス対象のウィンドウタイトルキーワードを解決する。"""
    raw_title = payload.get("tab_title")
    title_str = " ".join(str(raw_title).split()) if isinstance(raw_title, str) else ""
    if not title_str:
        binding_title = binding_manager.get_info().get("tab_title")
        title_str = " ".join(str(binding_title).split()) if isinstance(binding_title, str) else ""
    normalized = title_str[:80].strip().lower()
    if len(normalized) >= 4:
        return (normalized,)
    return ("google ai studio", "aistudio")
