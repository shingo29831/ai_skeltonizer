"""Module: @role: 既存空チャットタブ再利用に関する共有定数・新規チャットURL判定・タブタイトル正規化ロジックを定義する。"""
import re
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from .browser_paste_types import (
    _strip_browser_title_suffix,
    is_loading_or_unready_title,
    is_login_or_auth_title,
)

EMPTY_CHAT_TITLES_BY_DOMAIN: Dict[str, Tuple[str, ...]] = {
    "aistudio.google.com": (
        "google ai studio",
        "aistudio",
        "untitled prompt | google ai studio",
        "untitled prompt - google ai studio",
        "untitled prompt · google ai studio",
        "google ai studio - untitled prompt",
        "google ai studio | untitled prompt",
        "new chat | google ai studio",
        "new chat - google ai studio",
        "new prompt | google ai studio",
        "new prompt - google ai studio",
        "無題のプロンプト | google ai studio",
        "無題のプロンプト - google ai studio",
        "google ai studio - 無題のプロンプト",
        "google ai studio | 無題のプロンプト",
        "新しいチャット | google ai studio",
        "新しいチャット - google ai studio",
        "新規プロンプト | google ai studio",
        "新規プロンプト - google ai studio",
    ),
    "gemini.google.com": (
        "google gemini",
        "gemini",
        "new chat | gemini",
        "new chat - gemini",
        "gemini - new chat",
        "新しいチャット | gemini",
        "新しいチャット - gemini",
        "gemini - 新しいチャット",
    ),
    "chatgpt.com": (
        "chatgpt",
        "new chat - chatgpt",
        "chatgpt - new chat",
        "新しいチャット - chatgpt",
        "chatgpt - 新しいチャット",
    ),
    "chat.openai.com": (
        "chatgpt",
        "new chat - chatgpt",
        "chatgpt - new chat",
        "新しいチャット - chatgpt",
        "chatgpt - 新しいチャット",
    ),
    "claude.ai": (
        "claude",
        "claude.ai",
        "new chat - claude",
        "claude - new chat",
        "新しいチャット - claude",
        "claude - 新しいチャット",
    ),
}

_SERVICE_NAMES_BY_DOMAIN: Dict[str, Tuple[str, ...]] = {
    "aistudio.google.com": ("google ai studio", "aistudio", "ai studio"),
    "gemini.google.com": ("google gemini", "gemini"),
    "chatgpt.com": ("chatgpt", "openai"),
    "chat.openai.com": ("chatgpt", "openai"),
    "claude.ai": ("claude", "claude.ai"),
}

