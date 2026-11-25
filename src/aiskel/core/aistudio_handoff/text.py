"""Module: @role: パッチ・停止合図のテキスト検出、引き継ぎ用プロンプト整形、および要約バンドル読込を担当する。"""
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

from ...config import SkeletonConfig, load_bundle_filename_from_meta
from ..aistudio_prompts import PATCH_HANDOFF_STOP_SIGNAL
from ..patch_parser import DELIMITER_END, DELIMITER_MID, DELIMITER_START, MOVE_HEADER_PATTERN, has_patch_delimiters, is_file_move_patch

_DEFAULT_BUNDLE_FILENAMES = (
    "architecture_bundle.txt",
    "architecture_bundle.md",
    "architecture_bundle.xml",
)
_COMMIT_TAG_OPEN = "<" + "commit>"
_PATCH_STRUCTURE_HINT_RE = re.compile(
    r"^[ \t]*(?:ファイルパス|File(?:\s+path)?|Path|削除|移動|名前変更|リネーム|ファイル移動|move|mv|rename)[ \t]*[:：]|^[ \t]*"
    + re.escape(_COMMIT_TAG_OPEN)
    + r"[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_STOP_SIGNAL_LINE_RE = re.compile(
    r"^[ \t]*`?" + re.escape(PATCH_HANDOFF_STOP_SIGNAL) + r"`?[ \t。.]*$"
)

def _find_valid_patch_line_indices(lines: List[str]) -> Set[int]:
    excluded: Set[int] = set()
    total = len(lines)
    i = 0
    while i < total:
        if DELIMITER_START.match(lines[i]):
            has_mid = False
            end_idx = -1
            for j in range(i + 1, total):
                if DELIMITER_START.match(lines[j]):
                    break
                if DELIMITER_MID.match(lines[j]):
                    has_mid = True
                    continue
                if DELIMITER_END.match(lines[j]):
                    if has_mid:
                        end_idx = j
                    break
            if end_idx != -1:
                excluded.update(range(i, end_idx + 1))
                i = end_idx + 1
                continue
        i += 1
    return excluded

def _find_excluded_stop_signal_line_indices(lines: List[str]) -> Set[int]:
    # パッチ内自己言及やコードブロック内例示による誤停止を防ぐため対象範囲を除外
    excluded = _find_valid_patch_line_indices(lines)
    active_fence: Optional[Tuple[str, int]] = None
    for idx, line in enumerate(lines):
        if active_fence is not None:
            excluded.add(idx)
            fence_char, fence_len = active_fence
            stripped = line.strip()
            if len(stripped) >= fence_len and set(stripped) == {fence_char}:
                active_fence = None
            continue
        match = _FENCE_OPEN_RE.match(line)
        if match:
            marker, info = match.group(1), match.group(2)
            if marker[0] == "~" or "`" not in info:
                active_fence = (marker[0], len(marker))
                excluded.add(idx)
    return excluded

def has_patch_delimiters_in_text(text: str) -> bool:
    """テキスト内にaiskelパッチ形式（<<<< ==== >>>> の順序ペアとヘッダ/commit構造）が含まれるか判定する。"""
    if not text or not text.strip() or not has_patch_delimiters(text):
        return False
    if not _PATCH_STRUCTURE_HINT_RE.search(text):
        return False
    return bool(_find_valid_patch_line_indices(text.splitlines()))

def has_move_patch_in_text(text: str) -> bool:
    """移動・名前変更ディレクティブのみのパッチブロック(コードブロック内またはcommitタグ付き)が含まれるか判定する。"""
    if not text or not text.strip() or not is_file_move_patch(text):
        return False
    if _COMMIT_TAG_OPEN in text:
        return True
    active_fence: Optional[Tuple[str, int]] = None
    for line in text.splitlines():
        if active_fence is not None:
            fence_char, fence_len = active_fence
            stripped = line.strip()
            if len(stripped) >= fence_len and set(stripped) == {fence_char}:
                active_fence = None
                continue
            if MOVE_HEADER_PATTERN.match(line):
                return True
            continue
        match = _FENCE_OPEN_RE.match(line)
        if match:
            marker, info = match.group(1), match.group(2)
            if marker[0] == "~" or "`" not in info:
                active_fence = (marker[0], len(marker))
    return False

def has_applicable_patch_in_text(text: str) -> bool:
    """置換ブロックまたは移動・名前変更パッチブロックが含まれるか判定する。"""
    return has_patch_delimiters_in_text(text) or has_move_patch_in_text(text)

