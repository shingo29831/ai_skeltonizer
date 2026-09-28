"""Module: @role: ファイルシステムの更新を検知し、指定された間隔で自動同期処理をトリガーする。"""
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .scanner import get_target_files


def _get_file_snapshot(files: List[Path]) -> Dict[Path, Tuple[int, int]]:
    snapshot = {}
    for p in files:
        try:
            stat = p.stat()
            snapshot[p] = (stat.st_mtime_ns, stat.st_size)
        except (FileNotFoundError, PermissionError):
            pass
    return snapshot


def watch_project(
    project_root: Path,
    sync_callback: Callable[[], Tuple[int, int, int, Optional[Path]]],
    interval_sec: float = 1.0,
    debounce_sec: float = 0.5,
) -> None:
    """プロジェクトの変更を監視し、変更検知時に自動でコールバックを実行する。"""
    print(f"👀 監視を開始しました: {project_root} (ポーリング間隔: {interval_sec}秒)")
    print("   ファイルの変更を検知すると、裏で自動的に aiskel 同期を実行します。 (終了: Ctrl+C)\n")

    target_files = get_target_files(project_root)
    last_snapshot = _get_file_snapshot(target_files)

    try:
        while True:
            time.sleep(interval_sec)
            current_files = get_target_files(project_root)
            current_snapshot = _get_file_snapshot(current_files)

            changed = False
            if set(current_snapshot.keys()) != set(last_snapshot.keys()):
                changed = True
            else:
                for path, state in current_snapshot.items():
                    if last_snapshot.get(path) != state:
                        changed = True
                        break

            if changed:
                # なぜ必要か: エディタの一括保存や多重書き込みによる同期の多重実行を防止
                time.sleep(debounce_sec)
                updated_files = get_target_files(project_root)
                last_snapshot = _get_file_snapshot(updated_files)

                now_str = datetime.now().strftime("%H:%M:%S")
                print(f"[{now_str}] ⚡ ファイル変更を検知しました。同期を開始します...")
                try:
                    updated, skipped, deleted, _ = sync_callback()
                    print(f"[{now_str}] ✔ 同期完了 (更新: {updated}件, 削除: {deleted}件)")
                except Exception as e:
                    print(f"[{now_str}] ✖ 同期中にエラーが発生しました: {e}")

    except KeyboardInterrupt:
        print("\n🛑 ファイル監視を停止しました。")
