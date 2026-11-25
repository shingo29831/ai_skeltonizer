"""Module: @role: copyサブコマンド(c/cp)のコード抽出、クリップボード保存、ブラウザ貼り付け連携およびエラー処理を担当する。"""
import argparse
import io
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, TextIO, Tuple, Union

from .core.browser_focus import focus_default_browser_target
from .core.browser_paste import paste_to_browser_prompt
from .core.clipboard import set_clipboard_text
from .core.code_extractor import extract_and_format_snippets

PASTE_RETRY_DELAYS: Tuple[float, ...] = (0.15, 0.3, 0.5)
KNOWN_NON_COPY_SUBCOMMANDS = frozenset({
    "apply", "a", "revert", "r", "check", "chk", "verify", "commit", "cm", "ci",
    "move", "mv", "rename",
    "test", "t", "bloat", "b", "size", "docs", "d", "watch", "w", "listen", "l", "start", "s",
})

class TeeStderr:
    """元のstderrへリアルタイム出力しつつエラー内容をバッファリングするストリーム。"""

    def __init__(self, wrapped: Optional[TextIO]) -> None:
        self._wrapped = wrapped
        self._buffer = io.StringIO()

    def write(self, text: str) -> int:
        self._buffer.write(text)
        if self._wrapped is not None:
            return self._wrapped.write(text)
        return len(text)

    def flush(self) -> None:
        if self._wrapped is not None:
            self._wrapped.flush()

    def isatty(self) -> bool:
        return getattr(self._wrapped, "isatty", lambda: False)() if self._wrapped is not None else False

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)

    def getvalue(self) -> str:
        return self._buffer.getvalue()

@dataclass(frozen=True)
class CopyCommandResult:
    """aiskel c コマンドの構造化実行結果を保持する。"""

    exit_code: int
    pasted: bool = False
    paste_reason: str = ""
    copied_chars: int = 0
    error_message: str = ""

def focus_and_paste_clipboard(
    *,
    no_focus: bool,
    no_paste: bool,
    focus_browser: bool,
    allowed_title_keywords: Optional[Sequence[str]] = None,
    browser_target_focuser: Optional[Callable[..., Optional[Union[int, str]]]] = None,
    prompt_paster: Optional[Callable[..., Any]] = None,
    paste_retry_delays: Sequence[float] = PASTE_RETRY_DELAYS,
) -> Tuple[bool, str]:
    """クリップボード保存済みの内容をブラウザ入力欄へ貼り付け、(成功可否, 理由)を返す。"""
    if no_focus:
        return False, ""
    focuser = browser_target_focuser or focus_default_browser_target
    paster = prompt_paster or paste_to_browser_prompt
    target_window: Optional[Union[int, str]] = None
    can_attempt_paste = False
    if focus_browser:
        target_window = focuser(interactive_only=True)
        if target_window is not None:
            print("   🌐 起動済みのデフォルトブラウザを最前面に表示しました。")
            can_attempt_paste = True
        else:
            return False, "起動済みのデフォルトブラウザを検出できませんでした"
    else:
        # ブリッジ経由で対象タブが既に最前面のときは別ブラウザへのフォーカス奪取を防ぐ
        can_attempt_paste = True

    if not can_attempt_paste or no_paste:
        return False, ""

    paste_res = paster(
        interactive_only=True,
        target_window=target_window,
        allowed_title_keywords=allowed_title_keywords,
    )
    if getattr(paste_res, "target_window", None) is not None:
        target_window = paste_res.target_window
    # 一時的なUI遷移・フォーカス揺らぎによる貼り付け失敗を吸収する短時間リトライ
    for delay in paste_retry_delays:
        if paste_res.success or not getattr(paste_res, "retryable", False):
            break
        time.sleep(delay)
        if (
            focus_browser
            and getattr(paste_res, "error_kind", None)
            and str(getattr(paste_res.error_kind, "value", "")) == "window_lost"
        ):
            refocused = focuser(interactive_only=True)
            if refocused is not None:
                target_window = refocused
        paste_res = paster(
            interactive_only=True,
            target_window=target_window,
            allowed_title_keywords=allowed_title_keywords,
        )
        if getattr(paste_res, "target_window", None) is not None:
            target_window = paste_res.target_window

    if paste_res.success:
        print("   📝 プロンプト入力欄に貼り付けて自動入力しました。")
        return True, ""
    if paste_res.reason:
        print(f"   ℹ 自動貼り付けをスキップしました: {paste_res.reason}")
        return False, paste_res.reason
    return False, ""

def is_copy_invocation(raw_args: Sequence[str]) -> bool:
    """生のCLI引数群がcopyサブコマンドまたは--copyオプション指定かを判定する。"""
    if not raw_args:
        return False
    if any(tok in ("-h", "--help") for tok in raw_args):
        return False
    if raw_args[0] in ("copy", "c", "cp"):
        return True
    if raw_args[0] in KNOWN_NON_COPY_SUBCOMMANDS:
        return False
    return any(tok in ("-c", "--copy") for tok in raw_args)

