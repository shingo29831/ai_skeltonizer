"""Module: @role: CLI対話プロンプト、キー入力判定、コミットタグ抽出、およびGit復元操作を担当する。"""
import os
import re
import sys
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


def _find_patch_block_spans(patch_text: str) -> List[Tuple[int, int]]:
    """パッチの置換ブロック(<<<< 〜 >>>>)が占める文字オフセット範囲一覧を返す。"""
    start_re = re.compile(r"^[ \t]*<{4,}\s*$")
    mid_re = re.compile(r"^[ \t]*={4,}\s*$")
    end_re = re.compile(r"^[ \t]*>{4,}\s*$")
    spans: List[Tuple[int, int]] = []
    offset = 0
    block_start: Optional[int] = None
    has_mid = False
    for line in patch_text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if block_start is None:
            if start_re.match(stripped):
                block_start = offset
                has_mid = False
        else:
            if not has_mid and mid_re.match(stripped):
                has_mid = True
            elif has_mid and end_re.match(stripped):
                spans.append((block_start, offset + len(line)))
                block_start = None
                has_mid = False
        offset += len(line)
    return spans


def extract_commit_message(patch_text: str) -> Tuple[Optional[str], str]:
    # コード内の単発開始タグが末尾閉じタグまでパッチ本体を飲み込むのを防ぐためタグ分割＋非横断マッチを使用
    tag_start = "<" + "commit>"
    tag_end = "</" + "commit>"
    inner_pattern = r"((?:(?!" + re.escape(tag_start) + r").)*?)"
    pattern = re.compile(
        re.escape(tag_start) + r"\s*" + inner_pattern + r"\s*" + re.escape(tag_end),
        re.DOTALL | re.IGNORECASE,
    )

    block_spans = _find_patch_block_spans(patch_text)
    valid_matches = [
        m
        for m in pattern.finditer(patch_text)
        if not any(m.start() < span_end and m.end() > span_start for span_start, span_end in block_spans)
    ]
    if valid_matches:
        last_match = valid_matches[-1]
        msg = last_match.group(1).strip()
        msg = re.sub(r'^`{1,5}[a-zA-Z]*\s*', '', msg)
        msg = re.sub(r'\s*`{1,5}$', '', msg).strip()
        clean_patch_text = patch_text[:last_match.start()] + patch_text[last_match.end():]
        return (msg if msg else None), clean_patch_text
    return None, patch_text


def read_single_key_from_tty() -> Optional[str]:
    # なぜ必要か: Enter/Space/他キーの1打鍵即時判定をクロスプラットフォームで実現するため
    if sys.platform == "win32":
        try:
            import msvcrt
            ch = msvcrt.getwch()
            while msvcrt.kbhit():
                msvcrt.getwch()
            if ch in ('\x00', '\xe0'):
                return ""
            if ch == '\x03':
                raise KeyboardInterrupt
            return ch
        except Exception:
            return None
    else:
        try:
            import termios
            import tty
            tty_file = None
            if sys.stdin.isatty():
                fd = sys.stdin.fileno()
                stream = sys.stdin
            else:
                try:
                    tty_file = open("/dev/tty", "r", encoding="utf-8")
                    fd = tty_file.fileno()
                    stream = tty_file
                except OSError:
                    return None

            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                ch = stream.read(1)
                try:
                    termios.tcflush(fd, termios.TCIFLUSH)
                except Exception:
                    pass
                return ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                if tty_file:
                    tty_file.close()
        except Exception:
            return None


def prompt_user_confirmation(prompt: str, default: bool = False) -> bool:
    # なぜ必要か: Enter/Spaceで肯定、他キーで否定を即時判定し非対話時も安全に処理するため
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()

        key = read_single_key_from_tty()
        if key is not None:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return key in ("\r", "\n", " ", "y", "Y")

        if not sys.stdin.isatty():
            tty_path = "CON" if sys.platform == "win32" else "/dev/tty"
            try:
                with open(tty_path, "r", encoding="utf-8") as tty:
                    raw_line = tty.readline()
            except OSError:
                return default
        else:
            raw_line = sys.stdin.readline()

        if not raw_line:
            return default

        ans = raw_line.strip().lower()
        if not ans:
            return True
        return ans in ("y", "yes", " ")
    except (EOFError, KeyboardInterrupt):
        return False