def has_handoff_stop_signal_in_text(text: str) -> bool:
    """応答テキストのコードブロック・パッチ外に新規タブ作成停止合図が含まれるか判定する。"""
    if not text or not text.strip() or PATCH_HANDOFF_STOP_SIGNAL not in text:
        return False
    lines = text.splitlines()
    excluded_indices = _find_excluded_stop_signal_line_indices(lines)
    for idx, line in enumerate(lines):
        if idx in excluded_indices:
            continue
        if _STOP_SIGNAL_LINE_RE.match(line):
            return True
    return False

def _wrap_proposed_implementation(resp: str) -> str:
    # 応答内の同名閉じタグによる境界破壊を防ぐため衝突時は動的タグ名へ切り替える
    tag = "proposed_implementation"
    if re.search(rf"</?{tag}\b", resp, re.IGNORECASE):
        idx = 1
        while re.search(rf"</?{tag}_{idx}\b", resp, re.IGNORECASE):
            idx += 1
        tag = f"{tag}_{idx}"
    return f"<{tag}>\n{resp}\n</{tag}>"

def _format_check_error_section(check_err: str) -> str:
    return (
        "=== 事前検証結果 (aiskel a --check エラー) ===\n"
        "上記の提案実装に対して `aiskel a --check` を事前実行したところ、以下のエラーが検出されました。\n"
        "通常のレビュー観点とあわせてこの適用・構文エラーの原因も必ず解消し、"
        "未適用の現行コードベースへ単体でそのまま `aiskel a` 適用できる完全版パッチを提示してください。\n\n"
        f"{check_err}"
    )

def format_handoff_prompt_text(
    response_text: str,
    summary_text: str,
    review_prompt: Optional[str] = None,
    check_error: Optional[str] = None,
) -> str:
    """コピーしたAI応答・要約バンドル・任意のレビュー指示・事前checkエラーを新規タブ貼り付け用テキストへ統合する。"""
    resp = (response_text or "").strip()
    summary = (summary_text or "").strip()
    check_err = (check_error or "").strip()
    if not resp and not summary and not check_err:
        return ""
    prompt = (review_prompt or "").strip()
    if not prompt:
        parts = [part for part in (resp, _format_check_error_section(check_err) if check_err else "", summary) if part]
        return "\n\n".join(parts)
    sections = [prompt]
    if resp:
        sections.append(_wrap_proposed_implementation(resp))
    if check_err:
        sections.append(_format_check_error_section(check_err))
    if summary:
        sections.append(summary)
    return "\n\n".join(sections)

def format_summary_prompt_text(prompt_text: str, summary_text: str) -> str:
    """プロンプト指示とプロジェクト要約バンドルをチャット欄挿入用テキストへ統合する。"""
    prompt = (prompt_text or "").strip()
    summary = (summary_text or "").strip()
    if prompt and summary:
        return f"{prompt}\n\n{summary}"
    return prompt or summary

def _find_existing_bundle_file(project_root: Path, output_dir: Path) -> Optional[Path]:
    configured = load_bundle_filename_from_meta(project_root, output_dir)
    if configured:
        candidate = output_dir / configured
        if candidate.is_file():
            return candidate
        if configured != _DEFAULT_BUNDLE_FILENAMES[0]:
            return None
    for name in _DEFAULT_BUNDLE_FILENAMES:
        candidate = output_dir / name
        if candidate.is_file():
            return candidate
    return None

def load_project_summary_text(project_root: Path) -> str:
    """対象プロジェクトを差分同期し、最新の要約バンドル文字列を取得する。"""
    # 循環インポート回避のためcli_syncを遅延参照し、.gitignore更新・config初期化・最新バンドル生成を統一実行
    from ...cli_sync import resolve_output_dir, run_sync

    resolved_root = project_root.resolve()
    output_dir = resolve_output_dir(resolved_root, None)
    configured_name = load_bundle_filename_from_meta(resolved_root, output_dir)
    config = SkeletonConfig(
        create_bundle=True,
        bundle_format="txt",
        bundle_filename=configured_name,
    )
    _, _, _, bundle_path = run_sync(
        resolved_root,
        output_dir,
        config,
        force=False,
        quiet=True,
        check_bloat=False,
    )
    if bundle_path is None or not bundle_path.is_file():
        bundle_path = _find_existing_bundle_file(resolved_root, output_dir)
    if bundle_path is None or not bundle_path.is_file():
        _, _, _, bundle_path = run_sync(
            resolved_root,
            output_dir,
            config,
            force=True,
            quiet=True,
            check_bloat=False,
        )

    if bundle_path is None or not bundle_path.is_file():
        raise FileNotFoundError("要約バンドルファイルが見つかりません")

    content = bundle_path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("要約バンドルファイルが空です")
    return content
