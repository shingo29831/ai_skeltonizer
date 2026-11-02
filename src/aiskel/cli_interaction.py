"""Module: @role: CLI対話プロンプト、キー入力判定、コミットタグ抽出、およびGit復元操作を担当する。"""
import os
import re
import sys
import subprocess
from pathlib import Path
from typing import List, Optional, Set, Tuple


def extract_commit_message(patch_text: str) -> Tuple[Optional[str], str]:
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


def rollback_modified_files(project_root: Path, modified_files: Set[Path]) -> bool:
    # なぜ必要か: 新規作成ファイル(未追跡)の残留を防ぎ、Git経由で完全ロールバックを保証
    try:
        for f in modified_files:
            res = subprocess.run(["git", "checkout", "--", str(f)], cwd=project_root, capture_output=True)
            if res.returncode != 0:
                subprocess.run(["git", "clean", "-f", str(f)], cwd=project_root, capture_output=True)
        print("🔄 変更ファイルを元の状態に復元しました。")
        return True
    except Exception as e:
        print(f"⚠ ロールバックに失敗しました: {e}", file=sys.stderr)
        return False
