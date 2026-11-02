"""Module: @role: パッチ適用後コンテンツのPython構文検証、Dry-runプレビュー生成、およびディスクへのファイル書き込み・削除トランザクションコミットを担当する。"""
import ast
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .patch_matcher import generate_diff_display
from .patch_result import PatchResult


def validate_python_syntax(
    file_contents: Dict[Path, str],
    modified_files: Set[Path],
    original_contents: Dict[Path, str],
    project_root: Path,
    files_to_delete: Optional[Set[Path]] = None,
) -> Tuple[int, List[str], Set[Path]]:
    """Pythonファイルの構文を検査し、SyntaxErrorがある場合は変更を除外しロールバックする。"""
    syntax_errors: List[str] = []
    syntax_error_files: Set[Path] = set()
    failed_details: List[str] = []
    to_delete = files_to_delete or set()

    for path, content in file_contents.items():
        if path.suffix.lower() == ".py" and path not in to_delete and path in modified_files:
            try:
                ast.parse(content, filename=str(path))
            except SyntaxError as se:
                try:
                    rel_p = path.relative_to(project_root)
                except ValueError:
                    rel_p = path
                line_snippet = f" (行: '{se.text.strip()}')" if se.text and se.text.strip() else ""
                syntax_errors.append(f"  - {rel_p}:{se.lineno}: {se.msg}{line_snippet}")
                syntax_error_files.add(path)
                failed_details.append(f"{rel_p}:{se.lineno}: 構文エラー ({se.msg}){line_snippet}")

    if syntax_errors:
        print("\n⛔ パッチ適用後のコードに構文エラー(SyntaxError)を検知しました:", file=sys.stderr)
        for err in syntax_errors:
            print(err, file=sys.stderr)
        for p in syntax_error_files:
            modified_files.discard(p)
            file_contents[p] = original_contents.get(p, "")

    return len(syntax_errors), failed_details, syntax_error_files


def commit_patch_transaction(
    file_contents: Dict[Path, str],
    original_contents: Dict[Path, str],
    modified_files: Set[Path],
    files_to_delete: Set[Path],
    project_root: Path,
    success_count: int,
    fail_count: int,
    skipped_count: int,
    failed_details: List[str],
    dry_run: bool = False,
) -> PatchResult:
    """構文検証・ドライラン出力・ディスクへのトランザクションコミットを一括実行する。"""
    err_count, syntax_failed_details, syntax_error_files = validate_python_syntax(
        file_contents, modified_files, original_contents, project_root, files_to_delete
    )
    fail_count += err_count
    failed_details.extend(syntax_failed_details)

    if dry_run:
        print("\n🔍 [DRY-RUN] 差分プレビュー (ディスクは変更されません):")
        for path in modified_files:
            orig = original_contents.get(path, "")
            diff_text = generate_diff_display(path, orig, file_contents.get(path, ""), project_root)
            if diff_text:
                print(diff_text)
        return PatchResult(success_count, fail_count, skipped_count, modified_files, failed_details)

    written_files: Set[Path] = set()
    for path in files_to_delete:
        if path in modified_files and path not in syntax_error_files:
            if path.exists():
                path.unlink()
                try:
                    parent = path.parent
                    if parent != project_root and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    pass
            written_files.add(path)

    for path in modified_files:
        if path not in files_to_delete and path in file_contents and path not in syntax_error_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_content = file_contents[path]
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            path.write_text(new_content, encoding="utf-8")
            written_files.add(path)

    return PatchResult(success_count, fail_count, skipped_count, written_files, failed_details)
