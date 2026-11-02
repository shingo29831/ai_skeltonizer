"""Module: @role: ファイルの行数・文字数計測、肥大化ノード検出、およびAI向け要約レポート整形を担当する。"""
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set

from .scanner import get_target_files

DEFAULT_BLOAT_MAX_LINES = 500
DEFAULT_BLOAT_MAX_CHARS = 25000


@dataclass
class BloatFileInfo:
    path: Path
    rel_path: str
    lines: int
    chars: int
    exceeds_lines: bool
    exceeds_chars: bool


def is_path_excluded(file_path: Path, project_root: Path, exclude_patterns: Set[str]) -> bool:
    """指定ファイルパスが設定または引数で指定された除外パターンに合致するか判定する"""
    if not exclude_patterns:
        return False
    try:
        rel = file_path.relative_to(project_root).as_posix()
    except ValueError:
        rel = file_path.as_posix()

    rel_lower = rel.lower()
    for pat in exclude_patterns:
        p = pat.strip().replace("\\", "/").rstrip("/")
        if not p:
            continue
        p_lower = p.lower()
        if rel_lower == p_lower or rel_lower.startswith(p_lower + "/"):
            return True
        if fnmatch.fnmatch(rel_lower, p_lower) or fnmatch.fnmatch(file_path.name.lower(), p_lower):
            return True
    return False


def count_file_metrics(file_path: Path) -> Optional[tuple[int, int]]:
    """ファイルの行数および文字数を計測する。読込不可ファイルは安全にNoneを返す。"""
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # なぜ必要か: UTF-8以外の既存コードも文字化けクラッシュせず計測するため
        try:
            content = file_path.read_text(encoding="latin-1")
        except Exception:
            return None
    except OSError:
        return None

    lines = len(content.splitlines())
    chars = len(content)
    return lines, chars


def detect_bloated_files(
    project_root: Path,
    target_files: Optional[List[Path]] = None,
    max_lines: int = DEFAULT_BLOAT_MAX_LINES,
    max_chars: int = DEFAULT_BLOAT_MAX_CHARS,
    exclude_paths: Optional[Set[str]] = None,
) -> List[BloatFileInfo]:
    """設定値以上の行数または文字数を持つ肥大化ファイルを検出する"""
    if target_files is None:
        target_files = get_target_files(project_root)

    excludes = exclude_paths or set()
    bloated: List[BloatFileInfo] = []

    for f in target_files:
        if is_path_excluded(f, project_root, excludes):
            continue

        metrics = count_file_metrics(f)
        if metrics is None:
            continue

        lines, chars = metrics
        exceeds_lines = lines >= max_lines
        exceeds_chars = chars >= max_chars

        if exceeds_lines or exceeds_chars:
            try:
                rel = f.relative_to(project_root).as_posix()
            except ValueError:
                rel = f.as_posix()

            bloated.append(
                BloatFileInfo(
                    path=f,
                    rel_path=rel,
                    lines=lines,
                    chars=chars,
                    exceeds_lines=exceeds_lines,
                    exceeds_chars=exceeds_chars,
                )
            )

    # なぜ必要か: より重篤な肥大化ファイルから優先して分割対処させるため
    bloated.sort(key=lambda x: (x.lines / max(max_lines, 1) + x.chars / max(max_chars, 1)), reverse=True)
    return bloated


def format_bloat_report(bloated_files: List[BloatFileInfo], max_lines: int, max_chars: int) -> str:
    """AI向けに最小トークンで分割指示を伝えるプロンプトテキストを生成する"""
    if not bloated_files:
        return ""

    lines = [
        "# Bloated Files Refactoring Request",
        f"Thresholds: lines >= {max_lines} | chars >= {max_chars:,}",
        "Instruction: Split the following bloated files into modular, single-responsibility files according to architectural conventions.\n",
    ]

    for item in bloated_files:
        reasons = []
        if item.exceeds_lines:
            reasons.append("lines")
        if item.exceeds_chars:
            reasons.append("chars")
        reason_str = "+".join(reasons)
        lines.append(f"- {item.rel_path}: {item.lines} lines, {item.chars:,} chars ({reason_str})")

    return "\n".join(lines)


def check_and_warn_bloated_files(
    project_root: Path,
    max_lines: int = DEFAULT_BLOAT_MAX_LINES,
    max_chars: int = DEFAULT_BLOAT_MAX_CHARS,
    exclude_paths: Optional[Set[str]] = None,
) -> List[BloatFileInfo]:
    """パッチ適用後等に肥大化ファイルを検査し、検出時のみコンソールに警告を出力する(クリップボード更新なし)"""
    bloated = detect_bloated_files(
        project_root=project_root,
        max_lines=max_lines,
        max_chars=max_chars,
        exclude_paths=exclude_paths,
    )

    if bloated:
        print(f"\n⚠ 肥大化ファイルを検出しました ({len(bloated)}件, 行数 >= {max_lines} または 文字数 >= {max_chars:,}):")
        for item in bloated:
            reasons = []
            if item.exceeds_lines:
                reasons.append("lines")
            if item.exceeds_chars:
                reasons.append("chars")
            reason_str = "+".join(reasons)
            print(f"  - {item.rel_path}: {item.lines} lines, {item.chars:,} chars ({reason_str})")
        print("  (AIへの分割指示テキスト生成・コピーは 'aiskel bloat' を実行してください)")

    return bloated
