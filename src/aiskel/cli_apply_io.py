"""Module: @role: パッチ入力ソース(ファイル・標準入力・クリップボード)の読込および適用結果・失敗レポート出力を担当する。"""
import argparse
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional

from .core.clipboard import get_clipboard_text, set_clipboard_text
from .core.patch_parser import is_file_deletion_patch


def has_applicable_patch_content(patch_text: str) -> bool:
    return bool(patch_text and (re.search(r"<{4,}", patch_text) or is_file_deletion_patch(patch_text)))


def load_patch_text(
    parsed_args: argparse.Namespace,
    is_revert: bool,
    allow_stdin_fallback: bool = True,
    clipboard_reader: Optional[Callable[[], str]] = None,
    non_interactive: bool = False,
) -> Optional[str]:
    action_name = "リバート" if is_revert else "適用"
    if parsed_args.patch_file:
        patch_file: Path = parsed_args.patch_file.resolve()
        if not patch_file.exists():
            print(f"エラー: パッチファイルが見つかりません: {patch_file}", file=sys.stderr)
            return None
        print(f"🚀 AIパッチの{action_name}を開始します (ファイル: {patch_file.name})")
        return patch_file.read_text(encoding="utf-8")

    if parsed_args.paste:
        if non_interactive:
            print("エラー: --non-interactive 指定時は -p/--paste による手動入力待機を使用できません。", file=sys.stderr)
            return None
        if not allow_stdin_fallback and not sys.stdin.isatty():
            print("⚠ パイプ入力後の待機プロンプトでは -p/--paste を使用できません。クリップボード経由で 'aiskel a' を実行してください。", file=sys.stderr)
            return None
        print("🚀 AIの出力テキストをペーストしてください。")
        print("   (ペースト後、Windowsは Ctrl+Z を押してEnter、Mac/Linuxは Ctrl+D を押すと実行されます):")
        patch_text = sys.stdin.read()
        print(f"\n{action_name}を開始します...")
        return patch_text

    if allow_stdin_fallback and not sys.stdin.isatty():
        return sys.stdin.read()

    reader = clipboard_reader or get_clipboard_text
    patch_text = reader()
    if not has_applicable_patch_content(patch_text):
        if not patch_text:
            print("⚠ クリップボードが空です。")
        else:
            print("⚠ クリップボードに置換ブロック(<<<<)または削除指示が見つかりません。")
        if non_interactive or not allow_stdin_fallback:
            print("   修正パッチをコピーしてから再度 'aiskel a' を実行してください。")
            return None
        print("🚀 AIの出力テキストをペーストしてください。")
        print("   (ペースト後、Windowsは Ctrl+Z を押してEnter、Mac/Linuxは Ctrl+D を押すと実行されます):")
        patch_text = sys.stdin.read()
        print(f"\n{action_name}を開始します...")
        return patch_text

    print("🚀 クリップボードからAIの出力テキストを読み込みました。")
    return patch_text


def print_failed_details(failed_details: List[str], indent: str = "  ") -> None:
    if not failed_details:
        return
    print("\n【失敗箇所】")
    for detail in failed_details:
        d_lines = detail.splitlines()
        print(f"{indent}- {d_lines[0]}")
        for sub_line in d_lines[1:]:
            print(f"{indent}  {sub_line}")


def copy_failure_report_to_clipboard(
    total_success: int,
    fail: int,
    failed_details: List[str],
    clipboard_writer: Optional[Callable[[str], None]] = None,
) -> None:
    if total_success > 0:
        header = f"提示されたパッチのうち一部のみ適用されました (成功: {total_success}箇所, 失敗: {fail}箇所)。"
    else:
        header = f"提示されたパッチの適用に失敗しました (失敗: {fail}箇所)。"

    report_lines = [
        header,
        "以下の失敗箇所について、現在のコードと整合する修正パッチを再提示してください。\n",
        "【失敗箇所】",
    ]
    if failed_details:
        for detail in failed_details:
            d_lines = detail.splitlines()
            report_lines.append(f"- {d_lines[0]}")
            for sub_line in d_lines[1:]:
                report_lines.append(f"  {sub_line}")
    else:
        report_lines.append("- 置換テキストの不一致または構文エラー")

    report_text = "\n".join(report_lines)
    writer = clipboard_writer or set_clipboard_text
    try:
        writer(report_text)
        status_label = "一部適用の結果" if total_success > 0 else "適用失敗の結果"
        print(f"📋 {status_label}と失敗箇所をクリップボードにコピーしました (AIチャットにそのままペースト可能)。")
    except Exception as e:
        print(f"⚠ クリップボードへの保存に失敗しました: {e}", file=sys.stderr)


def report_patch_result(
    success: int,
    fail: int,
    skipped: int,
    failed_details: List[str],
    total_success: int,
    total_skipped: int,
    iteration: int,
    is_revert: bool,
    is_dry_run: bool,
    clipboard_writer: Optional[Callable[[str], None]] = None,
    copy_failure_report: bool = True,
) -> None:
    action_name = "リバート" if is_revert else "適用"
    mode_prefix = "[DRY-RUN] " if is_dry_run else ""
    print(f"\n=== {mode_prefix}{action_name}結果 ===")
    iter_note = f" (累計: {total_success} 箇所)" if iteration > 1 else ""
    print(f"✔ 成功: {success} 箇所{iter_note}")
    if skipped > 0 or (iteration > 1 and total_skipped > 0):
        sk_note = f" (累計: {total_skipped} 箇所)" if iteration > 1 else ""
        print(f"⏭ スキップ (適用済み): {skipped} 箇所{sk_note}")

    if fail > 0:
        print(f"✖ 失敗: {fail} 箇所")
        print_failed_details(failed_details)
        if copy_failure_report and not is_dry_run and not is_revert:
            copy_failure_report_to_clipboard(
                total_success=total_success,
                fail=fail,
                failed_details=failed_details,
                clipboard_writer=clipboard_writer,
            )
