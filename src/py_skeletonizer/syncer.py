# src/py_skeletonizer/syncer.py
"""
Role: プロジェクトの同期、各辞書マニュアルの作成、バンドル出力、およびトークン削減率の計算を統合制御する。
"""
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from .config import SkeletonConfig
from .ast_processor import process_code_all_in_one
from .role_extractor import RoleEntry, extract_roles_from_ast, generate_role_map_text
from .dependency_analyzer import DependencyEntry, extract_dependencies_from_ast, generate_dependency_map_text
from .bundle_builder import TokenStats, build_bundle_file
from .token_counter import estimate_tokens


class ProjectSyncer:
    def __init__(self, project_root: Path, output_dir: Path, config: SkeletonConfig):
        self.project_root = project_root
        self.output_dir = output_dir
        self.config = config
        self.all_role_entries: List[RoleEntry] = []
        self.all_dependency_entries: List[DependencyEntry] = []
        self.file_contents_map: Dict[str, str] = {}
        self.token_stats = TokenStats()
        # メタデータファイルを隔離格納する専用フォルダパスの定義
        self.meta_dir = output_dir / "ai_meta"

    def _is_outdated(self, src_file: Path, dest_file: Path) -> bool:
        if not dest_file.exists():
            return True
        return os.path.getmtime(src_file) > os.path.getmtime(dest_file)

    def clean_deleted_files(self, current_target_files: List[Path]) -> int:
        if not self.output_dir.exists():
            return 0

        valid_dest_files = {
            self.output_dir / f.relative_to(self.project_root) for f in current_target_files
        }
        
        # 隔離フォルダ内のメタデータファイルを削除保護対象リストに追加 (.md -> .txtに変更)
        valid_dest_files.add(self.meta_dir / "project_tree.txt")
        valid_dest_files.add(self.meta_dir / "project_roles.txt")
        valid_dest_files.add(self.meta_dir / "project_dependencies.txt")
        valid_dest_files.add(self.meta_dir / "ai_context_bundle.txt")
        valid_dest_files.add(self.meta_dir / "static_skeleton.txt")
        valid_dest_files.add(self.meta_dir / "ai_architecture_bundle.txt")

        deleted_count = 0
        for root, _, files in os.walk(self.output_dir):
            for file in files:
                dest_path = Path(root) / file
                if dest_path not in valid_dest_files:
                    try:
                        dest_path.unlink()
                        deleted_count += 1
                    except OSError as e:
                        raise RuntimeError(f"古いファイルの削除に失敗しました: {dest_path} ({e})")

        for root, dirs, _ in os.walk(self.output_dir, topdown=False):
            for d in dirs:
                dir_path = Path(root) / d
                try:
                    # ai_metaフォルダ自体は中身が空であってもここでは削除しないよう保護
                    if dir_path == self.meta_dir:
                        continue
                    if not any(dir_path.iterdir()):
                        dir_path.rmdir()
                except OSError:
                    pass

        return deleted_count

    def _read_text_safely(self, file_path: Path) -> Optional[str]:
        encodings = ("utf-8-sig", "cp932", "utf-8")
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    content = f.read()
                    return content.lstrip("\ufeff")
            except (UnicodeDecodeError, OSError):
                continue
        return None

    def _read_and_analyze_only(self, src_file: Path, rel_path: str) -> str:
        content = self._read_text_safely(src_file)
        if content is None:
            return ""
        try:
            import ast
            tree = ast.parse(content)
            self.all_role_entries.extend(extract_roles_from_ast(tree, rel_path, content))
            self.all_dependency_entries.append(extract_dependencies_from_ast(tree, rel_path))
            return content
        except Exception:
            return ""

    def sync_files(self, target_files: List[Path], tree_text: str, force_rebuild: bool = False) -> Tuple[int, int, Optional[Path]]:
        updated_count = 0
        skipped_count = 0
        self.all_role_entries.clear()
        self.all_dependency_entries.clear()
        self.file_contents_map.clear()
        self.token_stats = TokenStats()

        for src_file in target_files:
            rel_path = src_file.relative_to(self.project_root).as_posix()
            dest_file = self.output_dir / rel_path

            raw_content = self._read_text_safely(src_file)
            is_binary = raw_content is None

            if not is_binary and raw_content is not None:
                self.token_stats.raw_chars += len(raw_content)
                self.token_stats.raw_tokens += estimate_tokens(raw_content)

            if not force_rebuild and not self._is_outdated(src_file, dest_file):
                skipped_count += 1
                if src_file.suffix == ".py" and not is_binary:
                    self._read_and_analyze_only(src_file, rel_path)
                
                if not is_binary:
                    dest_content = self._read_text_safely(dest_file)
                    if dest_content is not None:
                        self.file_contents_map[rel_path] = dest_content
                        self.token_stats.skeleton_chars += len(dest_content)
                        self.token_stats.skeleton_tokens += estimate_tokens(dest_content)
                continue

            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if is_binary or self.config.is_full_code_path(src_file) or src_file.suffix != ".py":
                shutil.copy2(src_file, dest_file)
                updated_count += 1
                
                if not is_binary and raw_content is not None:
                    self.file_contents_map[rel_path] = raw_content
                    self.token_stats.skeleton_chars += len(raw_content)
                    self.token_stats.skeleton_tokens += estimate_tokens(raw_content)
                    if src_file.suffix == ".py":
                        self._read_and_analyze_only(src_file, rel_path)
                continue

            try:
                assert raw_content is not None
                skeleton_code, roles, dependency = process_code_all_in_one(
                    raw_content, rel_path, keep_functions=self.config.keep_functions, only_nodes=self.config.only_nodes
                )
                self.all_role_entries.extend(roles)
                self.all_dependency_entries.append(dependency)
                self.file_contents_map[rel_path] = skeleton_code
                self.token_stats.skeleton_chars += len(skeleton_code)
                self.token_stats.skeleton_tokens += estimate_tokens(skeleton_code)

                with open(dest_file, "w", encoding="utf-8") as f:
                    f.write(skeleton_code)

                shutil.copystat(src_file, dest_file)
                updated_count += 1
            except Exception as e:
                raise RuntimeError(f"ファイル処理中にエラーが発生しました: {src_file} ({e})")

        # ai_meta フォルダを確実に作成
        self.meta_dir.mkdir(parents=True, exist_ok=True)

        # ファイル名を .md から .txt に変更
        role_map_text = generate_role_map_text(self.all_role_entries)
        role_file = self.meta_dir / "project_roles.txt"
        role_file.write_text(role_map_text, encoding="utf-8")

        dependency_map_text = generate_dependency_map_text(self.all_dependency_entries)
        dep_file = self.meta_dir / "project_dependencies.txt"
        dep_file.write_text(dependency_map_text, encoding="utf-8")

        bundle_path: Optional[Path] = None
        if self.config.create_bundle:
            # バンドル出力先を隔離フォルダ(self.meta_dir)へと移譲
            bundle_path = build_bundle_file(
                project_root=self.project_root,
                output_dir=self.meta_dir,
                tree_text=tree_text,
                role_map_text=role_map_text,
                dependency_map_text=dependency_map_text,
                file_contents=self.file_contents_map,
                bundle_format=self.config.bundle_format,
                custom_policy_path=self.config.policy_path,
            )

        return updated_count, skipped_count, bundle_path