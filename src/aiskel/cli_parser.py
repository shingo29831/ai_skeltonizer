"""Module: @role: CLI引数の正規化、各種サブコマンドの引数定義およびパース処理を担当する。"""
import argparse
import sys
from pathlib import Path
from typing import List, Optional, Set

from .core.bloat_detector import DEFAULT_BLOAT_MAX_CHARS, DEFAULT_BLOAT_MAX_LINES, DEFAULT_TOP_FILES_COUNT
from .core.code_extractor import DEFAULT_MAX_CHARS


def normalize_cli_args(args: List[str]) -> List[str]:
    # 可変長位置引数の途中にオプションが混在した場合でも順序を再構成して解析エラーを防止
    if not args:
        return args

    # なぜ必要か: サブコマンド実行時にルートレベルの -c 正規化との競合を防ぐため
    if args[0] in ("apply", "a", "revert", "r", "test", "t", "docs", "d", "watch", "w", "bloat", "b", "size"):
        return args

    cmd_idx = -1
    for i, a in enumerate(args):
        if a in ("copy", "c", "cp"):
            cmd_idx = i
            break

    if cmd_idx != -1:
        prefix, rest = args[: cmd_idx + 1], args[cmd_idx + 1 :]
        options, positionals = [], []
        i = 0
        while i < len(rest):
            token = rest[i]
            if token in ("-d", "--deps", "--dependencies", "-h", "--help"):
                options.append(token)
                i += 1
            elif token in ("--max-chars", "--dir"):
                options.append(token)
                if i + 1 < len(rest):
                    options.append(rest[i + 1])
                    i += 2
                else:
                    i += 1
            elif token.startswith("--max-chars=") or token.startswith("--dir="):
                options.append(token)
                i += 1
            else:
                positionals.append(token)
                i += 1
        return prefix + options + positionals

    c_idx = -1
    for i, a in enumerate(args):
        if a in ("-c", "--copy"):
            c_idx = i
            break

    if c_idx != -1:
        prefix, rest = args[:c_idx], args[c_idx:]
        options, specs, c_flag = [], [], rest[0]
        i = 1
        while i < len(rest):
            token = rest[i]
            if token in ("-d", "--deps", "--dependencies", "--force", "--no-ui", "-g", "--git-diff", "--git-dif", "--diff", "--no-bundle", "--focus-deps"):
                options.append(token)
                i += 1
            elif token in ("--max-chars", "-f", "--full-path", "-k", "--keep-func", "--focus", "--only-nodes", "--format", "--policy"):
                options.append(token)
                if i + 1 < len(rest):
                    options.append(rest[i + 1])
                    i += 2
                else:
                    i += 1
            elif any(token.startswith(f"{opt}=") for opt in ("--max-chars", "--full-path", "--keep-func", "--focus", "--only-nodes", "--format", "--policy")):
                options.append(token)
                i += 1
            else:
                specs.append(token)
                i += 1
        return prefix + options + [c_flag] + specs

    return args