def _cleanup_empty_dirs_up_to_root(start_dir: Path, resolved_root: Path) -> None:
    cur = start_dir
    while cur != resolved_root:
        try:
            cur.relative_to(resolved_root)
        except ValueError:
            break
        if not cur.is_dir():
            break
        try:
            if any(cur.iterdir()):
                break
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent

def _resolve_under_root(path: Path, resolved_root: Path) -> Path:
    return path.resolve() if path.is_absolute() else (resolved_root / path).resolve()

def rollback_modified_files(
    project_root: Path,
    modified_files: Set[Path],
    snapshot: Optional[Dict[Path, Optional[bytes]]] = None,
) -> bool:
    # なぜ必要か: 非Git環境・未コミット変更・新規作成ファイル残留を防ぎ、適用前状態へ確実に復元するため
    resolved_root = project_root.resolve()
    snap_map = snapshot or {}
    try:
        resolved_snap: Dict[Path, Optional[bytes]] = {}
        delete_first_targets: List[Path] = []
        restore_targets: List[Path] = []
        for raw_k, orig_bytes in snap_map.items():
            rk = _resolve_under_root(raw_k, resolved_root)
            resolved_snap[rk] = orig_bytes
            if orig_bytes is None:
                delete_first_targets.append(raw_k)
            else:
                restore_targets.append(raw_k)

        # 大文字小文字のみ変更された新規側の削除を先に実行し、元のファイル名大文字小文字を復元可能にする
        handled_resolved: Set[Path] = set()
        for raw_p in delete_first_targets:
            resolved_f = _resolve_under_root(raw_p, resolved_root)
            handled_resolved.add(resolved_f)
            if resolved_f.exists() and resolved_f.is_file():
                resolved_f.unlink()
            _cleanup_empty_dirs_up_to_root(resolved_f.parent, resolved_root)

        for raw_p in restore_targets:
            resolved_f = _resolve_under_root(raw_p, resolved_root)
            handled_resolved.add(resolved_f)
            orig_bytes = resolved_snap[resolved_f]
            if orig_bytes is None:
                continue
            resolved_f.parent.mkdir(parents=True, exist_ok=True)
            if resolved_f.exists():
                try:
                    if resolved_f.resolve().name != raw_p.name:
                        resolved_f.unlink()
                except OSError:
                    pass
            resolved_f.write_bytes(orig_bytes)

        for f in modified_files:
            resolved_f = _resolve_under_root(f, resolved_root)
            if resolved_f in handled_resolved:
                continue
            handled_resolved.add(resolved_f)
            res = subprocess.run(["git", "checkout", "--", str(f)], cwd=project_root, capture_output=True)
            if res.returncode != 0:
                subprocess.run(["git", "clean", "-f", str(f)], cwd=project_root, capture_output=True)
                _cleanup_empty_dirs_up_to_root(resolved_f.parent, resolved_root)
        print("🔄 変更ファイルを元の状態に復元しました。")
        return True
    except Exception as e:
        print(f"⚠ ロールバックに失敗しました: {e}", file=sys.stderr)
        return False


def prompt_user_command(prompt: str) -> Optional[str]:
    # なぜ必要か: パイプ入力後でも対話プロンプトを受け付け、擬似stdin(テスト等)のハングも防ぐため
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()

        has_fileno = True
        try:
            sys.stdin.fileno()
        except Exception:
            has_fileno = False

        if sys.stdin.isatty() or not has_fileno:
            raw_line = sys.stdin.readline()
        else:
            tty_path = "CON" if sys.platform == "win32" else "/dev/tty"
            try:
                with open(tty_path, "r", encoding="utf-8") as tty:
                    raw_line = tty.readline()
            except OSError:
                return None

        if not raw_line:
            return None
        return raw_line.rstrip("\r\n")
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        sys.stdout.flush()
        return None
