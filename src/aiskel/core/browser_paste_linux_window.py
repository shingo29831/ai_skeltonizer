"""Module: @role: Linuxブラウザプロセス照合・アクティブウィンドウ待機検証およびキーボードショートカット送信を担当する。"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Set, Tuple

from .browser_paste_linux_atspi import _clear_atspi_single_cache, _disable_atspi_cache
from .browser_paste_types import (
    PasteErrorKind,
    PasteOutcome,
    is_loading_or_unready_title,
    is_login_or_auth_title,
)

_LINUX_BROWSER_BINARY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "googlechrome": ("chrome",),
    "microsoftedge": ("msedge",),
    "chromiumbrowser": ("chromium",),
}

def _normalize_linux_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.strip().lower())

def _build_linux_hint_norms(browser_hints: Sequence[str]) -> Set[str]:
    hint_norms: Set[str] = set()
    for raw_hint in browser_hints:
        norm = _normalize_linux_token(raw_hint)
        if len(norm) >= 2:
            hint_norms.add(norm)
            for alias in _LINUX_BROWSER_BINARY_ALIASES.get(norm, ()):
                hint_norms.add(alias)
        # ラッパーコマンドのsymlink先実体名(例: google-chrome -> chrome)も照合対象に含める
        resolved_bin = shutil.which(raw_hint.strip())
        if resolved_bin:
            try:
                real_norm = _normalize_linux_token(Path(os.path.realpath(resolved_bin)).name)
                if len(real_norm) >= 2:
                    hint_norms.add(real_norm)
            except OSError:
                pass
    return hint_norms

def _matches_browser_hint(candidate_norm: str, hint_norms: Set[str], allow_comm_truncation: bool = False) -> bool:
    if not candidate_norm:
        return False
    for hint in hint_norms:
        if candidate_norm == hint:
            return True
        if len(hint) >= 4 and hint in candidate_norm:
            return True
        if len(candidate_norm) >= 5 and hint.endswith(candidate_norm):
            return True
        # Linuxの/proc/<pid>/commは15文字で切り詰められるため完全前方一致のみ救済する
        if allow_comm_truncation and len(candidate_norm) == 15 and hint.startswith(candidate_norm):
            return True
    return False

def _is_atspi_app_default_browser(app: Any, hint_norms: Set[str]) -> bool:
    if not hint_norms:
        return False

    try:
        app_norm = _normalize_linux_token(app.get_name() or "")
        if _matches_browser_hint(app_norm, hint_norms, allow_comm_truncation=False):
            return True
    except Exception:
        pass

    try:
        pid = app.get_process_id()
        if pid and pid > 0:
            for proc_path in (Path(f"/proc/{pid}/exe"), Path(f"/proc/{pid}/comm")):
                try:
                    is_comm = proc_path.name == "comm"
                    raw = (
                        proc_path.read_text(encoding="utf-8", errors="ignore").strip()
                        if is_comm
                        else Path(os.readlink(proc_path)).name
                    )
                    proc_norm = _normalize_linux_token(raw)
                    if _matches_browser_hint(proc_norm, hint_norms, allow_comm_truncation=is_comm):
                        return True
                except OSError:
                    continue
            try:
                cmdline_raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                argv0 = cmdline_raw.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
                if argv0:
                    argv0_norm = _normalize_linux_token(Path(argv0).name)
                    if _matches_browser_hint(argv0_norm, hint_norms, allow_comm_truncation=False):
                        return True
            except OSError:
                pass
    except Exception:
        pass
    return False

def _parse_x11_window_id(raw_id: Optional[str]) -> Optional[int]:
    if not raw_id:
        return None
    try:
        return int(raw_id.strip(), 0)
    except ValueError:
        return None

def _get_x11_active_window_id() -> Optional[int]:
    if not shutil.which("xdotool"):
        return None
    try:
        res = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=1)
        if res.returncode == 0:
            return _parse_x11_window_id(res.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return None

def _is_x11_window_alive(win_id: int) -> bool:
    if not shutil.which("xdotool"):
        return True
    try:
        res = subprocess.run(["xdotool", "getwindowname", str(win_id)], capture_output=True, text=True, timeout=1)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True

def _has_any_active_atspi_window(atspi_mod: Any, browser_hints: Sequence[str]) -> Tuple[bool, bool]:
    """全AT-SPIアプリからアクティブウィンドウ有無とブラウザ一致有無を返す。"""
    desktop = atspi_mod.get_desktop(0)
    if desktop is None:
        return False, False
    _disable_atspi_cache(desktop, atspi_mod)
    hint_norms = _build_linux_hint_norms(browser_hints)
    for app_idx in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(app_idx)
        if app is None:
            continue
        is_browser = _is_atspi_app_default_browser(app, hint_norms)
        for win_idx in range(app.get_child_count()):
            win = app.get_child_at_index(win_idx)
            if win is None:
                continue
            try:
                _clear_atspi_single_cache(win)
                if win.get_state_set().contains(atspi_mod.StateType.ACTIVE):
                    return True, is_browser
            except Exception:
                continue
    return False, False

def _validate_linux_window_active(
    active_win: Any,
    atspi_mod: Any,
    browser_hints: Sequence[str],
    has_x11: bool,
    expected_x11_id: Optional[int],
    aborted_msg: str,
) -> Optional[PasteOutcome]:
    _clear_atspi_single_cache(active_win)
    try:
        states = active_win.get_state_set()
        is_active = states.contains(atspi_mod.StateType.ACTIVE)
        is_defunct = states.contains(atspi_mod.StateType.DEFUNCT)
    except Exception:
        is_active = False
        is_defunct = True

    if has_x11 and expected_x11_id is not None:
        cur_x11_id = _get_x11_active_window_id()
        if cur_x11_id is None:
            if not is_active:
                return PasteOutcome(
                    False,
                    "ウィンドウフォーカスが一時的に外れたため再試行します",
                    True,
                    PasteErrorKind.WINDOW_LOST,
                )
            return None
        if cur_x11_id != expected_x11_id:
            if not _is_x11_window_alive(expected_x11_id):
                _, is_browser_active = _has_any_active_atspi_window(atspi_mod, browser_hints)
                if is_browser_active:
                    return PasteOutcome(
                        False,
                        "対象ブラウザウィンドウが新ウィンドウへ切り替わったため再試行します",
                        True,
                        PasteErrorKind.TRANSIENT,
                        target_window=hex(cur_x11_id),
                    )
            return PasteOutcome(False, aborted_msg, False, PasteErrorKind.USER_ABORTED)
        if not is_defunct:
            return None

    if not is_active:
        # 生存中ウィンドウがACTIVEを失った場合はXWayland/システムダイアログ等への操作移譲とみなしフォーカス奪取しない
        if not is_defunct:
            return PasteOutcome(False, aborted_msg, False, PasteErrorKind.USER_ABORTED)
        has_active, is_browser_active = _has_any_active_atspi_window(atspi_mod, browser_hints)
        if has_active and is_browser_active:
            return PasteOutcome(
                False,
                "対象ブラウザウィンドウが新ウィンドウへ切り替わったため再試行します",
                True,
                PasteErrorKind.TRANSIENT,
            )
        if not has_active:
            return PasteOutcome(
                False,
                "ウィンドウフォーカスが一時的に外れたため再試行します",
                True,
                PasteErrorKind.WINDOW_LOST,
            )
        return PasteOutcome(False, aborted_msg, False, PasteErrorKind.USER_ABORTED)
    return None

def _wait_active_atspi_window(
    atspi_mod: Any,
    allowed_title_keywords: Sequence[str],
    browser_hints: Sequence[str],
    timeout_sec: float = 1.0,
) -> Tuple[Optional[Any], str, bool, PasteErrorKind]:
    desktop = atspi_mod.get_desktop(0)
    if desktop is None:
        return None, "AT-SPIデスクトップの取得に失敗しました", False, PasteErrorKind.UNSUPPORTED

    _disable_atspi_cache(desktop, atspi_mod)
    hint_norms = _build_linux_hint_norms(browser_hints)
    deadline = time.monotonic() + timeout_sec
    while True:
        last_browser_title: Optional[str] = None
        last_loading_title: Optional[str] = None
        saw_non_browser_active = False
        for app_idx in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(app_idx)
            if app is None:
                continue
            is_browser = _is_atspi_app_default_browser(app, hint_norms)
            for win_idx in range(app.get_child_count()):
                win = app.get_child_at_index(win_idx)
                if win is None:
                    continue
                try:
                    _clear_atspi_single_cache(win)
                    states = win.get_state_set()
                    if not states.contains(atspi_mod.StateType.ACTIVE):
                        continue
                    if not is_browser:
                        saw_non_browser_active = True
                        continue
                    win_title = (win.get_name() or "").strip()
                    if is_login_or_auth_title(win_title):
                        return (
                            None,
                            f"認証・ログイン画面のため自動貼り付けを中断しました (title: {win_title})",
                            False,
                            PasteErrorKind.LOGIN_REQUIRED,
                        )
                    if is_loading_or_unready_title(win_title):
                        last_loading_title = win_title
                        continue
                    if allowed_title_keywords:
                        title_lower = win_title.lower()
                        if not any(kw in title_lower for kw in allowed_title_keywords):
                            last_browser_title = win_title
                            continue
                    return win, "", False, PasteErrorKind.NONE
                except Exception:
                    continue

        # アクティブウィンドウが確定済みの場合は残余タイムアウトを待たずに即座に判定を返す
        if last_loading_title is not None:
            return (
                None,
                f"サイトを読み込み中のため完了を待機しています (title: {last_loading_title})",
                True,
                PasteErrorKind.TRANSIENT,
            )
        if last_browser_title is not None:
            return (
                None,
                f"許可対象外のページのため自動貼り付けをスキップしました (title: {last_browser_title})",
                True,
                PasteErrorKind.TRANSIENT,
            )
        if saw_non_browser_active:
            return (
                None,
                "別アプリがアクティブなため自動貼り付けを中断しました",
                False,
                PasteErrorKind.USER_ABORTED,
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    return None, "アクティブなブラウザウィンドウをAT-SPIで検出できませんでした", True, PasteErrorKind.WINDOW_LOST

def _send_linux_shortcut(
    is_wayland_only: bool,
    shortcut_label: str,
    wtype_key: str,
    ydotool_keycode: str,
    xdotool_combo: str,
    wayland_missing_msg: str,
    target_window: Optional[str] = None,
) -> PasteOutcome:
    if is_wayland_only:
        if shutil.which("wtype"):
            res = subprocess.run(["wtype", "-M", "ctrl", "-k", wtype_key, "-m", "ctrl"], capture_output=True, timeout=2)
            if res.returncode == 0:
                return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=target_window)
            return PasteOutcome(
                False, f"wtypeでの{shortcut_label}送信に失敗しました", False, PasteErrorKind.FAILED, target_window=target_window
            )
        if shutil.which("ydotool"):
            # keycode 29=LEFTCTRL
            res = subprocess.run(
                ["ydotool", "key", "29:1", f"{ydotool_keycode}:1", f"{ydotool_keycode}:0", "29:0"],
                capture_output=True,
                timeout=2,
            )
            if res.returncode == 0:
                return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=target_window)
            return PasteOutcome(
                False, f"ydotoolでの{shortcut_label}送信に失敗しました", False, PasteErrorKind.FAILED, target_window=target_window
            )
        return PasteOutcome(False, wayland_missing_msg, False, PasteErrorKind.UNSUPPORTED)

    if not shutil.which("xdotool"):
        return PasteOutcome(False, "xdotoolがインストールされていません", False, PasteErrorKind.UNSUPPORTED)

    res = subprocess.run(
        ["xdotool", "key", "--clearmodifiers", xdotool_combo],
        capture_output=True,
        timeout=2,
    )
    if res.returncode == 0:
        return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=target_window)
    return PasteOutcome(
        False, f"xdotoolでの{shortcut_label}送信に失敗しました", False, PasteErrorKind.FAILED, target_window=target_window
    )

def _send_linux_ctrl_v(is_wayland_only: bool, target_window: Optional[str] = None) -> PasteOutcome:
    return _send_linux_shortcut(
        is_wayland_only=is_wayland_only,
        shortcut_label="Ctrl+V",
        wtype_key="v",
        ydotool_keycode="47",
        xdotool_combo="ctrl+v",
        wayland_missing_msg="純粋Wayland環境で貼り付けツール(wtype/ydotool)が利用できません",
        target_window=target_window,
    )

def _send_linux_ctrl_enter(is_wayland_only: bool, target_window: Optional[str] = None) -> PasteOutcome:
    return _send_linux_shortcut(
        is_wayland_only=is_wayland_only,
        shortcut_label="Ctrl+Enter",
        wtype_key="Return",
        ydotool_keycode="28",
        xdotool_combo="ctrl+Return",
        wayland_missing_msg="純粋Wayland環境で送信ツール(wtype/ydotool)が利用できません",
        target_window=target_window,
    )
