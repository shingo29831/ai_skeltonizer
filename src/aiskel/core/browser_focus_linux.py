"""Module: @role: Linux環境における.desktop設定からのブラウザヒント抽出およびwmctrl/xdotoolを用いた最上位表示制御を担当する。"""
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional, Set

_IGNORED_LINUX_HINTS: Set[str] = {
    "sh", "bash", "dash", "zsh", "fish", "env", "exec", "flatpak", "snap",
    "gio", "gtk-launch", "xdg-open", "python", "python3", "perl", "ruby",
    "dbus-launch", "nohup", "setsid",
}
_CHANNEL_SUFFIXES = ("-stable", "-beta", "-dev", "-canary", "-nightly", "-bin")


def _append_linux_hint(hints: List[str], raw_hint: str) -> None:
    """有効かつ汎用シェル名でないLinuxヒント文字列のみを重複なく追加する。"""
    hint = raw_hint.strip().lower()
    if len(hint) < 2 or hint in _IGNORED_LINUX_HINTS:
        return
    if hint not in hints:
        hints.append(hint)
    for suffix in _CHANNEL_SUFFIXES:
        if hint.endswith(suffix):
            base = hint[: -len(suffix)]
            if len(base) >= 2 and base not in _IGNORED_LINUX_HINTS and base not in hints:
                hints.append(base)


def _extract_linux_exec_hints(exec_line: str, _depth: int = 0) -> List[str]:
    """.desktopのExec=値から引用符やラッパー(env/sh/flatpak/snap等)を除去して実体ヒントを抽出する。"""
    if _depth > 3:
        return []
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        tokens = exec_line.split()

    tokens = [t for t in tokens if not (len(t) == 2 and t.startswith("%"))]
    if not tokens:
        return []

    hints: List[str] = []
    idx = 0
    while idx < len(tokens):
        cmd_name = Path(tokens[idx]).name.lower()
        if cmd_name == "exec":
            idx += 1
            continue

        if cmd_name == "env":
            idx += 1
            while idx < len(tokens):
                tok = tokens[idx]
                if tok in ("-u", "-C", "-S"):
                    idx += 2
                elif tok.startswith("-") or ("=" in tok and not tok.startswith("/")):
                    idx += 1
                else:
                    break
            continue

        if cmd_name in ("sh", "bash", "dash", "zsh"):
            idx += 1
            while idx < len(tokens) and tokens[idx].startswith("-"):
                flag = tokens[idx]
                idx += 1
                if "c" in flag and idx < len(tokens):
                    return _extract_linux_exec_hints(tokens[idx], _depth + 1)
            continue

        if cmd_name in ("flatpak", "snap") and idx + 1 < len(tokens) and tokens[idx + 1] == "run":
            idx += 2
            while idx < len(tokens) and tokens[idx].startswith("-"):
                idx += 1
            if idx < len(tokens):
                app_ref = tokens[idx].lower()
                _append_linux_hint(hints, app_ref)
                if "." in app_ref:
                    _append_linux_hint(hints, app_ref.rsplit(".", 1)[-1])
            return hints

        if cmd_name == "gtk-launch" and idx + 1 < len(tokens):
            _append_linux_hint(hints, tokens[idx + 1].removesuffix(".desktop"))
            return hints

        if cmd_name == "gio" and idx + 2 < len(tokens) and tokens[idx + 1] == "launch":
            _append_linux_hint(hints, Path(tokens[idx + 2]).stem)
            return hints

        _append_linux_hint(hints, cmd_name)
        return hints

    return hints


def _get_linux_default_desktop_name() -> Optional[str]:
    """xdg-settingsまたはxdg-mimeからデフォルトブラウザの.desktopファイル名を取得する。"""
    commands = (
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "x-scheme-handler/https"],
        ["xdg-mime", "query", "default", "x-scheme-handler/http"],
    )
    for cmd in commands:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            continue
        name = res.stdout.strip()
        if res.returncode == 0 and name:
            return name
    return None


def _get_linux_browser_wm_hints() -> List[str]:
    """Linuxのデフォルトブラウザ設定(.desktop)の[Desktop Entry]からWM_CLASS・コマンド名ヒントを抽出する。"""
    desktop_name = _get_linux_default_desktop_name()
    if not desktop_name:
        return []

    hints: List[str] = []
    _append_linux_hint(hints, desktop_name.removesuffix(".desktop"))
    search_dirs = [
        Path.home() / ".local/share/applications",
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
    ]
    for app_dir in search_dirs:
        desktop_file = app_dir / desktop_name
        if not desktop_file.is_file():
            continue
        try:
            content = desktop_file.read_text(encoding="utf-8", errors="ignore")
            in_desktop_entry = False
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    if in_desktop_entry:
                        break
                    in_desktop_entry = line == "[Desktop Entry]"
                    continue
                if not in_desktop_entry:
                    continue

                if line.startswith("StartupWMClass="):
                    wm_class = line.split("=", 1)[1].strip().lower()
                    if wm_class and wm_class not in _IGNORED_LINUX_HINTS and wm_class not in hints:
                        hints.insert(0, wm_class)
                elif line.startswith("Exec="):
                    for exec_hint in _extract_linux_exec_hints(line.split("=", 1)[1].strip()):
                        _append_linux_hint(hints, exec_hint)
            break
        except OSError:
            continue
    return hints


def _to_case_insensitive_ere(text: str) -> str:
    """POSIX拡張正規表現向けに大文字小文字非依存のパターン文字列を生成する。"""
    parts: List[str] = []
    for ch in text:
        if ch.isalpha() and ch.lower() != ch.upper():
            parts.append(f"[{ch.lower()}{ch.upper()}]")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def focus_linux_default_browser() -> bool:
    """起動済みのLinuxデフォルトブラウザウィンドウをwmctrlまたはxdotoolでフォーカスする。"""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False

    hints = _get_linux_browser_wm_hints()
    if not hints:
        return False

    try:
        res = subprocess.run(["wmctrl", "-lx"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 3:
                    continue
                win_id, wm_class = parts[0], parts[2].lower()
                # instance.Class をトークン分割し完全一致のみで判定して誤爆を防ぐ
                wm_tokens = {wm_class} | {tok for tok in wm_class.split(".") if tok}
                if any(h in wm_tokens for h in hints):
                    act = subprocess.run(["wmctrl", "-i", "-a", win_id], capture_output=True, timeout=2)
                    if act.returncode == 0:
                        return True
    except (OSError, subprocess.SubprocessError):
        pass

    for hint in hints:
        pattern = f"^{_to_case_insensitive_ere(hint)}$"
        for search_flag in ("--class", "--classname"):
            try:
                search = subprocess.run(
                    ["xdotool", "search", "--onlyvisible", search_flag, pattern],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                win_ids = [w.strip() for w in search.stdout.splitlines() if w.strip()]
                if win_ids:
                    act = subprocess.run(["xdotool", "windowactivate", win_ids[-1]], capture_output=True, timeout=2)
                    if act.returncode == 0:
                        return True
            except FileNotFoundError:
                # xdotool未導入環境では残りのヒントに対する不要な起動リトライを行わない
                return False
            except (OSError, subprocess.SubprocessError):
                break

    return False
