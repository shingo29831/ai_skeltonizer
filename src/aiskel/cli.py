"""Module: @role: CLI引数の解析、実行オプションの検証、および各サブコマンドのディスパッチを担当する。"""
import argparse
import sys
import subprocess
import platform
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

from .config import SkeletonConfig
from .core.scanner import get_target_files, generate_tree_text
from .core.syncer import ProjectSyncer
from .core.layer_filter import filter_logic_files
from .core.git_diff_analyzer import get_staged_or_modified_files, parse_direct_dependencies
from .core.token_counter import format_token_display, estimate_tokens
from .core.patch_applier import apply_patch
from .core.clipboard import set_clipboard_text
from .core.code_extractor import extract_and_format_snippets

def _extract_commit_message(patch_text: str) -> Tuple[Optional[str], str]:
    # ソースコード内の正規表現リテラルに誤マッチしないようタグ文字列を分割定義
    tag_start = "<" + "commit>"
    tag_end = "</" + "commit>"
    pattern = re.compile(re.escape(tag_start) + r'\s*(.*?)\s*' + re.escape(tag_end), re.DOTALL | re.IGNORECASE)
    
    matches = list(pattern.finditer(patch_text))
    if matches:
        last_match = matches[-1]
        msg = last_match.group(1).strip()
        msg = re.sub(r'^`{1,5}[a-zA-Z]*\s*', '', msg)
        msg = re.sub(r'\s*`{1,5}$', '', msg).strip()
        clean_patch_text = patch_text[:last_match.start()] + patch_text[last_match.end():]
        return (msg if msg else None), clean_patch_text
    return None, patch_text

def _get_clipboard_text() -> str:
    system = platform.system()
    try:
        if system == "Darwin":
            text = subprocess.check_output(["pbpaste"], encoding="utf-8")
        elif system == "Windows":
            text = subprocess.check_output(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw"
                ],
                encoding="utf-8"
            )
        elif system == "Linux":
            try:
                text = subprocess.check_output(["xclip", "-selection", "clipboard", "-o"], encoding="utf-8")
            except FileNotFoundError:
                text = subprocess.check_output(["xsel", "--clipboard", "--output"], encoding="utf-8")
        else:
            print(f"⚠ OS {system} のクリップボード自動取得は未対応です。", file=sys.stderr)
            return ""
            
        return text.replace("\r\n", "\n").replace("\r", "\n")
    except Exception as e:
        print(f"⚠ クリップボードの読み込みに失敗しました: {e}", file=sys.stderr)
        return ""

def _run_validation_test(project_root: Path, test_command: Optional[str], timeout_sec: int = 60) -> Tuple[bool, str]:
    cmd = test_command
    if not cmd or cmd == "auto":
        # なぜ必要か: 仮想環境(.venv)のpytestやsys.executableを優先し、PATH未設定や依存不足による誤失敗を完全防止
        venv_pytest = None
        for venv_name in (".venv", "venv", "env"):
            scripts_dir = project_root / venv_name / ("Scripts" if sys.platform == "win32" else "bin")
            pt = scripts_dir / ("pytest.exe" if sys.platform == "win32" else "pytest")
            if pt.exists():
                venv_pytest = f'"{pt}"'
                break

        if (project_root / "pyproject.toml").exists() or (project_root / "pytest.ini").exists() or (project_root / "tests").exists():
            cmd = venv_pytest or f'"{sys.executable}" -m pytest'
        elif (project_root / "package.json").exists():
            cmd = "npm test"
        elif (project_root / "Cargo.toml").exists():
            cmd = "cargo test"
        elif (project_root / "go.mod").exists():
            cmd = "go test ./..."
        else:
            cmd = venv_pytest or f'"{sys.executable}" -m pytest'

    print(f"\n🧪 検証テストを実行中: {cmd}")
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        return (proc.returncode == 0), proc.stdout
    except subprocess.TimeoutExpired:
        return False, f"テスト実行がタイムアウトしました ({timeout_sec}秒)"
    except Exception as e:
        return False, f"テスト実行コマンドの起動に失敗しました: {e}"

