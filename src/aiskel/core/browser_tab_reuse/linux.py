"""Module: @role: Linux環境においてAT-SPIおよびwmctrl/xdotool経由で起動済みブラウザの空チャットタブを検出し、選択・最前面化を行う。"""
import os
import subprocess
import time
from collections import deque
from typing import Any, Deque, List, Optional, Sequence, Set, Tuple

from ..browser_focus.linux import _get_linux_browser_wm_hints, focus_linux_default_browser_target
from ..browser_paste_linux import (
    _build_linux_hint_norms,
    _clear_atspi_single_cache,
    _clear_atspi_subtree_cache,
    _collect_atspi_node_texts,
    _disable_atspi_cache,
    _is_atspi_app_default_browser,
    _is_atspi_document_busy,
    _is_atspi_prompt_empty,
    _score_atspi_texts,
)
from ..browser_paste_types import _strip_browser_title_suffix, is_loading_or_unready_title, is_login_or_auth_title
from .types import (
    CHAT_TURN_ACTION_BUTTON_KEYWORDS,
    NEW_CHAT_BUTTON_NAMES,
    is_empty_chat_tab_title,
)

_TAB_VERIFY_TIMEOUT_SEC = 2.0
_DISABLED_NEW_CHAT_TOOLTIPS = (
    "already in a new chat",
    "すでに新しいチャット",
    "既に新しいチャット",
)


def _is_atspi_button_disabled(node: Any, atspi_mod: Any) -> bool:
    try:
        states = node.get_state_set()
        if not states.contains(atspi_mod.StateType.ENABLED):
            return True
        attrs = node.get_attributes() or {}
        if str(attrs.get("aria-disabled") or "").lower() == "true":
            return True
        desc = (node.get_description() or "").lower()
        if desc and any(hint in desc for hint in _DISABLED_NEW_CHAT_TOOLTIPS):
            return True
    except Exception:
        return True
    return False


def _has_positive_atspi_extents(node: Any, atspi_mod: Any) -> bool:
    try:
        comp = node.get_component_iface()
        if comp is None:
            return True
        rect = comp.get_extents(atspi_mod.CoordType.WINDOW)
        return rect.width > 0 and rect.height > 0
    except Exception:
        return True


def _collect_visible_atspi_documents(win: Any, atspi_mod: Any, max_nodes: int = 1200) -> List[Any]:
    """タブUIや非表示タブを除外し、アクティブ表示中のDocumentノード一覧を収集する。"""
    skip_chrome_roles = {
        atspi_mod.Role.MENU,
        atspi_mod.Role.MENU_BAR,
        atspi_mod.Role.MENU_ITEM,
        atspi_mod.Role.POPUP_MENU,
        atspi_mod.Role.TOOL_BAR,
        atspi_mod.Role.STATUS_BAR,
    }
    tab_roles = {atspi_mod.Role.PAGE_TAB_LIST, atspi_mod.Role.PAGE_TAB}
    doc_roles = {atspi_mod.Role.DOCUMENT_WEB, atspi_mod.Role.DOCUMENT_FRAME}

    # 多数のタブヘッダでノード上限を消費しないよう非タブ領域を優先探索する
    main_queue: Deque[Tuple[Any, int]] = deque([(win, 0)])
    tab_queue: Deque[Tuple[Any, int]] = deque()
    docs: List[Any] = []
    visited = 0
    while (main_queue or tab_queue) and visited < max_nodes:
        node, depth = main_queue.popleft() if main_queue else tab_queue.popleft()
        if node is None:
            continue
        visited += 1
        try:
            _clear_atspi_single_cache(node)
            role = node.get_role()
            if role in doc_roles:
                states = node.get_state_set()
                if states.contains(atspi_mod.StateType.VISIBLE) and _has_positive_atspi_extents(node, atspi_mod):
                    docs.append(node)
                continue
            if depth > 0 and role in skip_chrome_roles:
                continue
            if role == atspi_mod.Role.PAGE_TAB:
                states = node.get_state_set()
                if not states.contains(atspi_mod.StateType.VISIBLE):
                    continue
                if states.contains(atspi_mod.StateType.SELECTABLE) and not states.contains(atspi_mod.StateType.SELECTED):
                    continue
            if depth >= 16:
                continue
            target_q = tab_queue if role in tab_roles else main_queue
            for i in range(node.get_child_count()):
                child = node.get_child_at_index(i)
                if child is not None:
                    target_q.append((child, depth + 1))
        except Exception:
            continue
    return docs


