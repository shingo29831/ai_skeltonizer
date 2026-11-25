"""Module: @role: startコマンド用の引数検証・同期オプション整合性検査・既存バンドル解決およびクリップボードコピーを担当する。"""
import argparse
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .cli_parser import process_comma_separated_args
from .config import SkeletonConfig, load_bundle_filename_from_meta
from .core.token_counter import estimate_tokens

DEFAULT_BUNDLE_FILENAMES: Tuple[str, ...] = (
    "architecture_bundle.txt",
    "architecture_bundle.md",
    "architecture_bundle.xml",
)

def resolve_existing_bundle_path(
    project_root: Path,
    output_dir: Path,
    *,
    bundle_filename_loader: Callable[[Path, Path], Optional[str]] = load_bundle_filename_from_meta,
) -> Optional[Path]:
    """設定済みまたはデフォルト候補から既存のバンドルファイルパスを解決する。"""
    configured = bundle_filename_loader(project_root, output_dir)
    if configured:
        candidate = output_dir / configured
        if candidate.is_file():
            return candidate
        # 自動生成デフォルト値と一致する場合のみ他拡張子へフォールバックし、明示的カスタム設定の誤代替を防ぐ
        if configured != DEFAULT_BUNDLE_FILENAMES[0]:
            return None
    for name in DEFAULT_BUNDLE_FILENAMES:
        candidate = output_dir / name
        if candidate.is_file():
            return candidate
    return None

def find_conflicting_sync_options(parsed_args: argparse.Namespace) -> List[str]:
    """--no-sync 指定時に併用できない同期系CLIオプションの一覧を返す。"""
    conflicts: List[str] = []
    if getattr(parsed_args, "full_path", []):
        conflicts.append("-f/--full-path")
    if getattr(parsed_args, "keep_func", []):
        conflicts.append("-k/--keep-func")
    if getattr(parsed_args, "focus", []):
        conflicts.append("--focus")
    if getattr(parsed_args, "focus_deps", False):
        conflicts.append("--focus-deps")
    if getattr(parsed_args, "only_nodes", []):
        conflicts.append("--only-nodes")
    if getattr(parsed_args, "_explicit_format", False) or getattr(parsed_args, "format", "txt") != "txt":
        conflicts.append("--format")
    if getattr(parsed_args, "policy", None) is not None:
        conflicts.append("--policy")
    if getattr(parsed_args, "force", False):
        conflicts.append("--force")
    if getattr(parsed_args, "no_ui", False):
        conflicts.append("--no-ui")
    if getattr(parsed_args, "git_diff", False):
        conflicts.append("-g/--git-diff")
    return conflicts

def validate_start_arguments(
    parsed_args: argparse.Namespace,
    project_root: Path,
    *,
    output_dir_resolver: Callable[[Path, Optional[Path]], Path],
    default_bridge_port: int,
    max_patch_handoff_max_tabs: int,
    default_aistudio_url: str,
    url_validator: Callable[[str], str],
    conflict_finder: Callable[[argparse.Namespace], List[str]] = find_conflicting_sync_options,
) -> Tuple[bool, Optional[Path], int, Optional[int], bool, str]:
    """startコマンドのディレクトリ・ポート・URL・同期オプション整合性を検証する。"""
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたプロジェクトディレクトリが存在しません: {project_root}", file=sys.stderr)
        return False, None, 0, None, False, ""

    output_dir = output_dir_resolver(project_root, getattr(parsed_args, "output_dir", None))
    if project_root == output_dir:
        print("エラー: ソースディレクトリと出力先ディレクトリに同じパスは指定できません。", file=sys.stderr)
        return False, None, 0, None, False, ""

    port = getattr(parsed_args, "port", default_bridge_port)
    if not isinstance(port, int) or isinstance(port, bool) or not (0 <= port <= 65535):
        print(f"エラー: ポート番号は 0〜65535 の範囲で指定してください: {port}", file=sys.stderr)
        return False, None, 0, None, False, ""

    patch_handoff_max_tabs = getattr(parsed_args, "patch_handoff_max_tabs", None)
    if patch_handoff_max_tabs is not None and (
        isinstance(patch_handoff_max_tabs, bool)
        or not isinstance(patch_handoff_max_tabs, int)
        or not (0 <= patch_handoff_max_tabs <= max_patch_handoff_max_tabs)
    ):
        print(
            f"エラー: --max-auto-tabs は 0〜{max_patch_handoff_max_tabs} の整数で指定してください: {patch_handoff_max_tabs}",
            file=sys.stderr,
        )
        return False, None, 0, None, False, ""

    no_browser = getattr(parsed_args, "no_browser", False)
    target_url = ""
    if not no_browser:
        try:
            target_url = url_validator(getattr(parsed_args, "url", default_aistudio_url))
        except ValueError as e:
            print(f"エラー: ブラウザURLが不正です: {e}", file=sys.stderr)
            return False, None, 0, None, False, ""

    no_sync = getattr(parsed_args, "no_sync", False)
    if no_sync:
        conflicts = conflict_finder(parsed_args)
        if conflicts:
            print(
                f"エラー: --no-sync 指定時は同期オプション ({', '.join(conflicts)}) を併用できません。",
                file=sys.stderr,
            )
            return False, None, 0, None, False, ""

    return True, output_dir, port, patch_handoff_max_tabs, no_browser, target_url

