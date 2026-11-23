"""Module: @role: Linux環境においてAT-SPIとキーボードエミュレーションを用いてブラウザ内のプロンプト入力欄へフォーカスし、Ctrl+V貼り付けを実行する。"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .browser_focus_linux import _get_linux_browser_wm_hints
from .browser_paste_types import PasteErrorKind, PasteOutcome, is_login_or_auth_title

_LINUX_BROWSER_BINARY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "googlechrome": ("chrome",),
    "microsoftedge": ("msedge",),
    "chromiumbrowser": ("chromium",),
}


def _collect_atspi_node_texts(node: Any, atspi_mod: Any) -> List[str]:
    texts: List[str] = []
    try:
        name = (node.get_name() or "").strip()
        if name:
            texts.append(name)
        desc = (node.get_description() or "").strip()
        if desc:
            texts.append(desc)
        attrs = node.get_attributes() or {}
        for key in ("placeholder-text", "placeholder", "aria-placeholder"):
            val = (attrs.get(key) or "").strip()
            if val:
                texts.append(val)
    except Exception:
        pass

    if not texts:
        try:
            count = min(node.get_child_count(), 4)
            for i in range(count):
                child = node.get_child_at_index(i)
                if child is None:
                    continue
                if child.get_role() in (atspi_mod.Role.STATIC, atspi_mod.Role.PARAGRAPH, atspi_mod.Role.TEXT):
                    c_name = (child.get_name() or "").strip()
                    if c_name:
                        texts.append(c_name)
        except Exception:
            pass
    return texts


def _score_atspi_texts(texts: Sequence[str], labels_lower: Sequence[str]) -> Tuple[int, str]:
    best = 0
    matched_lbl = ""
    for raw in texts:
        norm = raw.strip().lower()
        if not norm:
            continue
        for lbl in labels_lower:
            if norm == lbl:
                return 100, lbl
            if lbl in norm or (len(norm) >= 4 and norm in lbl):
                if 60 > best:
                    best = 60
                    matched_lbl = lbl
    return best, matched_lbl


def _find_atspi_prompt_node(
    node: Any,
    labels_lower: Sequence[str],
    roles: Set[Any],
    atspi_mod: Any,
    depth: int = 0,
) -> Optional[Tuple[Any, int, str]]:
    if node is None or depth > 48:
        return None

    best_match: Optional[Tuple[Any, int, str]] = None
    try:
        role = node.get_role()
        state_set = node.get_state_set()
        is_input = role in roles or state_set.contains(atspi_mod.StateType.EDITABLE)
        if is_input:
            is_enabled = state_set.contains(atspi_mod.StateType.ENABLED)
            is_visible = state_set.contains(atspi_mod.StateType.VISIBLE)
            is_focusable = state_set.contains(atspi_mod.StateType.FOCUSABLE)
            if is_enabled and is_visible and is_focusable and role != atspi_mod.Role.PASSWORD_TEXT:
                texts = _collect_atspi_node_texts(node, atspi_mod)
                score, matched_lbl = _score_atspi_texts(texts, labels_lower)
                if score > 0:
                    best_match = (node, score, matched_lbl)
                    if score >= 100:
                        return best_match

        count = node.get_child_count()
        for i in range(count):
            child_res = _find_atspi_prompt_node(
                node.get_child_at_index(i), labels_lower, roles, atspi_mod, depth + 1
            )
            if child_res is not None:
                if best_match is None or child_res[1] > best_match[1]:
                    best_match = child_res
                if best_match[1] >= 100:
                    return best_match
    except Exception:
        return best_match
    return best_match


def _get_atspi_existing_text(node: Any) -> str:
    try:
        text_iface = node.get_text_iface()
        if text_iface is not None:
            char_count = text_iface.get_character_count()
            if char_count > 0:
                return (text_iface.get_text(0, char_count) or "").strip()
    except Exception:
        pass
    return ""


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


def _wait_active_atspi_window(
    atspi_mod: Any,
    allowed_title_keywords: Sequence[str],
    browser_hints: Sequence[str],
    timeout_sec: float = 1.0,
) -> Tuple[Optional[Any], str, bool, PasteErrorKind]:
    desktop = atspi_mod.get_desktop(0)
    if desktop is None:
        return None, "AT-SPIデスクトップの取得に失敗しました", False, PasteErrorKind.UNSUPPORTED

    hint_norms = _build_linux_hint_norms(browser_hints)
    deadline = time.monotonic() + timeout_sec
    while True:
        last_browser_title: Optional[str] = None
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
                    if allowed_title_keywords:
                        title_lower = win_title.lower()
                        if not any(kw in title_lower for kw in allowed_title_keywords):
                            last_browser_title = win_title
                            continue
                    return win, "", False, PasteErrorKind.NONE
                except Exception:
                    continue

        # アクティブウィンドウが確定済みの場合は残余タイムアウトを待たずに即座に判定を返す
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


def _send_linux_ctrl_v(is_wayland_only: bool, target_window: Optional[str] = None) -> PasteOutcome:
    if is_wayland_only:
        if shutil.which("wtype"):
            res = subprocess.run(["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"], capture_output=True, timeout=2)
            if res.returncode == 0:
                return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=target_window)
            return PasteOutcome(False, "wtypeでのCtrl+V送信に失敗しました", False, PasteErrorKind.FAILED, target_window=target_window)
        if shutil.which("ydotool"):
            # keycode 29=LEFTCTRL, 47=V
            res = subprocess.run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], capture_output=True, timeout=2)
            if res.returncode == 0:
                return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=target_window)
            return PasteOutcome(False, "ydotoolでのCtrl+V送信に失敗しました", False, PasteErrorKind.FAILED, target_window=target_window)
        return PasteOutcome(False, "純粋Wayland環境で貼り付けツール(wtype/ydotool)が利用できません", False, PasteErrorKind.UNSUPPORTED)

    if not shutil.which("xdotool"):
        return PasteOutcome(False, "xdotoolがインストールされていません", False, PasteErrorKind.UNSUPPORTED)

    res = subprocess.run(
        ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
        capture_output=True,
        timeout=2,
    )
    if res.returncode == 0:
        return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=target_window)
    return PasteOutcome(False, "xdotoolでのCtrl+V送信に失敗しました", False, PasteErrorKind.FAILED, target_window=target_window)


def paste_linux_browser_prompt(
    prompt_labels: Sequence[str],
    target_window_id: Optional[str] = None,
    allowed_title_keywords: Sequence[str] = (),
    allow_non_empty: bool = False,
    quick_probe: bool = False,
) -> PasteOutcome:
    """Linuxのアクティブブラウザ上でプロンプト入力欄をフォーカスし、Ctrl+Vを送信する。"""
    has_x11 = bool(os.environ.get("DISPLAY"))
    has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if not has_x11 and not has_wayland:
        return PasteOutcome(False, "GUIディスプレイ環境変数が設定されていません", False, PasteErrorKind.UNSUPPORTED)
    if not prompt_labels:
        return PasteOutcome(False, "プロンプトラベルが指定されていません", False, PasteErrorKind.FAILED)

    browser_hints = _get_linux_browser_wm_hints()
    if not browser_hints:
        return PasteOutcome(False, "デフォルトブラウザの実行情報が特定できませんでした", False, PasteErrorKind.UNSUPPORTED)

    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as e:
        return PasteOutcome(False, f"PyGObject (Atspi 2.0) が利用できません: {e}", False, PasteErrorKind.UNSUPPORTED)

    timeout_sec = 0.5 if quick_probe else 1.0
    active_win, win_err, retryable, err_kind = _wait_active_atspi_window(
        Atspi, allowed_title_keywords, browser_hints, timeout_sec=timeout_sec
    )
    if active_win is None:
        return PasteOutcome(False, win_err, retryable, err_kind)

    resolved_win_id: Optional[str] = target_window_id
    expected_x11_id: Optional[int] = None
    target_x11_id = _parse_x11_window_id(target_window_id)
    if has_x11 and target_x11_id is not None:
        active_x11_id = _get_x11_active_window_id()
        if active_x11_id is not None:
            if active_x11_id != target_x11_id:
                if _is_x11_window_alive(target_x11_id):
                    return PasteOutcome(
                        False,
                        "別のブラウザウィンドウまたは別アプリが最前面になったため中断しました",
                        False,
                        PasteErrorKind.USER_ABORTED,
                    )
                # 一時ウィンドウ終了後にデフォルトブラウザの新ウィンドウへ遷移した場合のみ追従する
                resolved_win_id = hex(active_x11_id)
                expected_x11_id = active_x11_id
            else:
                expected_x11_id = target_x11_id

    labels_lower = [lbl.strip().lower() for lbl in prompt_labels if lbl.strip()]
    roles = {Atspi.Role.ENTRY, Atspi.Role.TEXT}
    found = _find_atspi_prompt_node(active_win, labels_lower, roles, Atspi)
    if found is None:
        return PasteOutcome(
            False,
            "ページ内に対象のプロンプト入力欄が見つかりませんでした",
            True,
            PasteErrorKind.TRANSIENT,
            target_window=resolved_win_id,
        )

    target_node, _, matched_lbl = found
    if not allow_non_empty:
        existing = _get_atspi_existing_text(target_node)
        if existing and existing.lower() != matched_lbl:
            return PasteOutcome(
                False,
                "入力欄に既存テキストが存在するため自動貼り付けをスキップしました",
                False,
                PasteErrorKind.USER_ABORTED,
                target_window=resolved_win_id,
            )

    comp = target_node.get_component_iface()
    if comp is None or not comp.grab_focus():
        return PasteOutcome(
            False,
            "プロンプト入力欄へのフォーカス設定に失敗しました",
            True,
            PasteErrorKind.TRANSIENT,
            target_window=resolved_win_id,
        )

    time.sleep(0.05)
    try:
        if not active_win.get_state_set().contains(Atspi.StateType.ACTIVE):
            return PasteOutcome(
                False,
                "貼り付け直前に別ウィンドウへ切り替わったため中断しました",
                False,
                PasteErrorKind.USER_ABORTED,
            )
    except Exception:
        return PasteOutcome(
            False,
            "貼り付け直前のアクティブウィンドウ確認に失敗したため中断しました",
            True,
            PasteErrorKind.TRANSIENT,
            target_window=resolved_win_id,
        )

    if has_x11 and expected_x11_id is not None:
        cur_x11_id = _get_x11_active_window_id()
        if cur_x11_id is not None and cur_x11_id != expected_x11_id:
            return PasteOutcome(
                False,
                "貼り付け直前に別ウィンドウへ切り替わったため中断しました",
                False,
                PasteErrorKind.USER_ABORTED,
            )

    return _send_linux_ctrl_v(is_wayland_only=(has_wayland and not has_x11), target_window=resolved_win_id)