def handle_copy_parse_failure(
    raw_args: Sequence[str],
    stderr_text: str,
    exit_code: int,
    *,
    focus_browser: bool,
    allowed_title_keywords: Optional[Sequence[str]],
    clipboard_writer: Optional[Callable[[str], None]] = None,
    paste_fn: Optional[Callable[..., Tuple[bool, str]]] = None,
) -> CopyCommandResult:
    """copy系コマンドの引数パース失敗時に診断レポートを保存・貼り付けして構造化結果を返す。"""
    writer = clipboard_writer or set_clipboard_text
    paster = paste_fn or focus_and_paste_clipboard
    detail = " ".join(line.strip() for line in stderr_text.splitlines() if line.strip()) or f"終了コード {exit_code}"
    cmd_text = "aiskel " + " ".join(raw_args)
    error_report = (
        f"[aiskel c コマンド引数エラー]\n"
        f"コマンド: {cmd_text}\n"
        f"失敗原因: {detail}\n\n"
        f"上記エラーを解消するための正しい aiskel c コマンド（オプションや対象指定）を提示してください。"
    )
    no_error_clipboard = "--no-error-clipboard" in raw_args
    no_focus = "--no-focus" in raw_args
    no_paste = "--no-paste" in raw_args

    copied_ok = False
    pasted = False
    paste_reason = ""
    if not no_error_clipboard:
        try:
            writer(error_report)
            copied_ok = True
            print("📋 失敗原因をクリップボードに保存しました (AIチャットにそのままペースト可能)。", file=sys.stderr)
        except Exception:
            copied_ok = False

    if copied_ok:
        pasted, paste_reason = paster(
            no_focus=no_focus,
            no_paste=no_paste,
            focus_browser=focus_browser,
            allowed_title_keywords=allowed_title_keywords,
        )

    return CopyCommandResult(
        exit_code=exit_code,
        pasted=pasted,
        paste_reason="" if pasted else (paste_reason or detail),
        copied_chars=len(error_report) if copied_ok else 0,
        error_message=detail,
    )

def execute_copy_command(
    parsed_args: argparse.Namespace,
    copy_specs: List[str],
    project_root: Path,
    focus_browser: bool = True,
    allowed_title_keywords: Optional[Sequence[str]] = None,
    *,
    snippet_extractor: Optional[Callable[..., Tuple[str, int, int]]] = None,
    clipboard_writer: Optional[Callable[[str], None]] = None,
    paste_fn: Optional[Callable[..., Tuple[bool, str]]] = None,
) -> CopyCommandResult:
    """対象ノードを抽出してクリップボードへコピーし、必要に応じてブラウザへ貼り付ける。"""
    extractor = snippet_extractor or extract_and_format_snippets
    writer = clipboard_writer or set_clipboard_text
    paster = paste_fn or focus_and_paste_clipboard
    max_chars = parsed_args.max_chars
    with_deps = getattr(parsed_args, "deps", False)
    no_focus = getattr(parsed_args, "no_focus", False)
    no_paste = getattr(parsed_args, "no_paste", False)
    try:
        formatted_text, est_tokens, item_count = extractor(
            copy_specs, project_root, max_chars=max_chars, with_deps=with_deps
        )
        writer(formatted_text)
        deps_note = " (依存ノード自動解決含む)" if with_deps else ""
        print(f"📋 クリップボードに {item_count} 件のコード{deps_note}をコピーしました。")
        print(f"   - 総文字数   : {len(formatted_text):,} 文字")
        print(f"   - 推定トークン: 約 {est_tokens:,} tokens")
        print("   AIチャットへそのままペーストしてご利用いただけます。")

        pasted, paste_reason = paster(
            no_focus=no_focus,
            no_paste=no_paste,
            focus_browser=focus_browser,
            allowed_title_keywords=allowed_title_keywords,
        )
        return CopyCommandResult(
            exit_code=0,
            pasted=pasted,
            paste_reason=paste_reason,
            copied_chars=len(formatted_text),
        )
    except (ValueError, FileNotFoundError, KeyError, RuntimeError) as e:
        print(f"✖ コピー失敗: {e}", file=sys.stderr)
        cmd_flag = "-d " if with_deps else ""
        specs_str = " ".join(copy_specs)
        error_report = (
            f"[aiskel c コード抽出エラー]\n"
            f"コマンド: aiskel c {cmd_flag}{specs_str}\n"
            f"失敗原因: {e}\n\n"
            f"上記エラーを解消するための正しい対象指定（ファイルパスやノード名）を提示してください。"
        )
        # なぜ必要か: 失敗時にAIへ即座に相談できるようエラー詳細を自動保存し成功時と同様に貼り付ける
        copied_ok = False
        pasted = False
        paste_reason = ""
        if not getattr(parsed_args, "no_error_clipboard", False):
            try:
                writer(error_report)
                copied_ok = True
                print("📋 失敗原因をクリップボードに保存しました (AIチャットにそのままペースト可能)。", file=sys.stderr)
            except Exception:
                copied_ok = False
        if copied_ok:
            pasted, paste_reason = paster(
                no_focus=no_focus,
                no_paste=no_paste,
                focus_browser=focus_browser,
                allowed_title_keywords=allowed_title_keywords,
            )
        return CopyCommandResult(
            exit_code=1,
            pasted=pasted,
            paste_reason="" if pasted else (paste_reason or str(e)),
            copied_chars=len(error_report) if copied_ok else 0,
            error_message=str(e),
        )
