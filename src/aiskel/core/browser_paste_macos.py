"""Module: @role: macOS環境においてJXA経由でブラウザ内のAXWebArea配下を走査し、プロンプト入力欄へのフォーカスとCmd+V貼り付けを実行する。"""
import json
import subprocess
from typing import Optional, Sequence

from .browser_focus.macos import _get_macos_default_browser_bundle_id, _resolve_running_macos_bundle_id
from .browser_paste_macos_jxa import build_macos_focus_and_paste_jxa, build_macos_trigger_run_jxa
from .browser_paste_types import (
    BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
    BROWSER_STANDALONE_PROFILE_REGEX_PATTERN,
    BROWSER_TITLE_SUFFIX_REGEX_PATTERN,
    BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
    LOADING_TITLE_REGEX_PATTERN,
    LOGIN_TITLE_REGEX_PATTERN,
    SERVICE_TITLE_KEYWORDS_REGEX_PATTERN,
    PasteErrorKind,
    PasteOutcome,
)

_MACOS_FOCUS_AND_PASTE_JXA = build_macos_focus_and_paste_jxa()


def _parse_macos_error_kind(raw_kind: object, ok: bool) -> PasteErrorKind:
    if ok:
        return PasteErrorKind.NONE
    try:
        return PasteErrorKind(str(raw_kind))
    except ValueError:
        return PasteErrorKind.FAILED


def paste_macos_browser_prompt(
    prompt_labels: Sequence[str],
    target_bundle_id: Optional[str] = None,
    allowed_title_keywords: Sequence[str] = (),
    allow_non_empty: bool = False,
    quick_probe: bool = False,
    verify_stability: bool = False,
) -> PasteOutcome:
    """最前面のmacOSデフォルトブラウザでプロンプト入力欄を探してフォーカスし、Cmd+Vを送信する。"""
    if not prompt_labels:
        return PasteOutcome(False, "プロンプトラベルが指定されていません", False, PasteErrorKind.FAILED)

    resolved_id = target_bundle_id
    if not resolved_id:
        bundle_id = _get_macos_default_browser_bundle_id()
        resolved_id = _resolve_running_macos_bundle_id(bundle_id)
    if not resolved_id:
        return PasteOutcome(False, "起動中のデフォルトブラウザが特定できませんでした", True, PasteErrorKind.TRANSIENT)

    payload = json.dumps(
        {
            "bundleId": resolved_id,
            "labels": list(prompt_labels),
            "titleKeywords": list(allowed_title_keywords),
            "loginRegex": LOGIN_TITLE_REGEX_PATTERN,
            "loadingRegex": LOADING_TITLE_REGEX_PATTERN,
            "browserSuffixRegex": BROWSER_TITLE_SUFFIX_REGEX_PATTERN,
            "standaloneProfileRegex": BROWSER_STANDALONE_PROFILE_REGEX_PATTERN,
            "trailingCustomProfileSuffixRegex": BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
            "customProfileSuffixRegex": BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
            "serviceTitleKeywordsRegex": SERVICE_TITLE_KEYWORDS_REGEX_PATTERN,
            "allowNonEmpty": allow_non_empty,
            "quickProbe": bool(quick_probe),
            "verifyStability": bool(verify_stability),
        }
    )

    try:
        # 安定確認待機＋AXツリー探索負荷によるTimeoutExpired誤爆を防ぐため8秒確保する
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-", payload],
            input=_MACOS_FOCUS_AND_PASTE_JXA,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if res.returncode != 0:
            err_msg = (res.stderr or "").strip()
            if "assistive" in err_msg.lower() or "-25211" in err_msg or "-1719" in err_msg:
                return PasteOutcome(False, "macOSのアクセシビリティ権限が許可されていません", False, PasteErrorKind.UNSUPPORTED)
            return PasteOutcome(False, f"osascript実行に失敗しました: {err_msg or f'code={res.returncode}'}", False, PasteErrorKind.FAILED)

        out = (res.stdout or "").strip()
        if not out:
            return PasteOutcome(False, "osascriptから応答がありませんでした", False, PasteErrorKind.FAILED)
        parsed = json.loads(out)
        ok = bool(parsed.get("ok"))
        reason = str(parsed.get("reason", ""))
        retryable = bool(parsed.get("retryable", False)) if not ok else False
        error_kind = _parse_macos_error_kind(parsed.get("errorKind"), ok)
        return PasteOutcome(ok, reason, retryable, error_kind, target_window=resolved_id)
    except subprocess.TimeoutExpired:
        return PasteOutcome(False, "ブラウザUI探索がタイムアウトしました", True, PasteErrorKind.TRANSIENT, target_window=resolved_id)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return PasteOutcome(False, f"macOSブラウザ操作エラー: {e}", False, PasteErrorKind.FAILED)


_MACOS_TRIGGER_RUN_JXA = build_macos_trigger_run_jxa()

def trigger_macos_browser_run(
    prompt_labels: Sequence[str],
    target_bundle_id: Optional[str] = None,
    allowed_title_keywords: Sequence[str] = (),
) -> PasteOutcome:
    """最前面のmacOSブラウザ対象ページでCmd+Enterを送信する。"""
    del prompt_labels
    resolved_id = target_bundle_id
    if not resolved_id:
        bundle_id = _get_macos_default_browser_bundle_id()
        resolved_id = _resolve_running_macos_bundle_id(bundle_id)
    if not resolved_id:
        return PasteOutcome(False, "起動中のデフォルトブラウザが特定できませんでした", False, PasteErrorKind.FAILED)

    payload = json.dumps(
        {
            "bundleId": resolved_id,
            "titleKeywords": list(allowed_title_keywords),
            "loginRegex": LOGIN_TITLE_REGEX_PATTERN,
            "loadingRegex": LOADING_TITLE_REGEX_PATTERN,
        }
    )
    try:
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-", payload],
            input=_MACOS_TRIGGER_RUN_JXA,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            err_msg = (res.stderr or "").strip()
            return PasteOutcome(False, f"osascript実行に失敗しました: {err_msg or f'code={res.returncode}'}", False, PasteErrorKind.FAILED)
        out = (res.stdout or "").strip()
        if not out:
            return PasteOutcome(False, "osascriptから応答がありませんでした", False, PasteErrorKind.FAILED)
        parsed = json.loads(out)
        ok = bool(parsed.get("ok"))
        reason = str(parsed.get("reason", ""))
        error_kind = _parse_macos_error_kind(parsed.get("errorKind"), ok)
        return PasteOutcome(ok, reason, False, error_kind, target_window=resolved_id)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return PasteOutcome(False, f"macOSブラウザ送信エラー: {e}", False, PasteErrorKind.FAILED)
