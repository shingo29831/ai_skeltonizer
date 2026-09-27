import ast
import os
from pathlib import Path
from typing import List, Optional
import pathspec

# なぜ必要か: サードパーティ製ライブラリ・肥大化した中間生成物がトークン消費と解析時間を圧迫するのを防止
EXCLUDED_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ipynb_checkpoints",
    "venv",
    ".venv",
    ".env",
    "node_modules",
    "vendor",
    "third_party",
    "submodules",
}

# なぜ必要か: アーキテクチャ解析に不要な非コードアセット・データセット・重みファイルを走査から除外
EXCLUDED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".ico", ".pt", ".onnx", ".pth", ".bin", ".dat",
    ".xml", ".csv", ".tsv", ".pdf",
}

EXCLUDED_FILENAMES = {
    ".gitmodules",
    ".DS_Store",
    "Thumbs.db",
}


def _build_pathspec(project_root: Path) -> pathspec.PathSpec:
    lines = [f"{d}/" for d in EXCLUDED_DIR_NAMES]
    lines.extend([f"*{ext}" for ext in EXCLUDED_EXTENSIONS])
    lines.extend(EXCLUDED_FILENAMES)

    gitignore_path = project_root / ".gitignore"
    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                lines.extend(f.readlines())
        except IOError as e:
            raise RuntimeError(f"Failed to read .gitignore: {e}")

    return pathspec.PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, lines)


def get_target_files(project_root: Path) -> List[Path]:
    """pathspecを利用し、無視リストを除外した処理対象ファイルのリストを取得する"""
    if not project_root.exists() or not project_root.is_dir():
        raise FileNotFoundError(f"Target directory not found: {project_root}")

    spec = _build_pathspec(project_root)
    target_files = []

    for root, dirs, files in os.walk(project_root):
        current_dir = Path(root)

        # なぜ必要か: os.walk の探索対象をインプレースで削り、不要ディレクトリの深層走査コストをゼロにする
        dirs[:] = [
            d for d in dirs
            if d not in EXCLUDED_DIR_NAMES
            and not spec.match_file((current_dir / d).relative_to(project_root).as_posix() + "/")
        ]

        for file in files:
            if file in EXCLUDED_FILENAMES:
                continue
            file_path = current_dir / file
            if file_path.suffix.lower() in EXCLUDED_EXTENSIONS:
                continue
            rel_path = file_path.relative_to(project_root).as_posix()
            if not spec.match_file(rel_path):
                target_files.append(file_path)

    return target_files


# なぜ必要か: パッケージレイヤーの責務境界を把握できるよう__init__.pyのdocstring/役割コメントを抽出
def _extract_dir_role(dir_path: Path) -> str:
    init_file = dir_path / "__init__.py"
    if not init_file.exists() or not init_file.is_file():
        return ""
    try:
        content = init_file.read_text(encoding="utf-8", errors="ignore").strip()
        if not content:
            return ""
        tree = ast.parse(content)
        doc = ast.get_docstring(tree)
        if doc:
            first_line = doc.strip().splitlines()[0].strip()
            for prefix in ["@role:", "Role:", "役割:", "Module:"]:
                if first_line.lower().startswith(prefix.lower()):
                    first_line = first_line[len(prefix):].strip()
            return first_line.strip()

        for line in content.splitlines():
            line_s = line.strip()
            if line_s.startswith("#"):
                comment = line_s.lstrip("#").strip()
                if comment and not comment.startswith("-*-") and not comment.startswith("coding"):
                    for prefix in ["@role:", "Role:", "役割:", "Module:"]:
                        if comment.lower().startswith(prefix.lower()):
                            comment = comment[len(prefix):].strip()
                    return comment.strip()
            elif line_s:
                break
    except Exception:
        pass
    return ""


class _TreeNode:
    def __init__(self, name: str, is_dir: bool, role_description: str = ""):
        self.name = name
        self.is_dir = is_dir
        self.role_description = role_description
        self.children: dict[str, "_TreeNode"] = {}


def _render_tree_node(node: _TreeNode, prefix: str = "") -> List[str]:
    lines = []
    # なぜ必要か: ディレクトリを先行配置して標準的なツリー階層の視認性を向上させる
    sorted_children = sorted(
        node.children.values(),
        key=lambda c: (not c.is_dir, c.name.lower())
    )
    total = len(sorted_children)
    for i, child in enumerate(sorted_children):
        is_last = (i == total - 1)
        connector = "└── " if is_last else "├── "
        child_label = f"{child.name}/" if child.is_dir else child.name

        # なぜ必要か: レイヤー責務をツリー行末へコメント形式で注釈し、アーキテクチャ境界の暗黙化を防止
        role_annotation = f"        # {child.role_description}" if child.is_dir and child.role_description else ""
        lines.append(f"{prefix}{connector}{child_label}{role_annotation}")

        if child.is_dir:
            extension = "    " if is_last else "│   "
            lines.extend(_render_tree_node(child, prefix + extension))
    return lines


# なぜ必要か: 中間ディレクトリ名が欠落してインデントのみ深くなる表示バグを根本解決するため木構造から生成
def generate_tree_text(project_root: Path, target_files: List[Path]) -> str:
    root_node = _TreeNode(project_root.name, is_dir=True)

    for file_path in target_files:
        rel_path = file_path.relative_to(project_root)
        current = root_node
        parts = rel_path.parts
        for idx, part in enumerate(parts[:-1]):
            if part not in current.children:
                dir_path = project_root / Path(*parts[:idx + 1])
                role_desc = _extract_dir_role(dir_path)
                current.children[part] = _TreeNode(part, is_dir=True, role_description=role_desc)
            current = current.children[part]
        filename = parts[-1]
        current.children[filename] = _TreeNode(filename, is_dir=False)

    lines = [f"{project_root.name}/"]
    lines.extend(_render_tree_node(root_node))
    return "\n".join(lines)
