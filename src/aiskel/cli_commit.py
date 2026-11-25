"""Module: @role: 未コミット時のコミットメッセージ保持および後続コミットサブコマンド(commit)の実行を担当する。"""
import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

from .cli_interaction import prompt_user_confirmation
from .cli_sync import ensure_gitignore_updated
from .core.test_runner import run_validation_test

PENDING_COMMIT_FILENAME = "pending_commit.json"


@dataclass
class PendingCommitInfo:
    commit_message: Optional[str]
    modified_files: List[str]


def _to_rel_posix(project_root: Path, file_path: Path) -> str:
    try:
        return file_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return file_path.as_posix()


def _to_git_path_arg(project_root: Path, file_path: Path) -> str:
    # シンボリックリンク差異による git outside repository エラーを防ぐため相対パス化
    try:
        return file_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(file_path)


def _get_pending_commit_candidates(project_root: Path) -> Tuple[Path, Path]:
    return (
        project_root / "ai_meta" / PENDING_COMMIT_FILENAME,
        project_root / ".ai_meta" / PENDING_COMMIT_FILENAME,
    )


def _resolve_pending_commit_path(project_root: Path, for_write: bool = False) -> Path:
    primary, alt = _get_pending_commit_candidates(project_root)
    if for_write or primary.is_file():
        return primary
    if alt.is_file():
        return alt
    return primary


def load_pending_commit(project_root: Path) -> Optional[PendingCommitInfo]:
    path = _resolve_pending_commit_path(project_root, for_write=False)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        raw_msg = data.get("commit_message")
        commit_message = raw_msg.strip() if isinstance(raw_msg, str) and raw_msg.strip() else None
        raw_files = data.get("modified_files", [])
        modified_files = [
            f.strip() for f in raw_files if isinstance(f, str) and f.strip()
        ] if isinstance(raw_files, list) else []
        if not commit_message and not modified_files:
            return None
        return PendingCommitInfo(commit_message=commit_message, modified_files=modified_files)
    except Exception:
        return None


def save_pending_commit(
    project_root: Path,
    commit_message: Optional[str],
    modified_files: Set[Path],
) -> Optional[Path]:
    existing = load_pending_commit(project_root)
    merged_files: List[str] = list(existing.modified_files) if existing else []
    seen = set(merged_files)

    for f in sorted(modified_files, key=lambda p: str(p)):
        rel = _to_rel_posix(project_root, f)
        if rel not in seen:
            merged_files.append(rel)
            seen.add(rel)

    clean_msg = commit_message.strip() if isinstance(commit_message, str) and commit_message.strip() else None
    final_msg = clean_msg or (existing.commit_message if existing else None)
    if not final_msg and not merged_files:
        return None

    target_path = _resolve_pending_commit_path(project_root, for_write=True)
    _, alt_path = _get_pending_commit_candidates(project_root)
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_gitignore_updated(project_root, target_path.parent)
        payload = {
            "commit_message": final_msg,
            "modified_files": merged_files,
        }
        target_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if alt_path != target_path and alt_path.is_file():
            try:
                alt_path.unlink()
            except OSError:
                pass
        return target_path
    except Exception as e:
        print(f"⚠ コミット情報の保持に失敗しました: {e}", file=sys.stderr)
        return None


def clear_pending_commit(project_root: Path, committed_files: Optional[Set[Path]] = None) -> None:
    candidates = [p for p in _get_pending_commit_candidates(project_root) if p.is_file()]
    if not candidates:
        return

    if committed_files is not None:
        existing = load_pending_commit(project_root)
        if existing:
            done_rels = {_to_rel_posix(project_root, f) for f in committed_files}
            remaining = [f for f in existing.modified_files if f not in done_rels]
            if remaining:
                primary = _resolve_pending_commit_path(project_root, for_write=True)
                try:
                    payload = {
                        "commit_message": existing.commit_message,
                        "modified_files": remaining,
                    }
                    primary.parent.mkdir(parents=True, exist_ok=True)
                    primary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                except Exception:
                    pass
                for c in candidates:
                    if c != primary:
                        try:
                            c.unlink()
                        except OSError:
                            pass
                return

    for c in candidates:
        try:
            c.unlink()
        except OSError:
            pass


