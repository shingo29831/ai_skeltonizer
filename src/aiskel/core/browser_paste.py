"""Module: @role: 最前面化されたデフォルトブラウザのプロンプト入力欄を検出し、フォーカスしてクリップボード内容を貼り付けるOS別ディスパッチャを提供する。"""
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple, Union

from .browser_paste_linux import paste_linux_browser_prompt
from .browser_paste_macos import paste_macos_browser_prompt
from .browser_paste_windows import paste_windows_browser_prompt

DEFAULT_PROMPT_LABELS: Tuple[str, ...] = (
    "Enter a prompt",
    "プロンプトを入力",
    "Start typing a prompt to see what our models can do",
    "Ask Gemini",
    "Message ChatGPT",
    "Reply to Claude",
)

DEFAULT_ALLOWED_TITLE_KEYWORDS: Tuple[str, ...] = (
    "google ai studio",
    "aistudio",
    "gemini",
    "chatgpt",
    "claude",
)


@dataclass(frozen=True)
class PasteResult:
    """ブラウザ自動貼り付け処理の成否とスキップ・失敗理由を保持する。"""

    success: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.success


def paste_to_browser_prompt(
    interactive_only: bool = False,
    prompt_labels: Optional[Sequence[str]] = None,
    target_window: Optional[Union[int, str]] = None,
    allowed_title_keywords: Optional[Sequence[str]] = None,
    allow_non_empty: bool = False,
) -> PasteResult:
    """ブラウザ上の対象プロンプト入力欄にフォーカスを当て、クリップボード内容を貼り付ける。"""
    # 非TTY（テスト実行やパイプ出力）およびCI環境でのキーボード送信を防ぐ
    if interactive_only and (not (sys.stdout is not None and sys.stdout.isatty()) or bool(os.environ.get("CI"))):
        return PasteResult(False, "非対話環境またはCI環境のためスキップしました")

    raw_labels = DEFAULT_PROMPT_LABELS if prompt_labels is None else prompt_labels
    labels = tuple(label.strip() for label in raw_labels if label and label.strip())
    if not labels:
        return PasteResult(False, "プロンプトラベルが指定されていません")

    raw_keywords = DEFAULT_ALLOWED_TITLE_KEYWORDS if allowed_title_keywords is None else allowed_title_keywords
    title_keywords = tuple(kw.strip().lower() for kw in raw_keywords if kw and kw.strip())

    system = platform.system()
    try:
        if system == "Windows":
            target_hwnd = target_window if isinstance(target_window, int) else None
            ok, reason = paste_windows_browser_prompt(
                labels,
                target_hwnd=target_hwnd,
                allowed_title_keywords=title_keywords,
                allow_non_empty=allow_non_empty,
            )
            return PasteResult(ok, reason)
        if system == "Darwin":
            bundle_id = target_window if isinstance(target_window, str) else None
            ok, reason = paste_macos_browser_prompt(
                labels,
                target_bundle_id=bundle_id,
                allowed_title_keywords=title_keywords,
                allow_non_empty=allow_non_empty,
            )
            return PasteResult(ok, reason)
        if system == "Linux":
            win_id = target_window if isinstance(target_window, str) else None
            ok, reason = paste_linux_browser_prompt(
                labels,
                target_window_id=win_id,
                allowed_title_keywords=title_keywords,
                allow_non_empty=allow_non_empty,
            )
            return PasteResult(ok, reason)
        return PasteResult(False, f"未対応のOS ({system}) です")
    except (OSError, subprocess.SubprocessError) as e:
        return PasteResult(False, f"ブラウザ操作エラー: {e}")
