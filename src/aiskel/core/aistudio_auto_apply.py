"""Module: @role: ブリッジ経由の非対話 aiskel apply 自動実行および失敗詳細抽出を担当する。"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Optional, TextIO, Tuple

DEFAULT_APPLY_ERROR_DETAIL_CHARS = 240


class _AutoApplyTeeIO:
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


def _normalize_exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return int(exc.code)
    return 1


def extract_apply_error_detail(
    stderr_text: str,
    stdout_text: str,
    fallback_note: str = "",
    max_chars: int = DEFAULT_APPLY_ERROR_DETAIL_CHARS,
) -> str:
    """aiskel apply の標準エラーおよび標準出力から失敗箇所・原因の要約文字列を抽出する。"""
    stderr_lines: List[str] = []
    has_stderr_error = False
    for raw in stderr_text.splitlines():
        cleaned = raw.strip()
        if not cleaned or cleaned.startswith("📋") or (cleaned.startswith("---") and cleaned.endswith("---")):
            continue
        if cleaned.startswith(("❌", "✖", "エラー:", "Error:", "致命的")):
            has_stderr_error = True
        stderr_lines.append(cleaned)

    stdout_lines: List[str] = []
    has_stdout_failure = False
    in_failed_section = False
    for raw in stdout_text.splitlines():
        cleaned = raw.strip()
        if not cleaned:
            in_failed_section = False
            continue
        if cleaned == "【失敗箇所】":
            in_failed_section = True
            has_stdout_failure = True
            continue
        if in_failed_section:
            if cleaned.startswith("📋"):
                in_failed_section = False
                continue
            stdout_lines.append(cleaned)
            continue
        if cleaned.startswith(("✖", "❌", "エラー:", "Error:")):
            has_stdout_failure = True
            stdout_lines.append(cleaned)
        elif cleaned.startswith("⚠"):
            stdout_lines.append(cleaned)

    if has_stdout_failure and stderr_lines:
        # stderrの単なる警告で【失敗箇所】が隠れないよう、実エラーがある場合のみ統合する
        combined = list(dict.fromkeys(stdout_lines + stderr_lines)) if has_stderr_error else stdout_lines
    else:
        combined = stderr_lines or stdout_lines

    if not combined:
        return fallback_note
    joined = " / ".join(combined)
    if len(joined) > max_chars:
        return joined[: max_chars - 3] + "..."
    return joined


def execute_auto_apply_patch(
    project_root: Path,
    patch_text: str,
    command_runner: Callable[[List[str]], Any],
    max_error_chars: int = DEFAULT_APPLY_ERROR_DETAIL_CHARS,
) -> Tuple[int, str]:
    """一時ファイル経由・非対話モードで aiskel apply を実行し、(終了コード, 結果メッセージ)を返す。"""
    # stdinブロックやクリップボード競合、Windowsでの改行二重変換を防ぐため一時ファイル経由で渡す
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            suffix=".patch",
            prefix="aiskel_auto_apply_",
            delete=False,
        ) as tmp_file:
            tmp_file.write(patch_text)
            tmp_path = Path(tmp_file.name)

        argv = ["apply", "--dir", str(project_root), "--non-interactive", str(tmp_path)]
        tee_stdout = _AutoApplyTeeIO(sys.stdout)
        tee_stderr = _AutoApplyTeeIO(sys.stderr)
        system_exit_note = ""
        with contextlib.redirect_stdout(tee_stdout), contextlib.redirect_stderr(tee_stderr):
            try:
                raw_res = command_runner(argv)
                exit_code = int(raw_res) if isinstance(raw_res, int) else int(getattr(raw_res, "exit_code", 1))
            except SystemExit as e:
                exit_code = _normalize_exit_code(e)
                if isinstance(e.code, str) and e.code.strip():
                    system_exit_note = e.code.strip()

        if exit_code == 0:
            return 0, "aiskel a でパッチを適用しました"
        detail = extract_apply_error_detail(
            tee_stderr.getvalue(),
            tee_stdout.getvalue(),
            fallback_note=system_exit_note,
            max_chars=max_error_chars,
        )
        msg = (
            f"aiskel a が終了コード {exit_code} で失敗しました: {detail}"
            if detail
            else f"aiskel a が終了コード {exit_code} で失敗しました"
        )
        return exit_code, msg
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