def _is_ignored_by_git(project_root: Path, path_arg: str) -> bool:
    res = subprocess.run(
        ["git", "check-ignore", "-q", "--", path_arg],
        cwd=project_root,
        capture_output=True,
    )
    return res.returncode == 0


def _is_stageable_path(project_root: Path, target_path: Path, path_arg: str) -> bool:
    if target_path.exists():
        return not _is_ignored_by_git(project_root, path_arg)
    res = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", path_arg],
        cwd=project_root,
        capture_output=True,
    )
    return res.returncode == 0


def stage_target_files(
    project_root: Path,
    target_files: Optional[Set[Path]] = None,
    stage_all: bool = False,
) -> Tuple[str, List[str]]:
    if stage_all:
        subprocess.run(["git", "add", "-A"], cwd=project_root, check=True)
        staged = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only"], cwd=project_root, encoding="utf-8"
        ).strip()
        return staged, []

    if not target_files:
        return "", []

    stageable_args: List[str] = []
    for f in sorted(target_files, key=lambda p: str(p)):
        arg = _to_git_path_arg(project_root, f)
        if arg not in stageable_args and _is_stageable_path(project_root, f, arg):
            stageable_args.append(arg)

    if not stageable_args:
        return "", []

    subprocess.run(["git", "add", "--", *stageable_args], cwd=project_root, check=True)
    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--", *stageable_args],
        cwd=project_root,
        encoding="utf-8",
    ).strip()
    return staged, stageable_args


def _has_git_head(project_root: Path) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=project_root,
        capture_output=True,
    )
    return res.returncode == 0


def execute_git_commit(
    project_root: Path,
    commit_msg: str,
    target_files: Optional[Set[Path]] = None,
    stage_all: bool = False,
    auto_push: bool = False,
    no_push: bool = False,
) -> Tuple[bool, bool, Optional[str], bool]:
    """指定ファイル(または全変更)をステージングし、(コミット成否, 差分有無, エラー, プッシュ成否)を返す"""
    try:
        staged_diff, stageable_args = stage_target_files(
            project_root, target_files=target_files, stage_all=stage_all
        )
        if not staged_diff:
            return False, False, None, True

        commit_cmd = ["git", "commit", "-m", commit_msg]
        if not stage_all and stageable_args and _has_git_head(project_root):
            # 他に手動ステージ済みの無関係ファイルがあっても対象ファイルのみをコミットする
            all_staged = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"], cwd=project_root, encoding="utf-8"
            ).strip().splitlines()
            if len(all_staged) > len(staged_diff.splitlines()):
                commit_cmd.extend(["--", *stageable_args])

        subprocess.run(commit_cmd, cwd=project_root, check=True)
        print("✔ コミットが完了しました。")

        push_ok = True
        # 非対話や自動適用時はプッシュ確認で待機・誤送信しないよう無条件スキップ
        should_push = False if no_push else (
            auto_push or prompt_user_confirmation(
                "リモートリポジトリにプッシュ (git push) しますか？ [Enter/Space: y, 他: n]: ",
                default=False,
            )
        )
        if should_push:
            print("プッシュを実行中...")
            push_res = subprocess.run(["git", "push"], cwd=project_root)
            if push_res.returncode == 0:
                print("✔ プッシュが完了しました。")
            else:
                push_ok = False
                print("⚠ プッシュに失敗しました（上記のエラーを確認してください）。", file=sys.stderr)
        else:
            print("ℹ プッシュをスキップしました。")

        return True, True, None, push_ok
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return False, False, str(e), False


