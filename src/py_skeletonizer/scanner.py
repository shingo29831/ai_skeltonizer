# src/py_skeletonizer/scanner.py
"""
Role: プロジェクトフォルダを走査し、Gitの仕様に準拠して無視対象をフィルタリングしつつディレクトリ構造を取得する。
"""
import os
from pathlib import Path
from typing import List
import pathspec


def _build_pathspec(project_root: Path) -> pathspec.PathSpec:
    lines = [".git/", "__pycache__/", "venv/", ".venv/", ".env"]
    gitignore_path = project_root / ".gitignore"

    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                lines.extend(f.readlines())
        except IOError as e:
            raise RuntimeError(f"Failed to read .gitignore: {e}")

    return pathspec.PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, lines)


def get_target_files(project_root: Path) -> List[Path]:
    """
    pathspecを利用し、無視リストを除外した処理対象ファイルのリストを取得する
    """
    if not project_root.exists() or not project_root.is_dir():
        raise FileNotFoundError(f"Target directory not found: {project_root}")

    spec = _build_pathspec(project_root)
    target_files = []

    for root, dirs, files in os.walk(project_root):
        current_dir = Path(root)

        # os.walkのdirsをインプレースで書き換えることで枝刈り(Prune)する
        dirs[:] = [
            d for d in dirs
            if not spec.match_file((current_dir / d).relative_to(project_root).as_posix() + "/")
        ]

        for file in files:
            file_path = current_dir / file
            rel_path = file_path.relative_to(project_root).as_posix()

            if not spec.match_file(rel_path):
                target_files.append(file_path)

    return target_files


def generate_tree_text(project_root: Path, target_files: List[Path]) -> str:
    """
    抽出されたファイルリストからツリー構造のテキストを生成する
    """
    tree_lines = [f"{project_root.name}/"]

    sorted_files = sorted([f.relative_to(project_root) for f in target_files])
    for file_path in sorted_files:
        indent = "    " * (len(file_path.parts) - 1)
        tree_lines.append(f"{indent}├── {file_path.name}")

    return "\n".join(tree_lines)