def parse_arguments(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aiskel",
        description="多言語プロジェクトのAIコンテキスト抽出、およびAI出力の自動適用ツール",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="実行するコマンド (省略時はコンテキスト抽出を実行)")

    apply_parser = subparsers.add_parser("apply", aliases=["a"], help="AIが出力した置換ブロック(<<<< ==== >>>>)をソースコードに自動適用します")
    apply_parser.add_argument("patch_file", type=Path, nargs="?", default=None, help="AIの出力テキストが保存されたファイルのパス (省略時はクリップボードから読み込みます)")
    apply_parser.add_argument("-p", "--paste", action="store_true", help="クリップボードを無視して、手動でのテキストペーストを強制します")
    apply_parser.add_argument("--dir", type=Path, default=Path("."), help="プロジェクトのルートディレクトリ (デフォルト: カレントディレクトリ)")
    apply_parser.add_argument("-t", "--target", type=Path, default=None, help="置換対象のファイルを強制的に指定します")
    # CLI入力コスト削減のためサブコマンド専用の短縮フラグ -f を提供
    apply_parser.add_argument("-f", "--force-replace", action="store_true", help="置換済みのコードでも強制的に置換処理を実行します")
    apply_parser.add_argument("--revert", action="store_true", help="パッチの変更を元に戻すリバート処理を行います")
    apply_parser.add_argument("-n", "--dry-run", action="store_true", help="実際にファイルを変更せず、差分プレビュー(Unified Diff)を表示します")
    apply_parser.add_argument("-T", "--test", dest="test", nargs="?", const="auto", default=None, metavar="CMD", help="パッチ適用後にテストを実行し、失敗時は自動リバートします (コマンド省略時は自動検出)")

    revert_parser = subparsers.add_parser("revert", aliases=["r"], help="AIが出力した置換ブロックの変更を元に戻すリバート処理を行います")
    revert_parser.add_argument("patch_file", type=Path, nargs="?", default=None, help="AIの出力テキストが保存されたファイルのパス")
    revert_parser.add_argument("-p", "--paste", action="store_true", help="クリップボードを無視して、手動でのテキストペーストを強制します")
    revert_parser.add_argument("--dir", type=Path, default=Path("."), help="プロジェクトのルートディレクトリ")
    revert_parser.add_argument("-t", "--target", type=Path, default=None, help="置換対象のファイルを強制的に指定します")
    # CLI入力コスト削減のためサブコマンド専用の短縮フラグ -f を提供
    revert_parser.add_argument("-f", "--force-replace", action="store_true", help="置換済みのコードでも強制的に置換処理を実行します")
    revert_parser.add_argument("-n", "--dry-run", action="store_true", help="実際にファイルを変更せず、差分プレビューを表示します")

    copy_parser = subparsers.add_parser("copy", aliases=["c", "cp"], help="指定したファイルや関数・クラスのコードをクリップボードにコピーします")
    copy_parser.add_argument("specs", nargs="+", help="コピー対象 (書式: path/to/file[:func_or_class,...])")
    copy_parser.add_argument("--max-chars", type=int, default=100_000, help="コピーを許可する最大文字数 (デフォルト: 100000)")
    copy_parser.add_argument("--dir", type=Path, default=Path("."), help="プロジェクトのルートディレクトリ")

    parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."), help="解析対象のプロジェクトルートディレクトリのパス")
    parser.add_argument("output_dir", type=Path, nargs="?", default=None, help="スケルトン化したファイルを出力する先のパス")
    parser.add_argument("-f", "--full-path", action="append", default=[], help="スケルトン化せずフルコードのまま保持するファイルまたはフォルダのパス")
    parser.add_argument("-k", "--keep-func", action="append", default=[], help="内部実装を削除せず保持する関数やメソッド名")
    parser.add_argument("--focus", action="append", default=[], help="指定したファイルまたはディレクトリのみを処理対象とする")
    parser.add_argument("--focus-deps", action="store_true", help="--focusで指定したファイルの直接依存ファイルも自動的に対象に含める")
    parser.add_argument("--only-nodes", action="append", default=[], help="指定したクラスや関数のみを抽出し、それ以外を完全に削除する")
    parser.add_argument("--no-bundle", action="store_true", help="単一ファイル・バンドルの出力を行わない")
    parser.add_argument("--format", choices=["txt", "xml", "markdown"], default="txt", help="単一バンドルファイルの出力フォーマット")
    parser.add_argument("--policy", type=Path, default=None, help="バンドルに自動注入するカスタムポリシーファイルのパス")
    parser.add_argument("--force", action="store_true", help="全ファイルを強制的に再処理する")
    parser.add_argument("--no-ui", action="store_true", help="UIレイヤーのファイルを除外してロジック層のみを抽出する")
    parser.add_argument("-g", "--git-diff", "--git-dif", "--diff", dest="git_diff", action="store_true", help="Gitの差分から、変更されたファイルとそれに直接依存するファイルのみを抽出する")
    parser.add_argument("-c", "--copy", nargs="+", metavar="SPEC", help="指定したファイルや関数・クラスのコードをクリップボードにコピーします")
    parser.add_argument("--max-chars", type=int, default=100_000, help="クリップボードコピー時の最大文字数")
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
    return project_root / "ai_meta"

