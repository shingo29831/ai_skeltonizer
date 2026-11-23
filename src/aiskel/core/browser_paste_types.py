"""Module: @role: ブラウザ自動貼り付け処理の結果モデル・エラー種別・ログイン画面判定ロジックを定義する。"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union


class PasteErrorKind(str, Enum):
    NONE = "none"
    TRANSIENT = "transient"
    WINDOW_LOST = "window_lost"
    LOGIN_REQUIRED = "login_required"
    USER_ABORTED = "user_aborted"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


_BROWSER_SUFFIX_PATTERN = (
    r"(?:\s*(?:-|—|\|)\s*(?:google\s+chrome|chromium|microsoft\s+edge|"
    r"mozilla\s+firefox|firefox|safari|brave|opera|vivaldi))*"
)

# Python reとmacOS JXAのRegExp双方で同一解釈される構文のみを使用し、会話スレッド名の誤検知を防ぐ
LOGIN_TITLE_REGEX_PATTERN: str = "|".join(
    (
        (
            r"^(?:sign\s*in|log\s*in|signin|login|ログイン|サインイン)"
            r"(?:\s*[-|—:]\s*(?:google\s*accounts?|google\s*アカウント|google\s*ai\s*studio|"
            r"aistudio|gemini|chatgpt|openai|claude|anthropic)"
            r"|\s+to\s+(?:chatgpt|openai|claude|google(?:\s+ai\s+studio)?))"
            + _BROWSER_SUFFIX_PATTERN
            + r"$"
        ),
        r"^(?:google\s*accounts?|google\s*アカウント)" + _BROWSER_SUFFIX_PATTERN + r"$",
        (
            r"^(?:https?://)?(?:accounts\.google\.com|auth\.openai\.com)"
            r"(?:[/?#][^\s]*)?" + _BROWSER_SUFFIX_PATTERN + r"$"
        ),
        (
            r"^(?:2-?step\s*verification|2段階認証|verify\s+it['’]?s\s+you|"
            r"本人確認(?:\s*を行ってください)?)"
            r"(?:\s*[-|—:]\s*(?:google\s*accounts?|google\s*アカウント|openai|chatgpt|claude))?"
            + _BROWSER_SUFFIX_PATTERN
            + r"$"
        ),
        (
            r"^(?:welcome\s+back|continue\s+with\s+google)\s*[-|—:]\s*"
            r"(?:chatgpt|openai|claude|google\s*ai\s*studio)"
            + _BROWSER_SUFFIX_PATTERN
            + r"$"
        ),
    )
)
_LOGIN_TITLE_RE = re.compile(LOGIN_TITLE_REGEX_PATTERN, re.IGNORECASE)


def is_login_or_auth_title(title: str) -> bool:
    """ウィンドウタイトルが認証・ログイン画面を示すか判定する。"""
    norm = (title or "").strip()
    if not norm:
        return False
    return bool(_LOGIN_TITLE_RE.search(norm))


@dataclass(frozen=True)
class PasteResult:
    """ブラウザ自動貼り付け処理の成否とスキップ・失敗理由を保持する。"""

    success: bool
    reason: str = ""
    retryable: bool = False
    error_kind: PasteErrorKind = PasteErrorKind.NONE
    target_window: Optional[Union[int, str]] = None

    def __bool__(self) -> bool:
        return self.success


class PasteOutcome(tuple):
    """既存2要素タプル (bool, str) と同値性・ハッシュ契約を保ちつつ構造化エラー情報を運ぶ戻り値型。"""

    def __new__(
        cls,
        success: bool,
        reason: str = "",
        retryable: bool = False,
        error_kind: PasteErrorKind = PasteErrorKind.NONE,
        target_window: Optional[Union[int, str]] = None,
    ) -> "PasteOutcome":
        return super().__new__(cls, (success, reason))

    def __init__(
        self,
        success: bool,
        reason: str = "",
        retryable: bool = False,
        error_kind: PasteErrorKind = PasteErrorKind.NONE,
        target_window: Optional[Union[int, str]] = None,
    ) -> None:
        self.success = success
        self.reason = reason
        self.retryable = retryable
        self.error_kind = error_kind
        self.target_window = target_window

    def __bool__(self) -> bool:
        return self.success

    def to_result(self) -> PasteResult:
        return PasteResult(
            success=self.success,
            reason=self.reason,
            retryable=self.retryable,
            error_kind=self.error_kind,
            target_window=self.target_window,
        )

    def __repr__(self) -> str:
        return (
            f"PasteOutcome(success={self.success!r}, reason={self.reason!r}, "
            f"retryable={self.retryable!r}, error_kind={self.error_kind!r}, "
            f"target_window={self.target_window!r})"
        )
