"""Module: @role: 自動分割モード向けの肥大化ファイル抽出・対象検証・プロンプト統合を担当する。"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import load_bloat_config_from_meta
from .aistudio_handoff import format_summary_prompt_text
from .aistudio_prompts import build_file_split_prompt, load_default_prompt
from .bloat_detector import (
    DEFAULT_BLOAT_MAX_CHARS,
    DEFAULT_BLOAT_MAX_LINES,
    BloatFileInfo,
    count_file_metrics,
    scan_project_files,
)

def _resolve_positive_threshold(value: Optional[int], default: int) -> int:
    if value is not None and not isinstance(value, bool) and value > 0:
        return int(value)
    return default

def collect_bloat_targets_sorted_by_size(
    project_root: Path,
    output_dir: Optional[Path] = None,
) -> Tuple[List[BloatFileInfo], int, int]:
    """aiskel b と同一基準で肥大化ファイルを検出し、ファイルサイズ(文字数→行数)降順で返す。"""
    resolved_root = project_root.resolve()
    meta_lines, meta_chars, meta_excludes = load_bloat_config_from_meta(resolved_root, output_dir)
    max_lines = _resolve_positive_threshold(meta_lines, DEFAULT_BLOAT_MAX_LINES)
    max_chars = _resolve_positive_threshold(meta_chars, DEFAULT_BLOAT_MAX_CHARS)

    bloated, _ = scan_project_files(
        project_root=resolved_root,
        max_lines=max_lines,
        max_chars=max_chars,
        exclude_paths=meta_excludes,
    )
    bloated.sort(key=lambda item: (item.chars, item.lines), reverse=True)
    return bloated, max_lines, max_chars

def format_bloat_targets_payload(
    project_root: Path,
    bloated: List[BloatFileInfo],
    max_lines: int,
    max_chars: int,
) -> Dict[str, object]:
    """検出した肥大化ファイル群をブリッジAPI応答用の辞書へ整形する。"""
    return {
        "ok": True,
        "project_root": str(project_root.resolve()),
        "max_lines": max_lines,
        "max_chars": max_chars,
        "files": [
            {
                "rel_path": item.rel_path,
                "lines": item.lines,
                "chars": item.chars,
                "exceeds_lines": item.exceeds_lines,
                "exceeds_chars": item.exceeds_chars,
            }
            for item in bloated
        ],
    }

def _parse_positive_int(raw: object, default: int) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return int(raw)
    return default

def validate_split_target_request(
    project_root: Path,
    payload: Dict[str, object],
    output_dir: Optional[Path] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """分割リクエスト内の対象ファイルパスと各種メトリクスを検証・正規化する。"""
    raw_target = payload.get("target_file")
    if not isinstance(raw_target, str) or not raw_target.strip():
        return None, "target_file を文字列で指定してください"
    if "\x00" in raw_target or "\n" in raw_target or "\r" in raw_target:
        return None, "target_file に不正な制御文字が含まれています"

    clean_target = raw_target.strip()
    candidate_input = Path(clean_target)
    if candidate_input.is_absolute():
        return None, "target_file はプロジェクトルートからの相対パスで指定してください"

    resolved_root = project_root.resolve()
    try:
        resolved_target = (resolved_root / candidate_input).resolve()
    except OSError:
        return None, f"対象パスの解決に失敗しました: {clean_target}"

    if not resolved_target.is_relative_to(resolved_root):
        return None, f"プロジェクト外のファイルは分割対象に指定できません: {clean_target}"
    if not resolved_target.is_file():
        return None, f"分割対象ファイルが存在しません: {clean_target}"

    rel_path = resolved_target.relative_to(resolved_root).as_posix()
    metrics = count_file_metrics(resolved_target)
    actual_lines, actual_chars = metrics if metrics is not None else (0, 0)

    meta_lines, meta_chars, _ = load_bloat_config_from_meta(resolved_root, output_dir)
    default_max_lines = _resolve_positive_threshold(meta_lines, DEFAULT_BLOAT_MAX_LINES)
    default_max_chars = _resolve_positive_threshold(meta_chars, DEFAULT_BLOAT_MAX_CHARS)

    lines = _parse_positive_int(payload.get("lines"), actual_lines or 1)
    chars = _parse_positive_int(payload.get("chars"), actual_chars or 1)
    max_lines = _parse_positive_int(payload.get("max_lines"), default_max_lines)
    max_chars = _parse_positive_int(payload.get("max_chars"), default_max_chars)
    total_files = _parse_positive_int(payload.get("total_files"), 1)
    file_index = max(1, min(total_files, _parse_positive_int(payload.get("file_index"), 1)))

    return {
        "rel_path": rel_path,
        "lines": lines,
        "chars": chars,
        "max_lines": max_lines,
        "max_chars": max_chars,
        "file_index": file_index,
        "total_files": total_files,
    }, None

def build_split_combined_text(
    project_root: Path,
    target_info: Dict[str, Any],
    summary_text: str,
) -> str:
    """対象ファイル1件の分割指示プロンプトと最新プロジェクト要約バンドルを統合する。"""
    default_prompt = load_default_prompt(project_root)
    split_prompt = build_file_split_prompt(
        default_prompt=default_prompt,
        target_rel_path=str(target_info["rel_path"]),
        lines=int(target_info["lines"]),
        chars=int(target_info["chars"]),
        max_lines=int(target_info["max_lines"]),
        max_chars=int(target_info["max_chars"]),
        file_index=int(target_info["file_index"]),
        total_files=int(target_info["total_files"]),
    )
    return format_summary_prompt_text(split_prompt, summary_text)
