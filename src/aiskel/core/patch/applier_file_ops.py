"""Module: @role: パッチ適用時の書き込みパス認可・テキスト安全読込・ファイル移動およびファイル削除を担当する。"""

from pathlib import Path
from typing import List, Optional, Set, Tuple

from ...config import WritePathPolicy
from ...write_path_policy_updater import find_new_top_directories
from .applier_types import PatchApplyState
from .parser import PatchChunk

def safe_read_text(file_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """バイナリや非UTF-8ファイルを安全に判別してテキストを読み込む。"""
    # なぜ必要か: バイナリや非UTF-8ファイルを安全に判別しクラッシュを防ぐため
    try:
        raw_bytes = file_path.read_bytes()
    except OSError as e:
        return None, f"ファイル読み込みエラー: {e}"
    if b"\x00" in raw_bytes[:8192]:
        return None, "バイナリファイルのためテキストパッチを適用できません"
    for enc in ("utf-8", "utf-8-sig", "cp932", "latin-1"):
        try:
            return raw_bytes.decode(enc), None
        except UnicodeDecodeError:
            continue
    return None, "テキストとしてデコードできません"

def is_within_project(target_path: Path, project_root: Path) -> bool:
    """対象パスがプロジェクトルート配下に収まるか判定する。"""
    try:
        target_path.resolve().relative_to(project_root.resolve())
        return True
    except ValueError:
        return False

def is_under_extra_allowed_dirs(target_path: Path, extra_dirs: Set[Path]) -> bool:
    """対象パスが動的に許可された新規作成ディレクトリ配下か判定する。"""
    resolved = target_path.resolve()
    return any(resolved == d or d in resolved.parents for d in extra_dirs)

def check_path_with_dynamic_dirs(
    write_policy: WritePathPolicy,
    target_path: Path,
    extra_allowed_dirs: Set[Path],
) -> Tuple[bool, str]:
    """書き込みポリシーと動的許可ディレクトリを考慮してパス可否を判定する。"""
    denied, deny_reason = write_policy.is_explicitly_denied(target_path)
    if denied:
        return False, deny_reason
    allowed, reject_reason = write_policy.check_path(target_path)
    if allowed:
        return True, ""
    if is_under_extra_allowed_dirs(target_path, extra_allowed_dirs):
        return True, ""
    return False, reject_reason

def check_move_destination_policy(
    write_policy: WritePathPolicy,
    dst_path: Path,
    project_root: Path,
    extra_allowed_dirs: Set[Path],
) -> Tuple[bool, str, List[Path]]:
    """移動先パスの書き込み可否と新規許可ディレクトリ候補を判定する。"""
    if not write_policy.is_active:
        return True, "", []
    denied, deny_reason = write_policy.is_explicitly_denied(dst_path)
    if denied:
        return False, deny_reason, []
    allowed, reject_reason = check_path_with_dynamic_dirs(write_policy, dst_path, extra_allowed_dirs)
    if allowed:
        return True, "", []
    # なぜ必要か: ホワイトリスト適用下でも新規ディレクトリ作成を伴う移動整理を成立させるため
    if is_within_project(dst_path, project_root) and not dst_path.parent.exists():
        new_tops = find_new_top_directories([dst_path], project_root)
        if new_tops and not any(write_policy.is_explicitly_denied(top)[0] for top in new_tops):
            return True, "", new_tops
    return False, reject_reason, []

def ensure_file_buffer_loaded(
    state: PatchApplyState,
    current_file: Path,
    *,
    is_file_deletion: bool,
) -> None:
    """対象ファイルの仮想バッファおよび初期内容を必要に応じて読み込む。"""
    if current_file in state.file_contents:
        return
    if is_file_deletion:
        state.file_contents[current_file] = ""
        state.original_contents[current_file] = ""
    elif current_file.exists() and current_file not in state.files_to_delete:
        orig_text, _ = safe_read_text(current_file)
        state.file_contents[current_file] = orig_text
        state.original_contents[current_file] = orig_text
    else:
        state.file_contents[current_file] = ""
        state.original_contents[current_file] = ""

def apply_file_move_chunk(
    state: PatchApplyState,
    chunk: PatchChunk,
    project_root: Path,
    write_policy: WritePathPolicy,
    disp_path: Path,
    *,
    force_replace: bool = False,
    revert: bool = False,
) -> None:
    """ファイル移動・名前変更チャンクを仮想バッファ上で処理する。"""
    src_path = chunk.file_path
    dst_path = chunk.move_target_path
    if src_path is None or dst_path is None:
        return

    if dst_path.exists() and dst_path.is_dir():
        dst_path = dst_path / src_path.name

    try:
        dst_disp = dst_path.relative_to(project_root)
    except ValueError:
        dst_disp = dst_path

    is_case_only_rename = (
        src_path == dst_path
        and src_path.exists()
        and src_path.is_file()
        and src_path.resolve().name != dst_path.name
    )
    if src_path == dst_path and not is_case_only_rename:
        print(f"⏭ ファイル移動スキップ (移動元と移動先が同一): {disp_path}")
        state.skipped_count += 1
        return

    src_allowed, src_reject = check_path_with_dynamic_dirs(write_policy, src_path, state.extra_allowed_dirs)
    if not src_allowed:
        action_label = "リバート移動" if revert else "ファイル移動"
        print(f"✖ {action_label}拒否 (許可されていない移動元パス): {disp_path} (パッチ行 {chunk.block_line_no})")
        state.failed_details.append(
            f"{disp_path}: {action_label}拒否 (パッチ行 {chunk.block_line_no})\n"
            f"  -> {src_reject}"
        )
        state.fail_count += 1
        return

    dst_allowed, dst_reject, new_top_dirs = check_move_destination_policy(
        write_policy, dst_path, project_root, state.extra_allowed_dirs
    )
    if not dst_allowed:
        action_label = "リバート移動" if revert else "ファイル移動"
        print(f"✖ {action_label}拒否 (許可されていない移動先パス): {dst_disp} (パッチ行 {chunk.block_line_no})")
        state.failed_details.append(
            f"{dst_disp}: {action_label}拒否 (パッチ行 {chunk.block_line_no})\n"
            f"  -> {dst_reject}"
        )
        state.fail_count += 1
        return

    if src_path.exists() and src_path.is_dir():
        print(f"✖ ファイル移動失敗: 移動元がディレクトリです: {disp_path} (パッチ行 {chunk.block_line_no})")
        state.failed_details.append(
            f"{disp_path}: ディレクトリ全体の移動には対応していません (ファイルパスを指定してください)"
        )
        state.fail_count += 1
        return

    if not is_case_only_rename and dst_path.exists() and dst_path.is_dir():
        print(f"✖ ファイル移動失敗: 移動先がディレクトリです: {dst_disp} (パッチ行 {chunk.block_line_no})")
        state.failed_details.append(f"{dst_disp}: 移動先と同名のディレクトリが既に存在します")
        state.fail_count += 1
        return

    src_in_memory = (src_path in state.file_contents and src_path not in state.files_to_delete) or (
        src_path in state.binary_contents
    )
    src_on_disk = src_path.exists() and src_path.is_file() and src_path not in state.files_to_delete
    dst_in_memory = not is_case_only_rename and (
        (dst_path in state.file_contents and dst_path not in state.files_to_delete)
        or (dst_path in state.binary_contents)
    )
    dst_on_disk = (not is_case_only_rename) and dst_path.exists() and dst_path not in state.files_to_delete

    if not src_in_memory and not src_on_disk:
        if dst_in_memory or dst_on_disk:
            print(f"⏭ ファイル移動スキップ (既に移動済み): {disp_path} -> {dst_disp}")
            state.skipped_count += 1
        else:
            print(f"✖ ファイル移動失敗: 移動元ファイルが存在しません: {disp_path} (パッチ行 {chunk.block_line_no})")
            state.failed_details.append(f"{disp_path}: 移動元ファイルが存在しません (パッチ行 {chunk.block_line_no})")
            state.fail_count += 1
        return

    if (dst_in_memory or dst_on_disk) and not force_replace:
        print(f"✖ ファイル移動失敗: 移動先に既にファイルが存在します: {dst_disp} (パッチ行 {chunk.block_line_no})")
        state.failed_details.append(
            f"{dst_disp}: 移動先に既にファイルが存在します (上書きするには -f/--force-replace を指定してください)"
        )
        state.fail_count += 1
        return

    state.extra_allowed_dirs.update(new_top_dirs)

    # なぜ必要か: 連続移動・往復移動時に中間ファイル残留や元内容上書きを防ぐため
    moved_binary: Optional[bytes] = None
    moved_text: Optional[str] = None
    if src_path in state.binary_contents:
        moved_binary = state.binary_contents.pop(src_path)
    elif src_path in state.file_contents:
        cached_text = state.file_contents.pop(src_path)
        if cached_text is None and src_on_disk:
            moved_binary = src_path.read_bytes()
        else:
            moved_text = cached_text or ""
    else:
        src_text, _ = safe_read_text(src_path)
        state.original_contents.setdefault(src_path, src_text)
        if src_text is None:
            moved_binary = src_path.read_bytes()
        else:
            moved_text = src_text

    if dst_path not in state.original_contents:
        if dst_on_disk and dst_path.is_file():
            orig_dst_text, _ = safe_read_text(dst_path)
            state.original_contents[dst_path] = orig_dst_text
        else:
            state.original_contents[dst_path] = ""

    root_src = state.move_sources.pop(src_path, src_path)
    state.modified_files.discard(src_path)
    state.case_rename_targets.discard(src_path)

    if is_case_only_rename:
        state.case_rename_targets.add(dst_path)
    elif dst_path == root_src:
        state.files_to_delete.discard(root_src)
    elif root_src.exists() and root_src.is_file():
        state.files_to_delete.add(root_src)
        state.modified_files.add(root_src)
        state.file_contents[root_src] = ""
        state.move_sources[dst_path] = root_src

    if moved_binary is not None:
        state.binary_contents[dst_path] = moved_binary
        state.file_contents[dst_path] = None
    else:
        state.binary_contents.pop(dst_path, None)
        state.file_contents[dst_path] = moved_text if moved_text is not None else ""

    state.files_to_delete.discard(dst_path)
    state.modified_files.add(dst_path)

    if src_path.parent == dst_path.parent:
        print(f"✏ ファイル名変更成功: {disp_path} -> {dst_disp}")
    else:
        print(f"📦 ファイル移動成功: {disp_path} -> {dst_disp}")
    state.success_count += 1

def _delete_file_in_buffer(
    state: PatchApplyState,
    current_file: Path,
    disp_path: Path,
    on_disk: bool,
    *,
    ensure_original_entry: bool = False,
) -> None:
    """仮想バッファおよび関連追跡状態を対象ファイル削除後の状態へ更新する。"""
    state.binary_contents.pop(current_file, None)
    state.case_rename_targets.discard(current_file)
    state.move_sources.pop(current_file, None)
    if on_disk:
        state.files_to_delete.add(current_file)
        state.modified_files.add(current_file)
        state.file_contents[current_file] = ""
        if ensure_original_entry:
            state.original_contents.setdefault(current_file, "")
    else:
        state.modified_files.discard(current_file)
        state.file_contents.pop(current_file, None)
    print(f"🗑 ファイル削除成功: {disp_path}")
    state.success_count += 1

def apply_standalone_file_delete(state: PatchApplyState, current_file: Path, disp_path: Path) -> None:
    """置換ブロックを伴わない単体ファイル削除ディレクティブを仮想バッファ上で処理する。"""
    in_mem = (current_file in state.file_contents and current_file not in state.files_to_delete) or (
        current_file in state.binary_contents
    )
    on_disk = current_file.exists() and current_file not in state.files_to_delete
    if not in_mem and not on_disk:
        print(f"⏭ ファイル削除スキップ (既に存在しません): {disp_path}")
        state.skipped_count += 1
        return
    _delete_file_in_buffer(state, current_file, disp_path, on_disk, ensure_original_entry=True)

def apply_block_file_delete(
    state: PatchApplyState,
    current_file: Path,
    disp_path: Path,
    content: Optional[str],
) -> None:
    """ファイル丸ごと削除チャンクを仮想バッファ上で処理する。"""
    in_mem = (current_file in state.file_contents and current_file not in state.files_to_delete) or (
        current_file in state.binary_contents
    )
    on_disk = current_file.exists() and current_file not in state.files_to_delete
    if not on_disk and not content and not in_mem:
        print(f"⏭ ファイル削除スキップ (既に存在しません): {disp_path}")
        state.skipped_count += 1
        return
    _delete_file_in_buffer(state, current_file, disp_path, on_disk)
