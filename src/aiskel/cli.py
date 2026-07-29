# src/aiskel/cli.py
import argparse
import sys
import subprocess
import platform
from pathlib import Path
from typing import List, Optional, Set

from .config import SkeletonConfig
from .core.scanner import get_target_files, generate_tree_text
from .core.syncer import ProjectSyncer
from .core.layer_filter import filter_logic_files
from .core.git_diff_analyzer import get_staged_or_modified_files, parse_direct_dependencies
from .core.token_counter import format_token_display, estimate_tokens
from .core.patch_applier import apply_patch

def _get_clipboard_text() -> str:
    system = platform.system()
    try:
        if system == "Darwin":
            return subprocess.check_output(["pbpaste"], text=True)
        elif system == "Windows":
            return subprocess.check_output(["powershell.exe", "-command", "Get-Clipboard"], text=True)
        elif system == "Linux":
            try:
                return subprocess.check_output(["xclip", "-selection", "clipboard", "-o"], text=True)
            except FileNotFoundError:
                return subprocess.check_output(["xsel", "--clipboard", "--output"], text=True)
        else:
            raise NotImplementedError(f"OS {system} のクリップボード取得は未対応です。")
    except Exception as e:
        print(f"クリップボードの読み込みに失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

def parse_arguments(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aiskel",
        description="多言語プロジェクトのAIコンテキスト抽出、およびAI出力の自動適用ツール",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="実行するコマンド (省略時はコンテキスト抽出を実行)")

    # apply サブコマンドの定義
    apply_parser = subparsers.add_parser("apply", help="AIが出力した置換ブロック(<<<< ==== >>>>)をソースコードに自動適用します")
    apply_parser.add_argument("patch_file", type=Path, nargs="?", default=None, help="AIの出力テキストが保存されたファイルのパス (省略時はクリップボードから読み込みます)")
    apply_parser.add_argument("--dir", type=Path, default=Path("."), help="プロジェクトのルートディレクトリ (デフォルト: カレントディレクトリ)")
    apply_parser.add_argument("-t", "--target", type=Path, default=None, help="置換対象のファイルを強制的に指定します (AIがファイルパスを出力しなかった場合に使用)")

    # 従来の抽出コマンド用の引数 (サブコマンドなしの場合)
    parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."), help="解析対象のプロジェクト・ルートディレクトリのパス")
    parser.add_argument("output_dir", type=Path, nargs="?", default=None, help="スケルトン化したファイルを出力する先のパス")
    parser.add_argument("-f", "--full-path", action="append", default=[], help="スケルトン化せずフルコードのまま保持するファイルまたはフォルダのパス")
    parser.add_argument("-k", "--keep-func", action="append", default=[], help="内部実装（中身）を削除せず保持する関数やメソッド名")
    parser.add_argument("--focus", action="append", default=[], help="指定したファイルまたはディレクトリのみを処理対象とする")
    parser.add_argument("--focus-deps", action="store_true", help="--focusで指定したファイルの直接依存ファイルも自動的に対象に含める")
    parser.add_argument("--only-nodes", action="append", default=[], help="指定したクラスや関数のみを抽出し、それ以外を完全に削除する")
    parser.add_argument("--no-bundle", action="store_true", help="単一ファイル・バンドルの出力を行わない")
    parser.add_argument("--format", choices=["txt", "xml", "markdown"], default="txt", help="単一バンドルファイルの出力フォーマット")
    parser.add_argument("--policy", type=Path, default=None, help="バンドルに自動注入するカスタムポリシーファイルのパス")
    parser.add_argument("--force", action="store_true", help="全ファイルを強制的に再処理する")
    parser.add_argument("--no-ui", action="store_true", help="UIレイヤーのファイル（.tsx, .html等）を除外してロジック層のみを抽出する")
    parser.add_argument("--git-diff", action="store_true", help="Gitの差分から、変更されたファイルとそれに直接依存するファイルのみを抽出する")
    return parser.parse_args(args)

def _process_comma_separated_args(arg_list: List[str]) -> Set[str]:
    result = set()
    for item in arg_list:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned:
                result.add(cleaned)
    return result

def _resolve_output_dir(project_root: Path, custom_output_dir: Optional[Path]) -> Path:
    if custom_output_dir is not None:
        return custom_output_dir.resolve()
    return project_root.with_name(f"{project_root.name}_ai_context")

def main(args: Optional[List[str]] = None) -> int:
    try:
        parsed_args = parse_arguments(args)

        # apply コマンドの処理
        if hasattr(parsed_args, "command") and parsed_args.command == "apply":
            project_root: Path = parsed_args.dir.resolve()
            
            if parsed_args.patch_file:
                patch_file: Path = parsed_args.patch_file.resolve()
                if not patch_file.exists():
                    print(f"エラー: パッチファイルが見つかりません: {patch_file}", file=sys.stderr)
                    return 1
                print(f"🚀 AIパッチの適用を開始します (ファイル: {patch_file.name})")
                patch_text = patch_file.read_text(encoding="utf-8")
            elif not sys.stdin.isatty():
                # パイプやリダイレクトからの標準入力
                patch_text = sys.stdin.read()
            else:
                # 引数なし、パイプなしの場合はデフォルトでクリップボードから読み込む
                print("🚀 クリップボードからAIの出力テキストを読み込みます...")
                patch_text = _get_clipboard_text()
                if not patch_text.strip():
                    print("エラー: クリップボードが空です。", file=sys.stderr)
                    return 1

            target_file = parsed_args.target.resolve() if parsed_args.target else None
            success, fail = apply_patch(patch_text, project_root, target_file)
            
            print("\n=== 適用結果 ===")
            print(f"✅ 成功: {success} 箇所")
            if fail > 0:
                print(f"❌ 失敗: {fail} 箇所")
            return 0 if fail == 0 else 1

        # 従来の抽出処理
        project_root: Path = parsed_args.project_dir.resolve()

        if not project_root.exists() or not project_root.is_dir():
            print(f"エラー: 指定されたソースディレクトリが存在しません: {project_root}", file=sys.stderr)
            return 1

        output_dir: Path = _resolve_output_dir(project_root, parsed_args.output_dir)

        if project_root == output_dir:
            print("エラー: ソースディレクトリと出力先ディレクトリに同じパスは指定できません。", file=sys.stderr)
            return 1

        resolved_full_paths = {
            (project_root / Path(p)).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            for p in _process_comma_separated_args(parsed_args.full_path)
        }

        config = SkeletonConfig(
            full_code_paths=resolved_full_paths,
            keep_functions=_process_comma_separated_args(parsed_args.keep_func),
            only_nodes=_process_comma_separated_args(parsed_args.only_nodes),
            create_bundle=not parsed_args.no_bundle,
            bundle_format=parsed_args.format,
            policy_path=parsed_args.policy.resolve() if parsed_args.policy else None,
        )

        print(f"解析を開始します: {project_root}")
        target_files = get_target_files(project_root)
        
        focus_paths = _process_comma_separated_args(parsed_args.focus)
        if focus_paths:
            resolved_focus_files = set()
            for p_str in focus_paths:
                p = (project_root / Path(p_str)).resolve() if not Path(p_str).is_absolute() else Path(p_str).resolve()
                if p.is_dir():
                    resolved_focus_files.update({tf for tf in target_files if p in tf.parents or p == tf.parent})
                elif p in target_files:
                    resolved_focus_files.add(p)
            if parsed_args.focus_deps:
                resolved_focus_files = parse_direct_dependencies(resolved_focus_files, set(target_files))
            target_files = list(resolved_focus_files)

        if parsed_args.git_diff:
            modified_files = get_staged_or_modified_files(project_root)
            if modified_files:
                target_files = list(parse_direct_dependencies(modified_files, set(target_files)))
        
        if parsed_args.no_ui:
            target_files = filter_logic_files(target_files)

        tree_text = generate_tree_text(project_root, target_files)
        syncer = ProjectSyncer(project_root, output_dir, config)
        deleted_count = syncer.clean_deleted_files(target_files)
        updated_count, skipped_count, bundle_path = syncer.sync_files(target_files, tree_text=tree_text, force_rebuild=parsed_args.force)

        stats = syncer.token_stats
        print("\n=== 同期およびコンテキスト最適化完了 ===")
        print(f"出力先ディレクトリ: {output_dir}")
        print(f"  - 更新/処理ファイル数 : {updated_count} 件")
        print(f"  - 変更なし(スキップ)   : {skipped_count} 件")
        if deleted_count > 0:
            print(f"  - 削除した古いファイル: {deleted_count} 件")
        print("\n--- 辞書・マニュアル出力 (ai_meta/ フォルダ内に隔離集約) ---")
        if bundle_path:
            arch_path = bundle_path.parent / "phase1_architecture_bundle.txt"
            if arch_path.exists():
                print(f"  - [フェーズ1用] アーキテクチャ要約 : ai_meta/{arch_path.name} (変更対象ファイルの特定用)")
            print(f"  - [フェーズ2用] 統合コンテキスト   : ai_meta/{bundle_path.name} (実際の実装・修正依頼用)")
            static_path = bundle_path.parent / "phase2_static_skeleton.txt"
            if static_path.exists():
                print(f"  - [フェーズ2用] 静的スケルトン     : ai_meta/{static_path.name} (型定義などの固定配置用)")
        print("\n--- トークン・予算削減アナライザー ---")
        print(f"  - 元のフルコード総量 : 約 {stats.raw_tokens:,} tokens")
        if bundle_path:
            arch_path = bundle_path.parent / "phase1_architecture_bundle.txt"
            if arch_path.exists():
                arch_tokens = estimate_tokens(arch_path.read_text(encoding="utf-8"))
                arch_red = (1.0 - (arch_tokens / max(stats.raw_tokens, 1))) * 100
                print(f"  - [フェーズ1] アーキテクチャ要約 : 約 {arch_tokens:,} tokens ({arch_red:.1f}% 削減)")
            
            ctx_tokens = estimate_tokens(bundle_path.read_text(encoding="utf-8"))
            ctx_red = (1.0 - (ctx_tokens / max(stats.raw_tokens, 1))) * 100
            print(f"  - [フェーズ2] 統合コンテキスト   : 約 {ctx_tokens:,} tokens ({ctx_red:.1f}% 削減)")
            
            static_path = bundle_path.parent / "phase2_static_skeleton.txt"
            if static_path.exists():
                static_tokens = estimate_tokens(static_path.read_text(encoding="utf-8"))
                print(f"  - [フェーズ2] 静的スケルトン     : 約 {static_tokens:,} tokens")
        else:
            print(f"  - 削減トークン数 : 約 {stats.saved_tokens:,} tokens ({stats.reduction_percentage:.1f}% 削減)")
        return 0

    except Exception as e:
        print(f"\n致命的なエラーが発生しました: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())