def _find_atspi_prompt_in_doc(
    doc: Any,
    labels_lower: Sequence[str],
    roles: Set[Any],
    atspi_mod: Any,
    max_nodes: int = 2500,
) -> Optional[Tuple[Any, str]]:
    """指定Document配下のみから可視・編集可能なプロンプト入力欄を探索する。"""
    queue: Deque[Tuple[Any, int]] = deque([(doc, 0)])
    best_node: Optional[Any] = None
    best_score = 0
    best_label = ""
    visited = 0
    while queue and visited < max_nodes:
        node, depth = queue.popleft()
        if node is None:
            continue
        visited += 1
        try:
            _clear_atspi_single_cache(node)
            role = node.get_role()
            state_set = node.get_state_set()
            is_input = role in roles or state_set.contains(atspi_mod.StateType.EDITABLE)
            if is_input:
                is_enabled = state_set.contains(atspi_mod.StateType.ENABLED)
                is_visible = state_set.contains(atspi_mod.StateType.VISIBLE)
                is_focusable = state_set.contains(atspi_mod.StateType.FOCUSABLE)
                if (
                    is_enabled
                    and is_visible
                    and is_focusable
                    and role != atspi_mod.Role.PASSWORD_TEXT
                    and _has_positive_atspi_extents(node, atspi_mod)
                ):
                    texts = _collect_atspi_node_texts(node, atspi_mod)
                    score, matched_lbl = _score_atspi_texts(texts, labels_lower)
                    if score > best_score:
                        best_node = node
                        best_score = score
                        best_label = matched_lbl
                        if score >= 100:
                            return best_node, best_label
            if depth >= 48:
                continue
            for i in range(node.get_child_count()):
                child = node.get_child_at_index(i)
                if child is not None:
                    queue.append((child, depth + 1))
        except Exception:
            continue
    return (best_node, best_label) if best_node is not None else None


def _inspect_atspi_window_empty_chat(
    win: Any,
    atspi_mod: Any,
    labels_lower: Sequence[str],
    is_aistudio: bool,
) -> Tuple[bool, bool]:
    """AT-SPIウィンドウを検査し、(空チャットか, 非空確定で即終了すべきか) を返す。"""
    if _is_atspi_document_busy(win, atspi_mod):
        return False, False

    docs = _collect_visible_atspi_documents(win, atspi_mod)
    if not docs:
        return False, False

    roles = {atspi_mod.Role.ENTRY, atspi_mod.Role.TEXT}
    found: Optional[Tuple[Any, str]] = None
    target_doc: Optional[Any] = None
    for doc in docs:
        found = _find_atspi_prompt_in_doc(doc, labels_lower, roles, atspi_mod)
        if found is not None:
            target_doc = doc
            break
    if found is None or target_doc is None:
        return False, False

    prompt_node, matched_lbl = found
    if not _is_atspi_prompt_empty(prompt_node, labels_lower, matched_lbl):
        return False, True

    btn_roles = {atspi_mod.Role.PUSH_BUTTON, atspi_mod.Role.TOGGLE_BUTTON}
    max_nodes = 2500
    queue: Deque[Tuple[Any, int]] = deque([(target_doc, 0)])
    visited = 0
    while queue and visited < max_nodes:
        cur, depth = queue.popleft()
        if cur is None:
            continue
        visited += 1
        try:
            _clear_atspi_single_cache(cur)
            role = cur.get_role()
            if role in btn_roles:
                states = cur.get_state_set()
                if states.contains(atspi_mod.StateType.VISIBLE) and _has_positive_atspi_extents(cur, atspi_mod):
                    name_lower = (cur.get_name() or "").strip().lower()
                    if name_lower:
                        if is_aistudio and name_lower in NEW_CHAT_BUTTON_NAMES:
                            if not _is_atspi_button_disabled(cur, atspi_mod):
                                return False, True
                        if name_lower in ("stop", "停止"):
                            return False, True
                        if any(kw in name_lower for kw in CHAT_TURN_ACTION_BUTTON_KEYWORDS):
                            return False, True
            if depth >= 48:
                # 走査打ち切り時は安全側(False)に倒しつつ描画途中リトライを阻害しないよう確定非空にはしない
                if cur.get_child_count() > 0:
                    return False, False
                continue
            for i in range(cur.get_child_count()):
                child = cur.get_child_at_index(i)
                if child is not None:
                    queue.append((child, depth + 1))
        except Exception:
            return False, False
    if queue:
        return False, False
    return True, False


