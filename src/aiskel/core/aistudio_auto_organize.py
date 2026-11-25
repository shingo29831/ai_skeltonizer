"""Module: @role: 自動階層整理モード向けの過密ディレクトリ抽出・対象検証・リネーム制約検証・プロンプト統合を担当する。"""
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config import DEFAULT_ORGANIZE_MAX_FILES, load_organize_config_from_meta
from .aistudio_auto_split import _parse_positive_int, _resolve_positive_threshold
from .aistudio_handoff import format_summary_prompt_text
from .aistudio_prompts import build_dir_organize_prompt, load_default_prompt
from .bloat_detector import count_file_metrics, is_path_excluded
from .patch_parser import parse_patch_chunks
from .scanner import get_target_files

_META_DIR_NAMES = {"ai_meta", ".ai_meta"}

@dataclass
class OrganizeDirInfo:
    """同一階層に多数のファイルを含む整理対象ディレクトリ情報を保持する。"""

    dir_path: Path
    rel_dir: str
    file_count: int
    total_lines: int
    total_chars: int
    files: List[str]

def _is_meta_or_packaging_path(rel_posix: str) -> bool:
    parts = [p for p in rel_posix.split("/") if p]
    if not parts:
        return False
    if parts[0] in _META_DIR_NAMES:
        return True
    return any(part.endswith(".egg-info") for part in parts)

