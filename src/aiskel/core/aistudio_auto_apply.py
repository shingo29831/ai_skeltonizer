"""Module: @role: ブリッジ経由の非対話 aiskel apply 自動実行および失敗詳細抽出を担当する。"""
import contextlib
import io
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, TextIO, Tuple

DEFAULT_APPLY_ERROR_DETAIL_CHARS = 240
DEFAULT_CHAT_ERROR_REPORT_CHARS = 4000
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

@dataclass(frozen=True)
class AutoApplyOutcome:
    """事前検証(check)および実適用(apply)の結果とチャット送信向けエラーレポートを保持する。"""

    exit_code: int
    message: str
    phase: str = ""
    error_report: str = ""
    test_failed: bool = False


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
    clean_err = _strip_ansi(stderr_text or "")
    clean_out = _strip_ansi(stdout_text or "")
    stderr_lines: List[str] = []
    has_stderr_error = False
    for raw in clean_err.splitlines():
        cleaned = raw.strip()
        if (
            not cleaned
            or cleaned.startswith("📋")
            or (cleaned.startswith("---") and cleaned.endswith("---"))
            or set(cleaned) == {"-"}
        ):
            continue
        if cleaned.startswith(("❌", "✖", "エラー:", "Error:", "致命的")):
            has_stderr_error = True
        stderr_lines.append(cleaned)

    stdout_lines: List[str] = []
    has_stdout_failure = False
    in_failed_section = False
    for raw in clean_out.splitlines():
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


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)

def extract_apply_success_summary(
    stdout_text: str,
    stderr_text: str,
    max_chars: int = DEFAULT_APPLY_ERROR_DETAIL_CHARS,
) -> Tuple[str, bool]:
    """aiskel check / apply 成功時の出力から件数・テスト・コミット等の要約とテスト失敗有無を抽出する。"""
    clean_out = _strip_ansi(stdout_text or "")
    clean_err = _strip_ansi(stderr_text or "")
    parts: List[str] = []
    test_failed = False

    success_matches = re.findall(r"✔\s*成功:\s*(\d+)\s*箇所", clean_out)
    if success_matches:
        parts.append(f"成功: {success_matches[-1]}箇所")
    skip_matches = re.findall(r"⏭\s*スキップ[^:]*:\s*(\d+)\s*箇所", clean_out)
    if skip_matches and int(skip_matches[-1]) > 0:
        parts.append(f"スキップ: {skip_matches[-1]}箇所")

    if "✅ 静的コード解析に成功しました" in clean_out:
        parts.append("静的解析成功")

    if "❌ 検証テストが失敗しました" in clean_err or "❌ 検証テストが失敗しました" in clean_out:
        test_failed = True
        parts.append("テスト失敗")
    elif "✅ 検証テストに成功しました" in clean_out:
        parts.append("テスト成功")

    if "✔ コミットが完了しました" in clean_out:
        parts.append("コミット完了")
    elif "コミットに失敗しました" in clean_out or "コミットに失敗しました" in clean_err:
        parts.append("コミット失敗")
    elif "変更内容が既存コードと同一のため、コミットをスキップしました" in clean_out:
        parts.append("変更なし")
    elif (
        "コミットを保留しました" in clean_out
        or "コミットは保留しました" in clean_out
        or "コミットを拒否（スキップ）しました" in clean_out
    ):
        parts.append("コミット保留")

    bloat_matches = re.findall(r"⚠\s*肥大化ファイルを検出しました\s*\((\d+)件", clean_out)
    if bloat_matches:
        parts.append(f"肥大化警告: {bloat_matches[-1]}件")

    if not parts:
        return "", test_failed
    summary = " / ".join(parts)
    if len(summary) > max_chars:
        if max_chars <= 3:
            summary = summary[: max(0, max_chars)]
        else:
            summary = summary[: max_chars - 3] + "..."
    return summary, test_failed

def extract_apply_error_report(
    stderr_text: str,
    stdout_text: str,
    *,
    phase: str = "apply",
    exit_code: int = 1,
    fallback_note: str = "",
    max_chars: int = DEFAULT_CHAT_ERROR_REPORT_CHARS,
) -> str:
    """aiskel check / apply のエラー出力からAIチャット自動送信用の詳細エラーレポートを構築する。"""
    clean_stdout = _strip_ansi(stdout_text or "")
    clean_stderr = _strip_ansi(stderr_text or "")

    extracted_stdout: List[str] = []
    in_failed_section = False
    for raw in clean_stdout.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("=== ") and stripped.endswith(" ==="):
            in_failed_section = False
            extracted_stdout.append(stripped)
            continue
        if stripped == "【失敗箇所】":
            in_failed_section = True
            extracted_stdout.append(stripped)
            continue
        if in_failed_section:
            if stripped.startswith(("📋", "⚠", "ℹ", "📦")):
                in_failed_section = False
                continue
            extracted_stdout.append(raw.rstrip())
            continue
        if stripped.startswith(("✖", "❌", "⛔", "エラー:", "Error:", "致命的")):
            extracted_stdout.append(raw.rstrip())

    is_static_analysis_failure = (
        "静的コード解析が失敗しました" in clean_stderr
        or "【静的コード解析エラー】" in clean_stdout
        or "【静的コード解析エラー】" in clean_stderr
    )

    extracted_stderr: List[str] = []
    for raw in clean_stderr.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("📋") or set(stripped) == {"-"}:
            continue
        if is_static_analysis_failure and stripped in ("--- 静的コード解析エラーログ ---", "--- テスト失敗ログ ---"):
            continue
        extracted_stderr.append(raw.rstrip())

    body_lines: List[str] = []
    if extracted_stdout:
        body_lines.extend(extracted_stdout)
    if extracted_stderr:
        if body_lines:
            body_lines.append("")
        body_lines.extend(extracted_stderr)

    if not body_lines and fallback_note:
        body_lines.append(fallback_note)

    if phase == "apply_check":
        cmd_label = "aiskel a --check (置換検証)"
    elif phase == "check":
        cmd_label = "aiskel check (置換検証)"
    else:
        cmd_label = "aiskel a"

    if is_static_analysis_failure:
        header = (
            f"提示されたパッチの適用後に行われた静的コード解析に失敗したため、変更を元に戻しました ({cmd_label}, 終了コード: {exit_code})。\n"
            "以下の静的コード解析エラーを解消し、現在のコードベースに対して単体でそのまま適用できる修正パッチを再提示してください。"
        )
    else:
        header = (
            f"提示されたパッチの適用に失敗しました ({cmd_label}, 終了コード: {exit_code})。\n"
            "以下のエラー箇所について、現在のコードと整合する修正パッチを再提示してください。"
        )
    if not body_lines:
        return header

    report = header + "\n\n" + "\n".join(body_lines)
    if len(report) > max_chars:
        return report[: max_chars - 3] + "..."
    return report