def handle_commit_command(
    parsed_args: argparse.Namespace,
    on_auto_sync: Optional[Callable[[Path, str, bool], None]] = None,
) -> int:
    project_root: Path = parsed_args.dir.resolve()
    if not project_root.exists() or not project_root.is_dir():
        print(f"エラー: 指定されたディレクトリが存在しません: {project_root}", file=sys.stderr)
        return 1

    if getattr(parsed_args, "clear", False):
        clear_pending_commit(project_root)
        print("🗑 保持されていたコミット情報を破棄しました。")
        return 0

    pending = load_pending_commit(project_root)
    explicit_msg = getattr(parsed_args, "message", None)
    if explicit_msg and explicit_msg.strip():
        commit_msg = explicit_msg.strip()
        msg_source = "指定"
    elif pending and pending.commit_message:
        commit_msg = pending.commit_message
        msg_source = "保持"
    elif pending and pending.modified_files:
        commit_msg = "chore: apply patch"
        msg_source = "デフォルト"
    else:
        print("ℹ 保持されているコミットメッセージがありません。", file=sys.stderr)
        return 1

    stage_all = getattr(parsed_args, "all", False)
    pending_paths: Set[Path] = {
        (project_root / rel).resolve() for rel in (pending.modified_files if pending else [])
    }

    print(f"📦 {msg_source}コミットメッセージ: {commit_msg}")
    if stage_all:
        print("📁 対象: リポジトリ内の全変更 (--all)")
    elif pending and pending.modified_files:
        preview = pending.modified_files[:10]
        extra = len(pending.modified_files) - len(preview)
        files_str = ", ".join(preview) + (f" ... (+{extra}件)" if extra > 0 else "")
        print(f"📁 対象ファイル ({len(pending.modified_files)}件): {files_str}")

    if getattr(parsed_args, "dry_run", False):
        print("[DRY-RUN] 実際のステージングおよびコミットは実行しませんでした。")
        return 0

    test_option = getattr(parsed_args, "test", None)
    test_failed = False
    if test_option is not None:
        test_ok, test_output = run_validation_test(project_root, test_option)
        if test_ok:
            print("✅ 検証テストに成功しました。")
        else:
            test_failed = True
            print("\n❌ 検証テストが失敗しました。", file=sys.stderr)
            if test_output:
                print("\n--- テスト失敗ログ ---", file=sys.stderr)
                print(test_output, file=sys.stderr)
                print("----------------------\n", file=sys.stderr)

    should_commit = False if test_failed else getattr(parsed_args, "yes", False)
    if not should_commit:
        prompt_msg = (
            "テストが失敗しましたが、コミットを実行しますか？ [Enter/Space: y, 他: n]: "
            if test_failed
            else "このメッセージでコミットを実行しますか？ [Enter/Space: y, 他: n]: "
        )
        should_commit = prompt_user_confirmation(prompt_msg, default=False)

    if not should_commit:
        print("ℹ コミットをスキップしました。コミットメッセージは保持されたままです。")
        return 1 if test_failed else 0

    print("コミットを実行します...")
    auto_push = getattr(parsed_args, "push", False)
    committed, had_diff, err, push_ok = execute_git_commit(
        project_root,
        commit_msg,
        target_files=pending_paths,
        stage_all=stage_all,
        auto_push=auto_push,
    )

    if not committed and not had_diff and err is None and not stage_all:
        try:
            status_out = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=project_root, encoding="utf-8"
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as status_err:
            err = str(status_err)
            status_out = ""

        if err is None:
            if status_out:
                if prompt_user_confirmation(
                    "⚠ 保持対象ファイルに差分がありませんが、他の未コミット変更が存在します。全変更を含めてコミットしますか？ [Enter/Space: y, 他: n]: ",
                    default=False,
                ):
                    committed, had_diff, err, push_ok = execute_git_commit(
                        project_root,
                        commit_msg,
                        stage_all=True,
                        auto_push=auto_push,
                    )
                else:
                    print("ℹ コミットをスキップしました。全変更を含める場合は '-a' を指定してください。")
                    return 0
            else:
                print("ℹ コミット対象の変更がありません (すでにコミット済みまたは変更なし)。")
                clear_pending_commit(project_root)
                return 0

    if err is not None:
        print(f"⚠ コミットに失敗しました: {err}", file=sys.stderr)
        return 1

    if committed:
        clear_pending_commit(project_root)
        if not getattr(parsed_args, "no_sync", False) and on_auto_sync:
            on_auto_sync(project_root, "コミット", False)
        if auto_push and not push_ok:
            return 1

    return 0