_EMPTY_CHAT_CORE_PHRASES_BY_DOMAIN: Dict[str, Tuple[str, ...]] = {
    "aistudio.google.com": (
        "untitled prompt",
        "無題のプロンプト",
        "new chat",
        "新しいチャット",
        "new prompt",
        "新規プロンプト",
    ),
    "gemini.google.com": ("new chat", "新しいチャット", "チャットを新規作成"),
    "chatgpt.com": ("new chat", "新しいチャット"),
    "chat.openai.com": ("new chat", "新しいチャット"),
    "claude.ai": ("new chat", "新しいチャット"),
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

TAB_STATUS_SUFFIX_REGEX_PATTERN: str = (
    r"\s+-\s+(?:audio\s+playing|muted|inactive(?:\s+tab)?|sleeping(?:\s+tab)?|discarded|"
    r"pinned|memory\s+usage:\s*[0-9.,]+\s*(?:b|bytes?|kb|mb|gb)|"
    r"音声を再生中|ミュート中|非アクティブ(?:\s*な?タブ)?|休止状態|スリープ(?:状態|中)|"
    r"固定済み|ピン留め済み|メモリ使用量:\s*[0-9.,]+\s*(?:バイト|kb|mb|gb))$"
)
_TAB_STATUS_SUFFIX_RE = re.compile(TAB_STATUS_SUFFIX_REGEX_PATTERN, re.IGNORECASE)
_USER_ACCOUNT_PREFIX_RE = re.compile(r"^/u/\d+(?=/|$)")
_TITLE_SEPARATOR_SPLIT_RE = re.compile(r"\s+(?:-|—|\||·)\s+|\s*(?:\||—|·)\s*|:\s+")


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
    if domain in ("aistudio.google.com", "gemini.google.com"):
        norm_path = _USER_ACCOUNT_PREFIX_RE.sub("", norm_path) or "/"
    allowed_paths = NEW_CHAT_PATHS_BY_DOMAIN.get(domain, ("/",))
    return norm_path in allowed_paths


def get_empty_chat_titles_for_url(target_url: str) -> Tuple[str, ...]:
    """対象URLのドメインに対応する空チャットタブタイトル候補一覧を返す。"""
    if not is_new_chat_target_url(target_url):
        return ()
    parsed = urlsplit((target_url or "").strip())
    domain = _match_supported_domain(parsed.hostname or "")
    return EMPTY_CHAT_TITLES_BY_DOMAIN.get(domain, ()) if domain else ()


def get_empty_chat_service_and_phrases(target_url: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """対象URLのドメインに対応するサービス名群と空チャット表現群を返す。"""
    if not is_new_chat_target_url(target_url):
        return (), ()
    parsed = urlsplit((target_url or "").strip())
    domain = _match_supported_domain(parsed.hostname or "")
    if not domain:
        return (), ()
    return _SERVICE_NAMES_BY_DOMAIN.get(domain, ()), _EMPTY_CHAT_CORE_PHRASES_BY_DOMAIN.get(domain, ())


def _extract_first_nonempty_line(raw_title: str) -> str:
    return next((ln.strip() for ln in (raw_title or "").splitlines() if ln.strip()), "")


def normalize_tab_title_for_match(raw_title: str) -> str:
    """ブラウザ名およびタブ状態サフィックスを除去してタブタイトルを正規化する。"""
    cleaned = _extract_first_nonempty_line(raw_title)
    # 固定済み・メモリ使用量・ブラウザ名など複数サフィックスが連続する場合も終端まで剥離する
    while cleaned:
        stripped, _ = _strip_browser_title_suffix(cleaned)
        next_val = _TAB_STATUS_SUFFIX_RE.sub("", stripped).strip()
        if next_val == cleaned:
            break
        cleaned = next_val
    return cleaned.lower()


def _strip_suffixes_guarding_blocked_titles(raw_line: str) -> Optional[str]:
    cleaned = raw_line.strip()
    while cleaned:
        # ブラウザ名と状態サフィックスの混在順によるすり抜けを防ぐため剥離各段階で検査する
        if is_login_or_auth_title(cleaned) or is_loading_or_unready_title(cleaned):
            return None
        stripped_browser, _ = _strip_browser_title_suffix(cleaned)
        if stripped_browser != cleaned:
            if (
                not stripped_browser
                or is_login_or_auth_title(stripped_browser)
                or is_loading_or_unready_title(stripped_browser)
            ):
                return None
        next_val = _TAB_STATUS_SUFFIX_RE.sub("", stripped_browser).strip()
        if next_val == cleaned:
            break
        cleaned = next_val
    if not cleaned or is_login_or_auth_title(cleaned) or is_loading_or_unready_title(cleaned):
        return None
    return cleaned.lower()


def is_empty_chat_tab_title(raw_title: str, target_url: str) -> bool:
    """タブタイトルが対象サイトの未会話（空チャット）タブに一致するか判定する。"""
    expected_titles = get_empty_chat_titles_for_url(target_url)
    if not expected_titles:
        return False
    first_line = _extract_first_nonempty_line(raw_title)
    if not first_line:
        return False
    norm = _strip_suffixes_guarding_blocked_titles(first_line)
    if not norm:
        return False
    if norm in expected_titles:
        return True

    service_names, empty_phrases = get_empty_chat_service_and_phrases(target_url)
    parts = [p.strip() for p in _TITLE_SEPARATOR_SPLIT_RE.split(norm) if p.strip()]
    if len(parts) == 1:
        return parts[0] in service_names
    if len(parts) == 2:
        return (
            (parts[0] in service_names and parts[1] in empty_phrases)
            or (parts[1] in service_names and parts[0] in empty_phrases)
        )
    return False
