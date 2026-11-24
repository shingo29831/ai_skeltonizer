"""Module: @role: 既存空チャットタブ再利用に関する共有定数・新規チャットURL判定・タブタイトル正規化ロジックを定義する。"""
import re
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from .browser_paste_types import _strip_browser_title_suffix

EMPTY_CHAT_TITLES_BY_DOMAIN: Dict[str, Tuple[str, ...]] = {
    "aistudio.google.com": (
        "google ai studio",
        "aistudio",
        "untitled prompt | google ai studio",
        "untitled prompt - google ai studio",
        "new chat | google ai studio",
        "new chat - google ai studio",
        "無題のプロンプト | google ai studio",
        "無題のプロンプト - google ai studio",
        "新しいチャット | google ai studio",
        "新しいチャット - google ai studio",
    ),
    "gemini.google.com": (
        "google gemini",
        "gemini",
    ),
    "chatgpt.com": ("chatgpt",),
    "chat.openai.com": ("chatgpt",),
    "claude.ai": ("claude",),
}

NEW_CHAT_PATHS_BY_DOMAIN: Dict[str, Tuple[str, ...]] = {
    "aistudio.google.com": ("/", "/app", "/prompts/new_chat"),
    "gemini.google.com": ("/", "/app"),
    "chatgpt.com": ("/",),
    "chat.openai.com": ("/",),
    "claude.ai": ("/", "/new"),
}

NEW_CHAT_BUTTON_NAMES: Tuple[str, ...] = (
    "new chat",
    "新しいチャット",
    "チャットを新規作成",
)

CHAT_TURN_ACTION_BUTTON_KEYWORDS: Tuple[str, ...] = (
    "good response",
    "bad response",
    "良い回答",
    "悪い回答",
    "thumbs up",
    "thumbs down",
    "rerun",
    "regenerate",
    "再生成",
    "stop generating",
    "stop response",
    "生成を停止",
    "回答を停止",
    "show drafts",
    "ドラフトを表示",
    "edit this message",
    "メッセージを編集",
    "read aloud",
    "読み上げる",
    "copy response",
    "回答をコピー",
)

_TAB_STATUS_SUFFIX_RE = re.compile(
    r"\s+-\s+(?:audio\s+playing|muted|inactive|pinned|memory\s+usage:.*|"
    r"音声を再生中|ミュート中|非アクティブ|固定済み|メモリ使用量:.*)$",
    re.IGNORECASE,
)


def _match_supported_domain(hostname: str) -> Optional[str]:
    norm_host = (hostname or "").lower().rstrip(".")
    if not norm_host:
        return None
    for domain in EMPTY_CHAT_TITLES_BY_DOMAIN:
        if norm_host == domain or norm_host.endswith(f".{domain}"):
            return domain
    return None


def is_aistudio_target_url(target_url: str) -> bool:
    """対象URLがGoogle AI Studioであるか判定する。"""
    hostname = (urlsplit((target_url or "").strip()).hostname or "").lower().rstrip(".")
    return hostname == "aistudio.google.com" or hostname.endswith(".aistudio.google.com")


def is_new_chat_target_url(target_url: str) -> bool:
    """対象URLが特定スレッドやクエリを含まない新規・空チャット起動URLであるか判定する。"""
    parsed = urlsplit((target_url or "").strip())
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    domain = _match_supported_domain(parsed.hostname or "")
    if not domain or parsed.query:
        return False
    raw_path = parsed.path or "/"
    norm_path = "/" if raw_path == "/" else raw_path.rstrip("/")
    allowed_paths = NEW_CHAT_PATHS_BY_DOMAIN.get(domain, ("/",))
    return norm_path in allowed_paths


def get_empty_chat_titles_for_url(target_url: str) -> Tuple[str, ...]:
    """対象URLのドメインに対応する空チャットタブタイトル候補一覧を返す。"""
    if not is_new_chat_target_url(target_url):
        return ()
    parsed = urlsplit((target_url or "").strip())
    domain = _match_supported_domain(parsed.hostname or "")
    return EMPTY_CHAT_TITLES_BY_DOMAIN.get(domain, ()) if domain else ()


def normalize_tab_title_for_match(raw_title: str) -> str:
    """ブラウザ名およびタブ状態サフィックスを除去してタブタイトルを正規化する。"""
    stripped, _ = _strip_browser_title_suffix(raw_title or "")
    cleaned = _TAB_STATUS_SUFFIX_RE.sub("", stripped).strip()
    return cleaned.lower()


def is_empty_chat_tab_title(raw_title: str, target_url: str) -> bool:
    """タブタイトルが対象サイトの未会話（空チャット）タブに一致するか判定する。"""
    expected_titles = get_empty_chat_titles_for_url(target_url)
    if not expected_titles:
        return False
    norm = normalize_tab_title_for_match(raw_title)
    return bool(norm and norm in expected_titles)