def process_comma_separated_args(arg_list: List[str]) -> Set[str]:
    result = set()
    for item in arg_list:
        for part in item.split(","):
            cleaned = part.strip()
            if cleaned:
                result.add(cleaned)
    return result


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
    apply_parser.add_argument("-T", "--test", dest="test", nargs="?", const="auto", default=None, metavar="CMD", help="パッチ適用後にテストを実行し、失敗時は自動リバートします (コマンド省略時はai_metaの設定または自動検出)")
    apply_parser.add_argument("-e", "-r", "--exec", "--run", dest="exec_cmd", nargs="?", const="auto", default=None, metavar="CMD", help="パッチ適用後にai_meta内の設定ファイルまたは指定されたコマンドを実行します (省略時は設定ファイルから取得)")
    apply_parser.add_argument("--no-test", action="store_true", help="設定ファイルのコマンド実行および検証テストを明示的にスキップします")
    apply_parser.add_argument("-y", "--yes", action="store_true", help="確認プロンプトをスキップし、自動的にコミットを承認・実行します")
    apply_parser.add_argument("--no-sync", action="store_true", help="置換成功後のスケルトン・バンドル自動同期をスキップします")
    apply_parser.add_argument("--no-bloat", action="store_true", help="置換適用後のファイル肥大化自動チェックをスキップします")

    revert_parser = subparsers.add_parser("revert", aliases=["r"], help="AIが出力した置換ブロックの変更を元に戻すリバート処理を行います")
    revert_parser.add_argument("patch_file", type=Path, nargs="?", default=None, help="AIの出力テキストが保存されたファイルのパス")
    revert_parser.add_argument("-p", "--paste", action="store_true", help="クリップボードを無視して、手動でのテキストペーストを強制します")
    revert_parser.add_argument("--dir", type=Path, default=Path("."), help="プロジェクトのルートディレクトリ")
    revert_parser.add_argument("-t", "--target", type=Path, default=None, help="置換対象のファイルを強制的に指定します")
    # CLI入力コスト削減のためサブコマンド専用の短縮フラグ -f を提供
    revert_parser.add_argument("-f", "--force-replace", action="store_true", help="置換済みのコードでも強制的に置換処理を実行します")
    revert_parser.add_argument("-n", "--dry-run", action="store_true", help="実際にファイルを変更せず、差分プレビューを表示します")
    revert_parser.add_argument("--no-sync", action="store_true", help="リバート成功後のスケルトン・バンドル自動同期をスキップします")

    test_parser = subparsers.add_parser("test", aliases=["t"], help="設定ファイル(ai_meta)または指定されたテスト・検証コマンドを実行します")
    test_parser.add_argument("cmd", nargs="*", default=[], help="実行するテストコマンド (省略時は設定ファイルから取得または自動検出)")
    test_parser.add_argument("--dir", type=Path, default=Path("."), help="プロジェクトのルートディレクトリ (デフォルト: カレントディレクトリ)")
    test_parser.add_argument("-q", "--quiet", action="store_true", help="テスト実行中の標準出力を抑制します")
    test_parser.add_argument("--timeout", type=int, default=60, help="コマンド1件あたりのタイムアウト秒数 (デフォルト: 60秒)")

    copy_parser = subparsers.add_parser("copy", aliases=["c", "cp"], help="指定したファイルや関数・クラスのコードをクリップボードにコピーします")
    copy_parser.add_argument("specs", nargs="+", help="コピー対象 (書式: path/to/file[:func_or_class,...])")
    copy_parser.add_argument("-d", "--deps", "--dependencies", action="store_true", help="指定した関数・クラスが参照している同一ファイル内の依存ノードも自動で一緒にコピーします")
    copy_parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help=f"コピーを許可する最大文字数 (デフォルト: {DEFAULT_MAX_CHARS:,})")
    copy_parser.add_argument("--dir", type=Path, default=Path("."), help="プロジェクトのルートディレクトリ")

    bloat_parser = subparsers.add_parser("bloat", aliases=["b", "size"], help="ファイルの行数・文字数を計測し、肥大化ファイルを検出してAI向け分割指示をクリップボードに保存します")
    bloat_parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."), help="プロジェクトのルートディレクトリ (デフォルト: カレントディレクトリ)")
    bloat_parser.add_argument("-l", "--max-lines", type=int, default=None, help=f"肥大化とみなす最大行数 (デフォルト: {DEFAULT_BLOAT_MAX_LINES})")
    bloat_parser.add_argument("-c", "-C", "--max-chars", type=int, default=None, help=f"肥大化とみなす最大文字数 (デフォルト: {DEFAULT_BLOAT_MAX_CHARS:,})")
    bloat_parser.add_argument("-n", "--top", "--limit", type=int, default=DEFAULT_TOP_FILES_COUNT, dest="top_count", help=f"肥大化ファイル未検出時に表示する上位ファイル数 (デフォルト: {DEFAULT_TOP_FILES_COUNT})")
    bloat_parser.add_argument("-e", "--exclude", action="append", default=[], help="追加で除外するファイルまたはディレクトリパス (カンマ区切り可)")
    bloat_parser.add_argument("--no-clipboard", action="store_true", help="クリップボードへの自動保存を行わず標準出力のみ行います")
    bloat_parser.add_argument("-q", "--quiet", action="store_true", help="肥大化ファイルが検出されなかった場合のメッセージを抑制します")

    docs_parser = subparsers.add_parser("docs", aliases=["d"], help="HTMLドキュメントをAI向けに解析・軽量構造化して出力します")
    docs_parser.add_argument("targets", type=Path, nargs="*", default=[], help="解析対象のHTMLファイルまたはディレクトリのパス (省略時は標準入力またはカレントディレクトリ)")
    docs_parser.add_argument("-c", "-cp", "--clipboard", "--copy", dest="clipboard", action="store_true", help="解析結果をクリップボードに自動保存します")
    docs_parser.add_argument("-o", "--output", type=Path, default=None, help="解析結果を保存する出力ファイルパス (省略時は標準出力)")

    watch_parser = subparsers.add_parser("watch", aliases=["w"], help="ファイル変更を常時監視し、変更があるたびに自動でスケルトン同期を実行します")
    watch_parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."), help="監視対象のプロジェクトルートディレクトリ")
    watch_parser.add_argument("output_dir", type=Path, nargs="?", default=None, help="スケルトン化したファイルを出力する先のパス")
    watch_parser.add_argument("-i", "--interval", type=float, default=1.0, help="ポーリング間隔 (秒, デフォルト: 1.0)")
    watch_parser.add_argument("-f", "--full-path", action="append", default=[], help="スケルトン化せずフルコードのまま保持するファイルまたはフォルダのパス")
    watch_parser.add_argument("-k", "--keep-func", action="append", default=[], help="内部実装を削除せず保持する関数やメソッド名")
    watch_parser.add_argument("--only-nodes", action="append", default=[], help="指定したクラスや関数のみを抽出し、それ以外を完全に削除する")
    watch_parser.add_argument("--no-bundle", action="store_true", help="単一ファイル・バンドルの出力を行わない")
    watch_parser.add_argument("--format", choices=["txt", "xml", "markdown"], default="txt", help="単一バンドルファイルの出力フォーマット")
    watch_parser.add_argument("--policy", type=Path, default=None, help="バンドルに自動注入するカスタムポリシーファイルのパス")
    watch_parser.add_argument("--no-ui", action="store_true", help="UIレイヤーのファイルを除外してロジック層のみを抽出する")

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
    parser.add_argument("-d", "--deps", "--dependencies", action="store_true", help="コピー時に参照している依存ノードも自動で一緒にコピーします")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help=f"クリップボードコピー時の最大文字数 (デフォルト: {DEFAULT_MAX_CHARS:,})")
    raw_args = list(args) if args is not None else sys.argv[1:]
    return parser.parse_args(normalize_cli_args(raw_args))


_normalize_cli_args = normalize_cli_args
_process_comma_separated_args = process_comma_separated_args