def build_start_skeleton_config(
    parsed_args: argparse.Namespace,
    project_root: Path,
    *,
    create_bundle: bool,
) -> SkeletonConfig:
    """startコマンドのCLI引数からスケルトン同期設定を構築する。"""
    resolved_full_paths = {
        (project_root / Path(p)).resolve() if not Path(p).is_absolute() else Path(p).resolve()
        for p in process_comma_separated_args(getattr(parsed_args, "full_path", []))
    }
    policy_arg = getattr(parsed_args, "policy", None)
    return SkeletonConfig(
        full_code_paths=resolved_full_paths,
        keep_functions=process_comma_separated_args(getattr(parsed_args, "keep_func", [])),
        only_nodes=process_comma_separated_args(getattr(parsed_args, "only_nodes", [])),
        create_bundle=create_bundle,
        bundle_format=getattr(parsed_args, "format", "txt"),
        policy_path=policy_arg.resolve() if policy_arg else None,
    )

def copy_bundle_to_clipboard(
    bundle_path: Optional[Path],
    project_root: Path,
    clipboard_writer: Callable[[str], None],
    no_sync: bool = False,
    no_bundle: bool = False,
    *,
    token_estimator: Callable[[str], int] = estimate_tokens,
) -> bool:
    """生成または既存のバンドルファイル内容をクリップボードへコピーする。"""
    if no_bundle:
        print("ℹ --no-bundle が指定されているためバンドルのクリップボードコピーをスキップしました。")
        return False
    if bundle_path is None or not bundle_path.is_file():
        hint = "（--no-sync の場合は事前にバンドルを生成してください）" if no_sync else ""
        print(
            f"⚠ バンドルファイルが見つからないためクリップボードへのコピーをスキップしました{hint}。直前のクリップボード内容のままです。",
            file=sys.stderr,
        )
        return False
    try:
        content = bundle_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"⚠ バンドルファイルの読み込みに失敗しました: {e}", file=sys.stderr)
        return False
    if not content.strip():
        print("⚠ バンドルファイルが空のためクリップボードへのコピーをスキップしました。", file=sys.stderr)
        return False

    try:
        clipboard_writer(content)
        est_tokens = token_estimator(content)
        try:
            display_path = bundle_path.relative_to(project_root)
        except ValueError:
            display_path = bundle_path
        print(
            f"\n📋 バンドルをクリップボードにコピーしました: {display_path} "
            f"(文字数: {len(content):,} 文字, 推定約 {est_tokens:,} tokens)"
        )
        return True
    except Exception as e:
        print(f"⚠ クリップボードへのコピーに失敗しました: {e}", file=sys.stderr)
        return False
