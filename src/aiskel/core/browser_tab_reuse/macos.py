"""Module: @role: macOS環境においてJXA/Accessibility経由で起動済みブラウザの空チャットタブを検出し、選択・最前面化を行う。"""
import json
import subprocess
from typing import Optional, Sequence

from ..browser_focus.macos import _get_macos_default_browser_bundle_id, _resolve_running_macos_bundle_id
from ..browser_paste_types import (
    BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
    BROWSER_STANDALONE_PROFILE_REGEX_PATTERN,
    BROWSER_TITLE_SUFFIX_REGEX_PATTERN,
    BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
    LOADING_TITLE_REGEX_PATTERN,
    LOGIN_TITLE_REGEX_PATTERN,
    SERVICE_TITLE_KEYWORDS_REGEX_PATTERN,
)
from .types import (
    CHAT_TURN_ACTION_BUTTON_KEYWORDS,
    NEW_CHAT_BUTTON_NAMES,
    TAB_STATUS_SUFFIX_REGEX_PATTERN,
    get_empty_chat_service_and_phrases,
    get_empty_chat_titles_for_url,
)
from .macos_jxa import build_macos_find_and_activate_empty_tab_jxa

_MACOS_FIND_AND_ACTIVATE_EMPTY_TAB_JXA = build_macos_find_and_activate_empty_tab_jxa()

def focus_macos_empty_chat_tab(
    target_url: str,
    prompt_labels: Sequence[str],
    allowed_title_keywords: Sequence[str],
    is_aistudio: bool,
) -> Optional[str]:
    """起動済みmacOSブラウザから対象サイトの空チャットタブを探し、選択・最前面化してBundle IDを返す。"""
    empty_titles = get_empty_chat_titles_for_url(target_url)
    if not empty_titles:
        return None
    empty_service_names, empty_core_phrases = get_empty_chat_service_and_phrases(target_url)

    bundle_id = _get_macos_default_browser_bundle_id()
    resolved_id = _resolve_running_macos_bundle_id(bundle_id)
    if not resolved_id:
        return None

    payload = json.dumps(
        {
            "bundleId": resolved_id,
            "labels": list(prompt_labels),
            "titleKeywords": list(allowed_title_keywords),
            "emptyChatTitles": list(empty_titles),
            "emptyServiceNames": list(empty_service_names),
            "emptyCorePhrases": list(empty_core_phrases),
            "isAiStudio": bool(is_aistudio),
            "newChatButtonNames": list(NEW_CHAT_BUTTON_NAMES),
            "turnActionKeywords": list(CHAT_TURN_ACTION_BUTTON_KEYWORDS),
            "tabStatusSuffixRegex": TAB_STATUS_SUFFIX_REGEX_PATTERN,
            "loginRegex": LOGIN_TITLE_REGEX_PATTERN,
            "loadingRegex": LOADING_TITLE_REGEX_PATTERN,
            "browserSuffixRegex": BROWSER_TITLE_SUFFIX_REGEX_PATTERN,
            "standaloneProfileRegex": BROWSER_STANDALONE_PROFILE_REGEX_PATTERN,
            "trailingCustomProfileSuffixRegex": BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
            "customProfileSuffixRegex": BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
            "serviceTitleKeywordsRegex": SERVICE_TITLE_KEYWORDS_REGEX_PATTERN,
        }
    )

    try:
        # JXA側overallDeadline(7.0s)後のタブ選択・最小化/フォーカス復元を確実に完了させるため余裕を持たせる
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-", payload],
            input=_MACOS_FIND_AND_ACTIVATE_EMPTY_TAB_JXA,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode != 0:
            return None
        parsed = json.loads((res.stdout or "").strip() or "{}")
        if not parsed.get("found"):
            return None
        act = subprocess.run(["open", "-b", resolved_id], capture_output=True, text=True, timeout=3)
        return resolved_id if act.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