def _ensure_gitignore_updated(project_root: Path, output_dir: Path) -> None:
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

def main(args: Optional[List[str]] = None) -> int:
    try:
        parsed_args = parse_arguments(args)

        is_copy_cmd = hasattr(parsed_args, "command") and parsed_args.command in ("copy", "c", "cp")
        copy_specs = parsed_args.specs if is_copy_cmd else getattr(parsed_args, "copy", None)
        if copy_specs:
            project_root = (parsed_args.dir if is_copy_cmd else parsed_args.project_dir).resolve()
            max_chars = parsed_args.max_chars
            try:
                formatted_text, est_tokens, item_count = extract_and_format_snippets(
                    copy_specs, project_root, max_chars=max_chars
                )
                set_clipboard_text(formatted_text)
                print(f"📋 クリップボードに {item_count} 件のコードをコピーしました。")
                print(f"   - 総文字数   : {len(formatted_text):,} 文字")
                print(f"   - 推定トークン: 約 {est_tokens:,} tokens")
                print("   AIチャットへそのままペーストしてご利用いただけます。")
                return 0
            except (ValueError, FileNotFoundError, KeyError, RuntimeError) as e:
                print(f"✖ コピー失敗: {e}", file=sys.stderr)
                return 1

        if hasattr(parsed_args, "command") and parsed_args.command in ("apply", "a", "revert", "r"):
            is_revert = getattr(parsed_args, "revert", False) or parsed_args.command in ("revert", "r")
            is_dry_run = getattr(parsed_args, "dry_run", False)
            test_option = getattr(parsed_args, "test", None)
            project_root: Path = parsed_args.dir.resolve()
            
            if parsed_args.patch_file:
                patch_file: Path = parsed_args.patch_file.resolve()
                if not patch_file.exists():
                    print(f"エラー: パッチファイルが見つかりません: {patch_file}", file=sys.stderr)
                    return 1
                action_name = "リバート" if is_revert else "適用"
                print(f"🚀 AIパッチの{action_name}を開始します (ファイル: {patch_file.name})")
                patch_text = patch_file.read_text(encoding="utf-8")
            elif parsed_args.paste:
                action_name = "リバート" if is_revert else "適用"
                print("🚀 AIの出力テキストをペーストしてください。")
                print("   (ペースト後、Windowsは Ctrl+Z を押してEnter、Mac/Linuxは Ctrl+D を押すと実行されます):")
                patch_text = sys.stdin.read()
                print(f"\n{action_name}を開始します...")
            elif not sys.stdin.isatty():
                patch_text = sys.stdin.read()
            else:
                patch_text = _get_clipboard_text()
                if not patch_text or "<<<<" not in patch_text:
                    if not patch_text:
                        print("⚠ クリップボードが空です。")
                    else:
                        print("⚠ クリップボードに置換ブロック(<<<<)が見つかりません。")
                    action_name = "リバート" if is_revert else "適用"
                    print("🚀 AIの出力テキストをペーストしてください。")
                    print("   (ペースト後、Windowsは Ctrl+Z を押してEnter、Mac/Linuxは Ctrl+D を押すと実行されます):")
                    patch_text = sys.stdin.read()
                    print(f"\n{action_name}を開始します...")
                else:
                    print("🚀 クリップボードからAIの出力テキストを読み込みました。")

            target_file = parsed_args.target.resolve() if parsed_args.target else None
            commit_msg, patch_text = _extract_commit_message(patch_text)
            
            if is_revert and commit_msg:
                try:
                    last_commit_msg = subprocess.check_output(["git", "log", "-1", "--pretty=%B"], cwd=project_root, encoding="utf-8", stderr=subprocess.DEVNULL).strip()
                    if last_commit_msg.splitlines()[0].strip() == commit_msg.splitlines()[0].strip():
                        print(f"\n📦 直前のコミットが対象パッチのコミットと一致しました: {commit_msg}")
                        print("コミットを破棄(git reset --hard HEAD~1)して元に戻します...")
                        subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=project_root, check=True)
                        print("✔ コミットの破棄が完了しました。")
                        return 0
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pass

            patch_result = apply_patch(
                patch_text, 
                project_root, 
                target_file,
                force_replace=getattr(parsed_args, "force_replace", False),
                revert=is_revert,
                dry_run=is_dry_run
            )
            success, fail, skipped, modified_files = patch_result[0], patch_result[1], patch_result[2], patch_result[3]
            
            action_name = "リバート" if is_revert else "適用"
            mode_prefix = "[DRY-RUN] " if is_dry_run else ""
            print(f"\n=== {mode_prefix}{action_name}結果 ===")
            print(f"✔ 成功: {success} 箇所")
            if skipped > 0:
                print(f"⏭ スキップ (適用済み): {skipped} 箇所")
            if fail > 0:
                print(f"✖ 失敗: {fail} 箇所")

            if is_dry_run:
                return 0 if fail == 0 else 1

            if not is_revert and test_option is not None and success > 0 and fail == 0:
                test_ok, test_output = _run_validation_test(project_root, test_option)
                if test_ok:
                    print("✅ 検証テストに成功しました。")
                else:
                    print("\n❌ 検証テストが失敗しました。変更を即座に自動リバート（ロールバック）します...", file=sys.stderr)
                    if test_output:
                        print("\n--- テスト失敗ログ ---", file=sys.stderr)
                        print(test_output, file=sys.stderr)
                        print("----------------------\n", file=sys.stderr)
                    # なぜ必要か: 新規作成ファイル(未追跡)の残留を防ぎ、Git内外を問わず完全ロールバックを保証
                    rolled_back = False
                    try:
                        for f in modified_files:
                            res = subprocess.run(["git", "checkout", "--", str(f)], cwd=project_root, capture_output=True)
                            if res.returncode != 0:
                                subprocess.run(["git", "clean", "-f", str(f)], cwd=project_root, capture_output=True)
                        rolled_back = True
                        print("🔄 git checkout / clean により変更ファイルを元の状態に復元しました。", file=sys.stderr)
                    except Exception:
                        pass
                    if not rolled_back:
                        apply_patch(patch_text, project_root, target_file, force_replace=True, revert=True)
                        print("🔄 逆パッチ適用により変更箇所を元に戻しました。", file=sys.stderr)
                    return 1

            if not is_revert and commit_msg and success > 0 and fail == 0:
                print(f"\n📦 コミットメッセージを検出しました: {commit_msg}")
                print("自動コミットを実行します...")
                try:
                    for f in modified_files:
                        subprocess.run(["git", "add", str(f)], cwd=project_root, check=True)
                    # 差分ゼロ時のgit commit失敗(nothing added to commit)を防止
                    staged_diff = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=project_root, encoding="utf-8").strip()
                    if staged_diff:
                        subprocess.run(["git", "commit", "-m", commit_msg], cwd=project_root, check=True)
                        print("✔ 自動コミットが完了しました。")
                    else:
                        print("ℹ 変更内容が既存コードと同一のため、コミットをスキップしました。")
                except (subprocess.CalledProcessError, FileNotFoundError) as e:
                    print(f"⚠ 自動コミットに失敗しました: {e}")

            return 0 if fail == 0 else 1

        project_root: Path = parsed_args.project_dir.resolve()
        if not project_root.exists() or not project_root.is_dir():
            print(f"エラー: 指定されたソースディレクトリが存在しません: {project_root}", file=sys.stderr)
            return 1

        output_dir: Path = _resolve_output_dir(project_root, parsed_args.output_dir)
        if project_root == output_dir:
            print("エラー: ソースディレクトリと出力先ディレクトリに同じパスは指定できません。", file=sys.stderr)
            return 1

        _ensure_gitignore_updated(project_root, output_dir)

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
        print("\n--- 辞書・マニュアル出力 ---")
        if bundle_path and bundle_path.exists():
            print(f"  - アーキテクチャ要素: {bundle_path.relative_to(project_root)} (変更対象ファイルの特定用)")
            
        print("\n--- トークン・予算削減アナライザー ---")
        print(f"  - 元のフルコード総量 : 約 {stats.raw_tokens:,} tokens")
        if bundle_path and bundle_path.exists():
            arch_tokens = estimate_tokens(bundle_path.read_text(encoding="utf-8"))
            arch_red = (1.0 - (arch_tokens / max(stats.raw_tokens, 1))) * 100
            print(f"  - アーキテクチャ要素: 約 {arch_tokens:,} tokens ({arch_red:.1f}% 削減)")
        else:
            print(f"  - 削減トークン数 : 約 {stats.saved_tokens:,} tokens ({stats.reduction_percentage:.1f}% 削減)")
        return 0

    except Exception as e:
        print(f"\n致命的なエラーが発生しました: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