def _is_atspi_window_empty_chat(
    win: Any,
    atspi_mod: Any,
    labels_lower: Sequence[str],
    is_aistudio: bool,
) -> bool:
    is_empty, _ = _inspect_atspi_window_empty_chat(win, atspi_mod, labels_lower, is_aistudio)
    return is_empty


def _collect_atspi_page_tabs(win: Any, atspi_mod: Any) -> List[Tuple[Any, str, bool]]:
    doc_roles = {atspi_mod.Role.DOCUMENT_WEB, atspi_mod.Role.DOCUMENT_FRAME}
    skip_roles = {atspi_mod.Role.MENU, atspi_mod.Role.MENU_BAR, atspi_mod.Role.POPUP_MENU}
    queue: Deque[Tuple[Any, int]] = deque([(win, 0)])
    tabs: List[Tuple[Any, str, bool]] = []
    visited = 0
    while queue and visited < 1200:
        cur, depth = queue.popleft()
        if cur is None:
            continue
        visited += 1
        try:
            _clear_atspi_single_cache(cur)
            role = cur.get_role()
            if role in doc_roles or (depth > 0 and role in skip_roles):
                continue
            if role == atspi_mod.Role.PAGE_TAB:
                name = (cur.get_name() or "").strip()
                selected = cur.get_state_set().contains(atspi_mod.StateType.SELECTED)
                tabs.append((cur, name, selected))
                continue
            if depth >= 16:
                continue
            for i in range(cur.get_child_count()):
                child = cur.get_child_at_index(i)
                if child is not None:
                    queue.append((child, depth + 1))
        except Exception:
            continue
    return tabs


def _select_atspi_tab(tab_node: Any) -> bool:
    try:
        parent = tab_node.get_parent()
        if parent is not None:
            sel_iface = parent.get_selection_iface()
            if sel_iface is not None:
                idx = tab_node.get_index_in_parent()
                if idx >= 0 and sel_iface.select_child(idx):
                    return True
    except Exception:
        pass
    try:
        act = tab_node.get_action_iface()
        if act is not None and act.get_n_actions() > 0:
            return bool(act.do_action(0))
    except Exception:
        pass
    return False


def _wait_and_verify_selected_atspi_tab(
    win: Any,
    atspi_mod: Any,
    labels_lower: Sequence[str],
    allowed_title_keywords: Sequence[str],
    is_aistudio: bool,
    pre_title: str = "",
) -> Optional[str]:
    deadline = time.monotonic() + _TAB_VERIFY_TIMEOUT_SEC
    mismatch_since: Optional[float] = None
    while time.monotonic() < deadline:
        _clear_atspi_subtree_cache(win)
        try:
            post_title = (win.get_name() or "").strip()
        except Exception:
            post_title = ""
        if is_login_or_auth_title(post_title):
            return None
        if not is_loading_or_unready_title(post_title):
            title_ok = (not allowed_title_keywords) or any(
                kw in post_title.lower() for kw in allowed_title_keywords
            )
            if not title_ok:
                # 切替直後の旧タブ残像(pre_titleと同一)による誤打ち切りを防ぐ
                if post_title != pre_title and not _is_atspi_document_busy(win, atspi_mod):
                    if mismatch_since is None:
                        mismatch_since = time.monotonic()
                    elif (time.monotonic() - mismatch_since) >= 0.6:
                        return None
                else:
                    mismatch_since = None
            else:
                mismatch_since = None
                is_empty, definite_non_empty = _inspect_atspi_window_empty_chat(
                    win, atspi_mod, labels_lower, is_aistudio
                )
                if is_empty:
                    return post_title
                if definite_non_empty:
                    return None
        time.sleep(0.08)
    return None