def collect_organize_targets_sorted_by_count(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> Tuple[List[OrganizeDirInfo], int]:
    """同一階層内の直接ファイル数が閾値以上のディレクトリを検出し、ファイル数降順で返す。"""
    resolved_root = project_root.resolve()
    meta_max_files, meta_excludes = load_organize_config_from_meta(resolved_root, output_dir)
    max_files = _resolve_positive_threshold(meta_max_files, DEFAULT_ORGANIZE_MAX_FILES)

    target_files = get_target_files(resolved_root)
    grouped: Dict[str, List[Tuple[Path, str, int, int]]] = defaultdict(list)

    for file_path in target_files:
        if is_path_excluded(file_path, resolved_root, meta_excludes):
            continue
        try:
            rel_path = file_path.relative_to(resolved_root)
        except ValueError:
            continue
        rel_posix = rel_path.as_posix()
        if _is_meta_or_packaging_path(rel_posix):
            continue

        metrics = count_file_metrics(file_path)
        if metrics is None:
            continue
        lines, chars = metrics
        rel_dir = rel_path.parent.as_posix() or "."
        grouped[rel_dir].append((file_path, file_path.name, lines, chars))

    targets: List[OrganizeDirInfo] = []
    for rel_dir, entries in grouped.items():
        if len(entries) < max_files:
            continue
        entries.sort(key=lambda item: item[1].lower())
        dir_path = resolved_root if rel_dir == "." else (resolved_root / rel_dir)
        targets.append(
            OrganizeDirInfo(
                dir_path=dir_path,
                rel_dir=rel_dir,
                file_count=len(entries),
                total_lines=sum(item[2] for item in entries),
                total_chars=sum(item[3] for item in entries),
                files=[item[1] for item in entries],
            )
        )

    targets.sort(
        key=lambda item: (-item.file_count, -item.total_chars, -item.total_lines, item.rel_dir.lower())
    )
    return targets, max_files

def format_organize_targets_payload(
    project_root: Path,
    dirs: List[OrganizeDirInfo],
    max_files: int,
) -> Dict[str, object]:
    """検出した過密ディレクトリ群をブリッジAPI応答用の辞書へ整形する。"""
    total_target_files = sum(item.file_count for item in dirs)
    return {
        "ok": True,
        "project_root": str(project_root.resolve()),
        "max_files": max_files,
        "total_target_files": total_target_files,
        "dirs": [
            {
                "rel_dir": item.rel_dir,
                "file_count": item.file_count,
                "total_lines": item.total_lines,
                "total_chars": item.total_chars,
            }
            for item in dirs
        ],
    }

def _collect_direct_files_in_dir(
    resolved_root: Path,
    resolved_dir: Path,
    exclude_paths: Set[str],
) -> List[Tuple[str, int, int]]:
    items: List[Tuple[str, int, int]] = []
    for file_path in get_target_files(resolved_root):
        if file_path.parent.resolve() != resolved_dir:
            continue
        if is_path_excluded(file_path, resolved_root, exclude_paths):
            continue
        try:
            rel_posix = file_path.relative_to(resolved_root).as_posix()
        except ValueError:
            continue
        if _is_meta_or_packaging_path(rel_posix):
            continue
        metrics = count_file_metrics(file_path)
        if metrics is None:
            continue
        lines, chars = metrics
        items.append((file_path.name, lines, chars))
    # 同一プレフィックスの機能グループを視認しやすくするためファイル名順で整列
    items.sort(key=lambda x: x[0].lower())
    return items

def validate_organize_target_request(
    project_root: Path,
    payload: Dict[str, object],
    output_dir: Optional[Path] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """階層整理リクエスト内の対象ディレクトリパスと各種メトリクスを検証・正規化する。"""
    raw_target = payload.get("target_dir")
    if raw_target is None and isinstance(payload.get("target_file"), str):
        raw_target = payload.get("target_file")
    if not isinstance(raw_target, str) or not raw_target.strip():
        return None, "target_dir を文字列で指定してください"
    if "\x00" in raw_target or "\n" in raw_target or "\r" in raw_target:
        return None, "target_dir に不正な制御文字が含まれています"

    clean_target = raw_target.strip()
    candidate_input = Path(clean_target)
    if candidate_input.is_absolute():
        return None, "target_dir はプロジェクトルートからの相対パスで指定してください"

    resolved_root = project_root.resolve()
    try:
        resolved_dir = (resolved_root / candidate_input).resolve()
    except OSError:
        return None, f"対象ディレクトリパスの解決に失敗しました: {clean_target}"

    if not resolved_dir.is_relative_to(resolved_root):
        return None, f"プロジェクト外のディレクトリは階層整理対象に指定できません: {clean_target}"
    if not resolved_dir.is_dir():
        return None, f"階層整理対象ディレクトリが存在しません: {clean_target}"

    rel_dir = "." if resolved_dir == resolved_root else resolved_dir.relative_to(resolved_root).as_posix()
    meta_max_files, meta_excludes = load_organize_config_from_meta(resolved_root, output_dir)
    default_max_files = _resolve_positive_threshold(meta_max_files, DEFAULT_ORGANIZE_MAX_FILES)

    direct_files = _collect_direct_files_in_dir(resolved_root, resolved_dir, meta_excludes)
    max_files = _parse_positive_int(payload.get("max_files"), default_max_files)
    total_dirs = _parse_positive_int(payload.get("total_dirs"), 1)
    dir_index = max(1, min(total_dirs, _parse_positive_int(payload.get("dir_index"), 1)))
    total_target_files = _parse_positive_int(payload.get("total_target_files"), max(1, len(direct_files)))
    batch_index = _parse_positive_int(payload.get("batch_index"), 1)
    allow_rename = bool(payload.get("allow_rename"))

    return {
        "rel_dir": rel_dir,
        "file_count": len(direct_files),
        "direct_files": direct_files,
        "max_files": max_files,
        "dir_index": dir_index,
        "total_dirs": total_dirs,
        "total_target_files": total_target_files,
        "batch_index": batch_index,
        "allow_rename": allow_rename,
    }, None

def _format_target_dir_files_section(rel_dir: str, direct_files: List[Tuple[str, int, int]]) -> str:
    prefix = "" if rel_dir == "." else f"{rel_dir}/"
    lines = [f"=== 整理対象ディレクトリ直下のファイル一覧 ({rel_dir}: {len(direct_files)} ファイル) ==="]
    for name, line_count, char_count in direct_files:
        lines.append(f"- {prefix}{name} ({line_count} lines, {char_count:,} chars)")
    return "\n".join(lines)

def build_organize_combined_text(
    project_root: Path,
    target_info: Dict[str, Any],
    summary_text: str,
) -> str:
    """対象ディレクトリ1件の階層整理指示プロンプト・最新要約バンドル・直下ファイル一覧を統合する。"""
    rel_dir = str(target_info["rel_dir"])
    allow_rename = bool(target_info.get("allow_rename"))
    default_prompt = load_default_prompt(project_root)
    organize_prompt = build_dir_organize_prompt(
        default_prompt=default_prompt,
        target_rel_dir=rel_dir,
        file_count=int(target_info["file_count"]),
        max_files=int(target_info["max_files"]),
        allow_rename=allow_rename,
        dir_index=int(target_info["dir_index"]),
        total_dirs=int(target_info["total_dirs"]),
        total_target_files=int(target_info["total_target_files"]),
        batch_index=int(target_info.get("batch_index") or 1),
        project_root=project_root,
    )
    prompt_and_summary = format_summary_prompt_text(organize_prompt, summary_text)
    files_section = _format_target_dir_files_section(rel_dir, list(target_info.get("direct_files") or []))
    if prompt_and_summary and files_section:
        return f"{prompt_and_summary}\n\n{files_section}"
    return prompt_and_summary or files_section

def validate_patch_against_rename_policy(
    patch_text: str,
    project_root: Path,
    *,
    allow_rename: bool,
) -> Optional[Tuple[str, str]]:
    """ファイル名変更不許可モード時に、パッチ内の移動・名前変更がbasenameを変えていないか検証する。"""
    if allow_rename:
        return None
    resolved_root = project_root.resolve()
    chunks, _, _ = parse_patch_chunks(patch_text, resolved_root)
    violations: List[str] = []
    for chunk in chunks:
        if not chunk.is_file_move or chunk.file_path is None or chunk.move_target_path is None:
            continue
        if chunk.file_path.name == chunk.move_target_path.name:
            continue
        try:
            src_disp = chunk.file_path.relative_to(resolved_root).as_posix()
        except ValueError:
            src_disp = str(chunk.file_path)
        try:
            dst_disp = chunk.move_target_path.relative_to(resolved_root).as_posix()
        except ValueError:
            dst_disp = str(chunk.move_target_path)
        violations.append(f"- 行 {chunk.block_line_no}: {src_disp} -> {dst_disp}")

    if not violations:
        return None

    short_msg = f"ファイル名変更不許可モード違反: {len(violations)}件のリネームが含まれています"
    report = (
        "階層整理モードでは「ファイル名変更を許可」が無効（ディレクトリ移動のみ許可）です。\n"
        "以下の箇所でファイル名（basename）が変更されているため検証を拒否しました:\n"
        + "\n".join(violations)
        + "\n\n元のファイル名を一切変更せず、サブディレクトリへの移動のみを行う完全版パッチを再提示してください。"
    )
    return short_msg, report
