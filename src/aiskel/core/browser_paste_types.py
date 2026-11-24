"""Module: @role: ブラウザ自動貼り付け処理の結果モデル・エラー種別・ログイン画面判定ロジックを定義する。"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, Union


class PasteErrorKind(str, Enum):
    NONE = "none"
    TRANSIENT = "transient"
    WINDOW_LOST = "window_lost"
    LOGIN_REQUIRED = "login_required"
    USER_ABORTED = "user_aborted"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


_BROWSER_NAMES_PATTERN = (
    r"(?:google\s+chrome|chromium|microsoft\s+edge|"
    r"mozilla\s+firefox|firefox|safari|brave|opera|vivaldi)"
    r"(?:\s+(?:beta|dev|canary|nightly|esr))?"
    r"(?:\s*(?:\([^()\r\n]{1,40}\)|private\s+browsing|プライベート\s*ブラウジング))?"
)
_PROFILE_OR_MODE_PATTERN = (
    r"(?:personal|個人|work|職場|仕事|school|学校|guest|ゲスト|inprivate|incognito|シークレット|"
    r"private(?:\s+browsing)?|プライベート(?:\s*ブラウジング)?|profile\s*\d+|プロファイル\s*\d+)"
)
_BROWSER_SUFFIX_PATTERN = (
    r"(?:\s*(?:-|—|\|)\s*(?:" + _PROFILE_OR_MODE_PATTERN + r"\s*(?:-|—|\|)\s*)?"
    + _BROWSER_NAMES_PATTERN
    + r"(?:\s*(?:-|—|\|)\s*" + _PROFILE_OR_MODE_PATTERN + r")?)*"
)
BROWSER_TITLE_SUFFIX_REGEX_PATTERN: str = (
    r"\s*(?:-|—|\|)\s*(?:" + _PROFILE_OR_MODE_PATTERN + r"\s*(?:-|—|\|)\s*)?"
    + _BROWSER_NAMES_PATTERN
    + r"(?:\s*(?:-|—|\|)\s*" + _PROFILE_OR_MODE_PATTERN + r")?\s*$"
)
BROWSER_STANDALONE_PROFILE_REGEX_PATTERN: str = r"^" + _PROFILE_OR_MODE_PATTERN + r"$"
BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN: str = (
    r"\s*(?:-|—|\|)\s*" + _BROWSER_NAMES_PATTERN + r"\s*(?:-|—|\|)\s*[^|—\r\n-]{1,40}\s*$"
)
BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN: str = r"\s*(?:-|—|\|)\s*[^|—\r\n-]{1,40}\s*$"
SERVICE_TITLE_KEYWORDS_REGEX_PATTERN: str = (
    r"(?:google\s+ai\s+studio|aistudio|gemini|chatgpt|openai|claude|anthropic)"
)
_COMMON_FILE_EXTENSIONS_PATTERN = (
    r"(?:py|pyc|pyo|js|mjs|cjs|ts|tsx|jsx|json|jsonc|yaml|yml|toml|md|markdown|txt|"
    r"html|htm|xhtml|css|scss|sass|less|xml|csv|tsv|log|sh|bash|zsh|ps1|bat|cmd|rb|php|"
    r"java|kt|kts|swift|dart|cpp|hpp|cc|cxx|c|h|cs|rs|go|sql|env|ini|cfg|conf|lock|"
    r"gz|bz2|xz|zip|rar|7z|tar|png|jpg|jpeg|gif|svg|webp|ico|pdf|doc|docx|xls|xlsx|"
    r"ppt|pptx|exe|dll|so|dylib|bin|wasm|map|vue|svelte|astro|proto|graphql|gql)"
)

# ページタイトル確定前のURL/ドメイン表示や新規タブ・読込中表示を検出し、ファイル名やバージョン番号の誤爆を防ぐ
LOADING_TITLE_REGEX_PATTERN: str = "|".join(
    (
        r"^$",
        (
            r"^(?:" + _PROFILE_OR_MODE_PATTERN + r"\s*(?:-|—|\|)\s*)?"
            + _BROWSER_NAMES_PATTERN
            + r"(?:\s*(?:-|—|\|)\s*" + _PROFILE_OR_MODE_PATTERN + r")?$"
        ),
        r"^(?:new\s*tab|新しいタブ|untitled|無題|about:blank)$",
        r"^(?:loading|読み込み中|connecting|接続中|just\s+a\s+moment|しばらくお待ちください)(?:\.{3}|…)?$",
        r"^(?:about|chrome|edge|brave|opera|vivaldi):[^\s]*$",
        r"^https?://[^\s]+$",
        r"^localhost(?::\d+)?(?:[/?#][^\s]*)?$",
        r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:[/?#][^\s]*)?$",
        (
            r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
            r"(?!" + _COMMON_FILE_EXTENSIONS_PATTERN + r"(?![a-z0-9-]))"
            r"(?:[a-z]{2,24}|xn--[a-z0-9-]{2,})(?::\d+)?(?:[/?#][^\s]*)?$"
        ),
    )
)
_BROWSER_TITLE_SUFFIX_RE = re.compile(BROWSER_TITLE_SUFFIX_REGEX_PATTERN, re.IGNORECASE)
_STANDALONE_PROFILE_RE = re.compile(BROWSER_STANDALONE_PROFILE_REGEX_PATTERN, re.IGNORECASE)
_TRAILING_CUSTOM_PROFILE_SUFFIX_RE = re.compile(
    BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN, re.IGNORECASE
)
_CUSTOM_PROFILE_SUFFIX_RE = re.compile(BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN)
_SERVICE_TITLE_KEYWORDS_RE = re.compile(SERVICE_TITLE_KEYWORDS_REGEX_PATTERN, re.IGNORECASE)
_LOADING_TITLE_RE = re.compile(LOADING_TITLE_REGEX_PATTERN, re.IGNORECASE)

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


def _strip_browser_title_suffix(title: str) -> Tuple[str, bool]:
    stripped = (title or "").strip()
    stripped_browser = False
    while stripped:
        updated = _BROWSER_TITLE_SUFFIX_RE.sub("", stripped).strip()
        if updated == stripped:
            break
        stripped = updated
        stripped_browser = True
    return stripped, stripped_browser


def is_loading_or_unready_title(title: str) -> bool:
    """ウィンドウタイトルがサイト読込前（空・新規タブ・プロファイルのみ・URL表示・読込中）か判定する。"""
    core, stripped_browser = _strip_browser_title_suffix(title)
    if _LOADING_TITLE_RE.search(core):
        return True
    if stripped_browser:
        if _STANDALONE_PROFILE_RE.search(core):
            return True
        # ブラウザ名除去後に任意プロファイル名1語のみが残るURL/未読込タイトルも救済する
        match = _CUSTOM_PROFILE_SUFFIX_RE.search(core)
        if match:
            tail = match.group(0)
            if not _SERVICE_TITLE_KEYWORDS_RE.search(tail):
                without_profile = core[: match.start()].strip()
                if _LOADING_TITLE_RE.search(without_profile):
                    return True
        return False

    # 末尾に「- ブラウザ名 - 任意プロファイル名」が付く形式のURL/未読込タイトルを救済する
    trailing_match = _TRAILING_CUSTOM_PROFILE_SUFFIX_RE.search(core)
    if trailing_match:
        tail = trailing_match.group(0)
        if not _SERVICE_TITLE_KEYWORDS_RE.search(tail):
            without_trailing = core[: trailing_match.start()].strip()
            if _LOADING_TITLE_RE.search(without_trailing) or _STANDALONE_PROFILE_RE.search(without_trailing):
                return True
    return False


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
