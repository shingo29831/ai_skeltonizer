"""Module: @role: パッチ適用後コンテンツのPython構文検証、Dry-runプレビュー生成、およびディスクへのファイル書き込み・削除トランザクションコミットを担当する。"""
import ast
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

from ...config import (
    WritePathPolicy,
    load_write_path_policy_from_meta,
)
from ...write_path_policy_loader import (
    ALLOWED_WRITE_PATH_TXT_FILES,
    _collect_meta_dirs,
    iter_meta_config_files,
)
from ...write_path_policy_updater import (
    find_new_top_directories,
    update_write_policy_for_created_dirs,
)
from .matcher import generate_diff_display
from .result import PatchResult


def validate_python_syntax(
    file_contents: Dict[Path, Optional[str]],
    modified_files: Set[Path],
    original_contents: Dict[Path, Optional[str]],
    project_root: Path,
    files_to_delete: Optional[Set[Path]] = None,
) -> Tuple[int, List[str], Set[Path]]:
    """Pythonファイルの構文を検査し、SyntaxErrorがある場合は変更を除外しロールバックする。"""
    syntax_errors: List[str] = []
    syntax_error_files: Set[Path] = set()
    failed_details: List[str] = []
    to_delete = files_to_delete or set()

    for path, content in file_contents.items():
        if path.suffix.lower() == ".py" and path not in to_delete and path in modified_files and content is not None:
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


def _is_under_extra_dirs(target_path: Path, extra_dirs: Sequence[Path]) -> bool:
    if not extra_dirs:
        return False
    resolved = target_path.resolve()
    return any(resolved == d.resolve() or d.resolve() in resolved.parents for d in extra_dirs)


def _cleanup_empty_parent_dirs(start_dir: Path, project_root: Path) -> None:
    resolved_root = project_root.resolve()
    cur = start_dir.resolve()
    while cur != resolved_root:
        try:
            cur.relative_to(resolved_root)
        except ValueError:
            break
        if not cur.is_dir():
            break
        try:
            if any(cur.iterdir()):
                break
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent


