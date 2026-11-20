"""Module: @role: Linux環境においてAT-SPIとキーボードエミュレーションを用いてブラウザ内のプロンプト入力欄へフォーカスし、Ctrl+V貼り付けを実行する。"""
import os
import shutil
import subprocess
import time
from typing import Any, List, Optional, Sequence, Set, Tuple


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


def _wait_active_atspi_window(
    atspi_mod: Any,
    allowed_title_keywords: Sequence[str],
    timeout_sec: float = 1.0,
) -> Tuple[Optional[Any], str]:
    desktop = atspi_mod.get_desktop(0)
    if desktop is None:
        return None, "AT-SPIデスクトップの取得に失敗しました"

    deadline = time.monotonic() + timeout_sec
    last_title = ""
    while time.monotonic() < deadline:
        for app_idx in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(app_idx)
            if app is None:
                continue
            for win_idx in range(app.get_child_count()):
                win = app.get_child_at_index(win_idx)
                if win is None:
                    continue
                try:
                    states = win.get_state_set()
                    if not states.contains(atspi_mod.StateType.ACTIVE):
                        continue
                    last_title = (win.get_name() or "").strip()
                    if allowed_title_keywords:
                        title_lower = last_title.lower()
                        if not any(kw in title_lower for kw in allowed_title_keywords):
                            return None, f"許可対象外のページのため自動貼り付けをスキップしました (title: {last_title})"
                    return win, ""
                except Exception:
                    continue
        time.sleep(0.05)

    return None, "アクティブなブラウザウィンドウをAT-SPIで検出できませんでした"


def _send_linux_ctrl_v(is_wayland_only: bool) -> Tuple[bool, str]:
    if is_wayland_only:
        if shutil.which("wtype"):
            res = subprocess.run(["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"], capture_output=True, timeout=2)
            return (res.returncode == 0, "" if res.returncode == 0 else "wtypeでのCtrl+V送信に失敗しました")
        if shutil.which("ydotool"):
            # keycode 29=LEFTCTRL, 47=V
            res = subprocess.run(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], capture_output=True, timeout=2)
            return (res.returncode == 0, "" if res.returncode == 0 else "ydotoolでのCtrl+V送信に失敗しました")
        return False, "純粋Wayland環境で貼り付けツール(wtype/ydotool)が利用できません"

    if not shutil.which("xdotool"):
        return False, "xdotoolがインストールされていません"

    res = subprocess.run(
        ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
        capture_output=True,
        timeout=2,
    )
    return (res.returncode == 0, "" if res.returncode == 0 else "xdotoolでのCtrl+V送信に失敗しました")


def paste_linux_browser_prompt(
    prompt_labels: Sequence[str],
    target_window_id: Optional[str] = None,
    allowed_title_keywords: Sequence[str] = (),
    allow_non_empty: bool = False,
) -> Tuple[bool, str]:
    """Linuxのアクティブブラウザ上でプロンプト入力欄をフォーカスし、Ctrl+Vを送信する。"""
    has_x11 = bool(os.environ.get("DISPLAY"))
    has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if not has_x11 and not has_wayland:
        return False, "GUIディスプレイ環境変数が設定されていません"
    if not prompt_labels:
        return False, "プロンプトラベルが指定されていません"

    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as e:
        return False, f"PyGObject (Atspi 2.0) が利用できません: {e}"

    _ = target_window_id  # AT-SPI側ではアクティブウィンドウ状態とタイトルで検証する
    active_win, win_err = _wait_active_atspi_window(Atspi, allowed_title_keywords)
    if active_win is None:
        return False, win_err

    labels_lower = [lbl.strip().lower() for lbl in prompt_labels if lbl.strip()]
    roles = {Atspi.Role.ENTRY, Atspi.Role.TEXT}
    found = _find_atspi_prompt_node(active_win, labels_lower, roles, Atspi)
    if found is None:
        return False, "ページ内に対象のプロンプト入力欄が見つかりませんでした"

    target_node, _, matched_lbl = found
    if not allow_non_empty:
        existing = _get_atspi_existing_text(target_node)
        if existing and existing.lower() != matched_lbl:
            return False, "入力欄に既存テキストが存在するため自動貼り付けをスキップしました"

    comp = target_node.get_component_iface()
    if comp is None or not comp.grab_focus():
        return False, "プロンプト入力欄へのフォーカス設定に失敗しました"

    time.sleep(0.05)
    return _send_linux_ctrl_v(is_wayland_only=(has_wayland and not has_x11))