def _activate_linux_window_by_title(win_title: str, hints: Sequence[str]) -> Optional[str]:
    try:
        res = subprocess.run(["wmctrl", "-lx"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            title_norm = (win_title or "").strip().lower()
            core_norm = _strip_browser_title_suffix(win_title or "")[0].lower()
            browser_windows: List[Tuple[str, str]] = []
            for line in res.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 5:
                    continue
                win_id, wm_class, raw_title = parts[0], parts[2].lower(), parts[4].strip()
                wm_tokens = {wm_class} | {tok for tok in wm_class.split(".") if tok}
                if any(h in wm_tokens for h in hints):
                    browser_windows.append((win_id, raw_title))

            chosen_id: Optional[str] = None
            if title_norm:
                for win_id, raw_title in browser_windows:
                    if raw_title.lower() == title_norm:
                        chosen_id = win_id
                        break
            if chosen_id is None and core_norm:
                for win_id, raw_title in browser_windows:
                    if _strip_browser_title_suffix(raw_title)[0].lower() == core_norm:
                        chosen_id = win_id
                        break
            if chosen_id is None and len(browser_windows) == 1:
                chosen_id = browser_windows[0][0]
            if chosen_id is not None:
                act = subprocess.run(["wmctrl", "-i", "-a", chosen_id], capture_output=True, timeout=2)
                if act.returncode == 0:
                    return chosen_id
    except (OSError, subprocess.SubprocessError):
        pass
    return focus_linux_default_browser_target()


def focus_linux_empty_chat_tab(
    target_url: str,
    prompt_labels: Sequence[str],
    allowed_title_keywords: Sequence[str],
    is_aistudio: bool,
) -> Optional[str]:
    """起動済みLinuxブラウザから対象サイトの空チャットタブを探し、選択・最前面化してウィンドウIDを返す。"""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return None
    hints = _get_linux_browser_wm_hints()
    if not hints:
        return None
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        return None

    desktop = Atspi.get_desktop(0)
    if desktop is None:
        return None
    _disable_atspi_cache(desktop, Atspi)
    hint_norms = _build_linux_hint_norms(hints)
    labels_lower = [lbl.strip().lower() for lbl in prompt_labels if lbl.strip()]

    for app_idx in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(app_idx)
        if app is None or not _is_atspi_app_default_browser(app, hint_norms):
            continue
        for win_idx in range(app.get_child_count()):
            win = app.get_child_at_index(win_idx)
            if win is None:
                continue
            try:
                _clear_atspi_single_cache(win)
                win_title = (win.get_name() or "").strip()
                title_lower = win_title.lower()
                active_matches = (
                    not is_login_or_auth_title(win_title)
                    and not is_loading_or_unready_title(win_title)
                    and ((not allowed_title_keywords) or any(kw in title_lower for kw in allowed_title_keywords))
                )
                if active_matches and _is_atspi_window_empty_chat(win, Atspi, labels_lower, is_aistudio):
                    return _activate_linux_window_by_title(win_title, hints) or "atspi-focused"

                tabs = _collect_atspi_page_tabs(win, Atspi)
                prev_selected = next((tab for tab, _, is_sel in tabs if is_sel), None)
                candidates = [
                    tab for tab, name, is_sel in tabs if (not is_sel) and is_empty_chat_tab_title(name, target_url)
                ]
                if not candidates:
                    continue

                for tab in candidates:
                    if not _select_atspi_tab(tab):
                        continue
                    matched_title = _wait_and_verify_selected_atspi_tab(
                        win, Atspi, labels_lower, allowed_title_keywords, is_aistudio, pre_title=win_title
                    )
                    if matched_title is not None:
                        return _activate_linux_window_by_title(matched_title, hints) or "atspi-focused"

                if prev_selected is not None:
                    _select_atspi_tab(prev_selected)
            except Exception:
                continue
    return None
