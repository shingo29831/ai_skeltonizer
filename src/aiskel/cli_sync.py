"""Module: @role: プロジェクトのスケルトン同期、出力先パス解決、.gitignore自動更新、および自動同期トリガーを担当する。"""
import sys
from pathlib import Path
from typing import Optional, Set, Tuple

from .config import (
    SkeletonConfig,
    ensure_default_config_file,
    load_bloat_config_from_meta,
    load_bundle_filename_from_meta,
)
from .core.bloat_detector import (
    DEFAULT_BLOAT_MAX_CHARS,
    DEFAULT_BLOAT_MAX_LINES,
    check_and_warn_bloated_files,
)
from .core.git_diff_analyzer import get_staged_or_modified_files, parse_direct_dependencies
from .core.layer_filter import filter_logic_files
from .core.scanner import generate_tree_text, get_target_files
from .core.syncer import ProjectSyncer
from .core.token_counter import estimate_tokens


def resolve_output_dir(project_root: Path, custom_output_dir: Optional[Path]) -> Path:
    if custom_output_dir is not None:
        return custom_output_dir.resolve()
    return project_root / "ai_meta"


def ensure_gitignore_updated(project_root: Path, output_dir: Path) -> None:
    try:
        try:
            rel_path = output_dir.relative_to(project_root).as_posix()
        except ValueError:
            return

        if rel_path == ".":
            return

        gitignore_path = project_root / ".gitignore"
        ignore_entry = f"{rel_path}/"

        if gitignore_path.exists():
            content = gitignore_path.read_text(encoding="utf-8")
            lines = [line.strip() for line in content.splitlines()]
            if rel_path in lines or ignore_entry in lines:
                return

            with gitignore_path.open("a", encoding="utf-8") as f:
                if content and not content.endswith("\n"):
                    f.write("\n")
                f.write(f"\n# aiskel output directory\n{ignore_entry}\n")
        else:
            gitignore_path.write_text(f"# aiskel output directory\n{ignore_entry}\n", encoding="utf-8")

    except Exception as e:
        print(f"⚠ .gitignore の更新に失敗しました: {e}", file=sys.stderr)


def run_sync(
    project_root: Path,
    output_dir: Path,
    config: SkeletonConfig,
    force: bool = False,
    focus_paths: Optional[Set[str]] = None,
    focus_deps: bool = False,
    git_diff: bool = False,
    no_ui: bool = False,
    quiet: bool = False,
    check_bloat: bool = True,
) -> Tuple[int, int, int, Optional[Path]]:
    ensure_gitignore_updated(project_root, output_dir)
    created_cfg = ensure_default_config_file(output_dir, project_root)
    if created_cfg and not quiet:
        try:
            rel_cfg = created_cfg.relative_to(project_root)
        except ValueError:
            rel_cfg = created_cfg
        print(f"✨ 設定ファイルを自動作成しました: {rel_cfg}")
    if not config.bundle_filename:
        config.bundle_filename = load_bundle_filename_from_meta(project_root, output_dir)
    target_files = get_target_files(project_root)

    if focus_paths:
        resolved_focus_files = set()
        for p_str in focus_paths:
            p = (project_root / Path(p_str)).resolve() if not Path(p_str).is_absolute() else Path(p_str).resolve()
            if p.is_dir():
                resolved_focus_files.update({tf for tf in target_files if p in tf.parents or p == tf.parent})
            elif p in target_files:
                resolved_focus_files.add(p)
        if focus_deps:
            resolved_focus_files = parse_direct_dependencies(resolved_focus_files, set(target_files))
        target_files = list(resolved_focus_files)

    if git_diff:
        modified_files = get_staged_or_modified_files(project_root)
        if modified_files:
            target_files = list(parse_direct_dependencies(modified_files, set(target_files)))

    if no_ui:
        target_files = filter_logic_files(target_files)

    tree_text = generate_tree_text(project_root, target_files)
    syncer = ProjectSyncer(project_root, output_dir, config)
    deleted_count = syncer.clean_deleted_files(target_files)
    updated_count, skipped_count, bundle_path = syncer.sync_files(target_files, tree_text=tree_text, force_rebuild=force)

    if not quiet:
        stats = syncer.token_stats
        print("\n=== 同期およびコンテキスト最適化完了 ===")
        print(f"出力先ディレクトリ: {output_dir}")
        print(f"  - 更新/処理ファイル数 : {updated_count} 件")
        print(f"  - 変更なし(スキップ)   : {skipped_count} 件")
        if deleted_count > 0:
            print(f"  - 削除した古いファイル: {deleted_count} 件")
        print("\n--- 辞書・マニュアル出力 ---")
        if bundle_path and bundle_path.exists():
            print(f"  - アーキテクチャ要素: {bundle_path.relative_to(project_root)} (変更対象ファイルの特定用)")
        cfg_file = output_dir / "config.json"
        if cfg_file.exists():
            try:
                rel_cfg = cfg_file.relative_to(project_root)
            except ValueError:
                rel_cfg = cfg_file
            print(f"  - 検証設定ファイル  : {rel_cfg} (適用後テスト実行用)")

        print("\n--- トークン・予算削減アナライザー ---")
        print(f"  - 元のフルコード総量 : 約 {stats.raw_tokens:,} tokens")
        if bundle_path and bundle_path.exists():
            arch_tokens = estimate_tokens(bundle_path.read_text(encoding="utf-8"))
            arch_red = (1.0 - (arch_tokens / max(stats.raw_tokens, 1))) * 100
            print(f"  - アーキテクチャ要素: 約 {arch_tokens:,} tokens ({arch_red:.1f}% 削減)")
        else:
            print(f"  - 削減トークン数 : 約 {stats.saved_tokens:,} tokens ({stats.reduction_percentage:.1f}% 削減)")

        if check_bloat:
            meta_l, meta_c, meta_ex = load_bloat_config_from_meta(project_root, output_dir)
            check_and_warn_bloated_files(
                project_root,
                max_lines=meta_l or DEFAULT_BLOAT_MAX_LINES,
                max_chars=meta_c or DEFAULT_BLOAT_MAX_CHARS,
                exclude_paths=meta_ex,
            )

    return updated_count, skipped_count, deleted_count, bundle_path


def trigger_auto_sync(project_root: Path, action_label: str, check_bloat: bool = True) -> None:
    # なぜ必要か: コード変更コマンド実行後に最新のスケルトンとバンドル状態を完全同期・担保するため
    output_dir = resolve_output_dir(project_root, None)
    sync_config = SkeletonConfig(
        full_code_paths=set(),
        keep_functions=set(),
        only_nodes=set(),
        create_bundle=True,
        bundle_format="txt",
        policy_path=None,
    )
    print(f"\n🔄 {action_label}完了に伴い、スケルトンとバンドルを自動同期します...")
    run_sync(project_root, output_dir, sync_config, quiet=False, check_bloat=check_bloat)


_resolve_output_dir = resolve_output_dir
_ensure_gitignore_updated = ensure_gitignore_updated
_run_sync = run_sync
_trigger_auto_sync = trigger_auto_sync