def commit_patch_transaction(
    file_contents: Dict[Path, Optional[str]],
    original_contents: Dict[Path, Optional[str]],
    modified_files: Set[Path],
    files_to_delete: Set[Path],
    project_root: Path,
    success_count: int,
    fail_count: int,
    skipped_count: int,
    failed_details: List[str],
    dry_run: bool = False,
    show_diff: bool = True,
    allowed_write_paths: Optional[Sequence[Union[str, Path]]] = None,
    binary_contents: Optional[Dict[Path, bytes]] = None,
    move_sources: Optional[Dict[Path, Path]] = None,
    extra_allowed_dirs: Optional[Sequence[Path]] = None,
    case_rename_targets: Optional[Sequence[Path]] = None,
) -> PatchResult:
    """構文検証・ドライラン出力・ディスクへのトランザクションコミットを一括実行する。"""
    write_policy = (
        load_write_path_policy_from_meta(project_root)
        if allowed_write_paths is None
        else WritePathPolicy.from_allowed_paths(allowed_write_paths, project_root)
    )
    extra_dirs = list(extra_allowed_dirs or [])
    move_src_map = dict(move_sources or {})
    bin_map = dict(binary_contents or {})
    case_rename_set = set(case_rename_targets or ())
    persist_policy_update = allowed_write_paths is None

    if write_policy.is_active:
        disallowed_reasons: Dict[Path, str] = {}
        for p in sorted(modified_files | files_to_delete):
            denied, deny_reason = write_policy.is_explicitly_denied(p)
            if denied:
                disallowed_reasons[p] = deny_reason
                continue
            allowed, reject_reason = write_policy.check_path(p)
            if not allowed and not _is_under_extra_dirs(p, extra_dirs):
                disallowed_reasons[p] = reject_reason
        for p, reject_reason in disallowed_reasons.items():
            try:
                rel_p = p.relative_to(project_root)
            except ValueError:
                rel_p = p
            modified_files.discard(p)
            files_to_delete.discard(p)
            bin_map.pop(p, None)
            file_contents[p] = original_contents.get(p, "")
            # なぜ必要か: 移動先がポリシー拒否された際に移動元ファイルだけ削除されるのを防ぐため
            if p in move_src_map:
                src_p = move_src_map[p]
                files_to_delete.discard(src_p)
                modified_files.discard(src_p)
                file_contents[src_p] = original_contents.get(src_p, "")
            print(f"✖ 書き込み拒否 (許可されていないパス): {rel_p}")
            failed_details.append(f"{rel_p}: {reject_reason}")
            fail_count += 1

    err_count, syntax_failed_details, syntax_error_files = validate_python_syntax(
        file_contents, modified_files, original_contents, project_root, files_to_delete
    )
    fail_count += err_count
    failed_details.extend(syntax_failed_details)

    # なぜ必要か: 移動先ファイルに構文エラーが発生した際に移動元ファイルだけ削除されて消失するのを防ぐため
    protected_sources: Set[Path] = {
        move_src_map[err_dst]
        for err_dst in syntax_error_files
        if err_dst in move_src_map
    }
    for src_p in protected_sources:
        files_to_delete.discard(src_p)
        modified_files.discard(src_p)
        file_contents[src_p] = original_contents.get(src_p, "")

    write_targets = [
        p for p in sorted(modified_files, key=lambda x: str(x))
        if p not in files_to_delete
        and p not in syntax_error_files
        and (p in bin_map or (p in file_contents and file_contents[p] is not None))
    ]
    created_top_dirs = find_new_top_directories(write_targets, project_root)

    if dry_run:
        if show_diff:
            print("\n🔍 [DRY-RUN] 差分プレビュー (ディスクは変更されません):")
            for path in modified_files:
                orig = original_contents.get(path, "") or ""
                updated = file_contents.get(path, "") or ""
                diff_text = generate_diff_display(path, orig, updated, project_root)
                if diff_text:
                    print(diff_text)
        policy_added_dirs, _ = update_write_policy_for_created_dirs(
            project_root,
            created_top_dirs,
            write_policy,
            dry_run=True,
            created_files=write_targets,
            persist=persist_policy_update,
        )
        return PatchResult(
            success_count,
            fail_count,
            skipped_count,
            modified_files,
            failed_details,
            created_dirs=created_top_dirs,
            policy_added_dirs=policy_added_dirs,
        )

    rollback_snapshot: Dict[Path, Optional[bytes]] = {}
    for path in set(write_targets) | (files_to_delete & modified_files):
        if path in syntax_error_files:
            continue
        resolved_key = path.resolve()
        # 大文字小文字のみ異なるリネーム先は既存別ケースと衝突するため未存在扱いにする
        if path in case_rename_set and path.exists():
            try:
                if path.resolve().name != path.name:
                    rollback_snapshot[path] = None
                    continue
            except OSError:
                pass
        if path.exists() and path.is_file():
            try:
                rollback_snapshot[resolved_key] = path.read_bytes()
            except OSError:
                # 読込失敗時にNone登録するとロールバックで既存ファイルを誤削除するため登録しない
                continue
        else:
            rollback_snapshot[resolved_key] = None

    pre_policy_bytes: Dict[Path, bytes] = {}
    if created_top_dirs and write_policy.is_active and persist_policy_update:
        candidate_policy_files: List[Path] = list(iter_meta_config_files(project_root))
        for meta_dir in _collect_meta_dirs(project_root):
            for txt_name in ALLOWED_WRITE_PATH_TXT_FILES:
                candidate_policy_files.append(meta_dir / txt_name)
        for pf in candidate_policy_files:
            if pf.is_file():
                try:
                    pre_policy_bytes[pf.resolve()] = pf.read_bytes()
                except OSError:
                    pass

    written_files: Set[Path] = set()
    for path in files_to_delete:
        if path in modified_files and path not in syntax_error_files:
            if path.exists():
                path.unlink()
                _cleanup_empty_parent_dirs(path.parent, project_root)
            written_files.add(path)

    for path in write_targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path in case_rename_set and path.exists():
            try:
                if path.resolve().name != path.name:
                    path.unlink()
            except OSError:
                pass
        if path in bin_map:
            path.write_bytes(bin_map[path])
            written_files.add(path)
            continue
        new_content = file_contents[path]
        if new_content is not None:
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            path.write_text(new_content, encoding="utf-8")
            written_files.add(path)

    policy_added_dirs: List[Path] = []
    if created_top_dirs:
        if write_policy.is_active:
            policy_added_dirs, updated_policy_path = update_write_policy_for_created_dirs(
                project_root,
                created_top_dirs,
                write_policy,
                dry_run=False,
                created_files=write_targets,
                persist=persist_policy_update,
            )
            if updated_policy_path is not None:
                resolved_policy = updated_policy_path.resolve()
                rollback_snapshot.setdefault(resolved_policy, pre_policy_bytes.get(resolved_policy))
        else:
            disp_dirs = []
            for d in created_top_dirs:
                try:
                    disp_dirs.append(f"{d.relative_to(project_root).as_posix()}/")
                except ValueError:
                    disp_dirs.append(str(d))
            print(f"📁 新規ディレクトリを作成しました: {', '.join(disp_dirs)}")

    return PatchResult(
        success_count,
        fail_count,
        skipped_count,
        written_files,
        failed_details,
        created_dirs=created_top_dirs,
        policy_added_dirs=policy_added_dirs,
        rollback_snapshot=rollback_snapshot,
    )