def _run_captured_command(
    command_runner: Callable[[List[str]], Any],
    argv: List[str],
) -> Tuple[int, str, str, str]:
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
    return exit_code, tee_stdout.getvalue(), tee_stderr.getvalue(), system_exit_note

@contextlib.contextmanager
def _temporary_patch_file(patch_text: str) -> Iterator[Path]:
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
        yield tmp_path
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

def _execute_patch_command_phase(
    command_runner: Callable[[List[str]], Any],
    argv: List[str],
    *,
    phase: str,
    cmd_display: str,
    success_message: str,
    max_error_chars: int,
    max_report_chars: int,
) -> AutoApplyOutcome:
    exit_code, out, err, note = _run_captured_command(command_runner, argv)
    if exit_code == 0:
        summary, test_failed = extract_apply_success_summary(out, err, max_chars=max_error_chars)
        final_msg = f"{success_message} ({summary})" if (success_message and summary) else (success_message or summary)
        return AutoApplyOutcome(exit_code=0, message=final_msg, phase=phase, test_failed=test_failed)

    detail = extract_apply_error_detail(
        err,
        out,
        fallback_note=note,
        max_chars=max_error_chars,
    )
    msg = (
        f"{cmd_display} が終了コード {exit_code} で失敗しました: {detail}"
        if detail
        else f"{cmd_display} が終了コード {exit_code} で失敗しました"
    )
    report = extract_apply_error_report(
        err,
        out,
        phase=phase,
        exit_code=exit_code,
        fallback_note=note or detail,
        max_chars=max_report_chars,
    )
    return AutoApplyOutcome(exit_code=exit_code, message=msg, phase=phase, error_report=report)

def execute_auto_check_patch_detailed(
    project_root: Path,
    patch_text: str,
    command_runner: Callable[[List[str]], Any],
    max_error_chars: int = DEFAULT_APPLY_ERROR_DETAIL_CHARS,
    max_report_chars: int = DEFAULT_CHAT_ERROR_REPORT_CHARS,
) -> AutoApplyOutcome:
    """一時ファイル経由で非対話の aiskel a --check を実行し、検証結果を返す。"""
    with _temporary_patch_file(patch_text) as tmp_path:
        check_argv = ["apply", "--check", "--dir", str(project_root), "--non-interactive", str(tmp_path)]
        return _execute_patch_command_phase(
            command_runner,
            check_argv,
            phase="apply_check",
            cmd_display="aiskel a --check (置換検証)",
            success_message="aiskel a --check でパッチ検証に成功しました",
            max_error_chars=max_error_chars,
            max_report_chars=max_report_chars,
        )

def execute_auto_apply_patch_detailed(
    project_root: Path,
    patch_text: str,
    command_runner: Callable[[List[str]], Any],
    max_error_chars: int = DEFAULT_APPLY_ERROR_DETAIL_CHARS,
    max_report_chars: int = DEFAULT_CHAT_ERROR_REPORT_CHARS,
) -> AutoApplyOutcome:
    """一時ファイル経由で事前検証(check)を行い、成功時のみ aiskel apply を実行する。"""
    with _temporary_patch_file(patch_text) as tmp_path:
        check_argv = ["check", "--dir", str(project_root), "--non-interactive", str(tmp_path)]
        check_outcome = _execute_patch_command_phase(
            command_runner,
            check_argv,
            phase="check",
            cmd_display="aiskel check (置換検証)",
            success_message="",
            max_error_chars=max_error_chars,
            max_report_chars=max_report_chars,
        )
        if check_outcome.exit_code != 0:
            return check_outcome

        apply_argv = ["apply", "--dir", str(project_root), "--non-interactive", "--yes", "--no-push", str(tmp_path)]
        return _execute_patch_command_phase(
            command_runner,
            apply_argv,
            phase="apply",
            cmd_display="aiskel a",
            success_message="aiskel a でパッチを適用しました",
            max_error_chars=max_error_chars,
            max_report_chars=max_report_chars,
        )

def execute_auto_apply_patch(
    project_root: Path,
    patch_text: str,
    command_runner: Callable[[List[str]], Any],
    max_error_chars: int = DEFAULT_APPLY_ERROR_DETAIL_CHARS,
) -> Tuple[int, str]:
    """一時ファイル経由・非対話モードで事前検証後に aiskel apply を実行し、(終了コード, 結果メッセージ)を返す。"""
    outcome = execute_auto_apply_patch_detailed(
        project_root=project_root,
        patch_text=patch_text,
        command_runner=command_runner,
        max_error_chars=max_error_chars,
    )
    return outcome.exit_code, outcome.message
