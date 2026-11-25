"""Module: @role: Windowsブラウザのページ読み込み状態判定および可視Document要素探索を担当する。"""
import ctypes
from collections import deque
from typing import Any, Deque, List, Optional, Sequence, Tuple

from .browser_paste_windows_uia_com import (
    _TreeScope_Children,
    _UIA_ButtonControlTypeId,
    _UIA_ComboBoxControlTypeId,
    _UIA_ControlTypePropertyId,
    _UIA_CustomControlTypeId,
    _UIA_DocumentControlTypeId,
    _UIA_EditControlTypeId,
    _UIA_GroupControlTypeId,
    _UIA_IsEnabledPropertyId,
    _UIA_IsOffscreenPropertyId,
    _UIA_MenuBarControlTypeId,
    _UIA_MenuControlTypeId,
    _UIA_NamePropertyId,
    _UIA_PaneControlTypeId,
    _UIA_TabControlTypeId,
    _UIA_TabItemControlTypeId,
    _UIA_ToolBarControlTypeId,
    _com_release,
    _uia_create_int_condition,
    _uia_create_true_condition,
    _uia_find_all,
    _uia_find_scoped_all,
    _uia_get_bounding_rect,
    _uia_get_prop_bool,
    _uia_get_prop_int,
    _uia_get_prop_str,
)

_STOP_LOADING_EXACT_NAMES = frozenset({"stop", "停止"})
_STOP_LOADING_SUBSTRINGS = (
    "このページの読み込みを中止",
    "現在のページの読み込みを中止",
    "読み込みを中止",
    "読み込みを停止",
    "stop loading",
    "cancel loading",
)
_NAV_TOOLBAR_HINT_SUBSTRINGS = (
    "back",
    "forward",
    "reload",
    "refresh",
    "戻る",
    "進む",
    "更新",
    "再読み込み",
)
_NAV_CONTAINER_CONTROL_TYPES = frozenset(
    {
        _UIA_ToolBarControlTypeId,
        _UIA_GroupControlTypeId,
        _UIA_PaneControlTypeId,
        _UIA_CustomControlTypeId,
    }
)
_SKIP_LOADING_SCAN_CONTROL_TYPES = frozenset(
    {
        _UIA_DocumentControlTypeId,
        _UIA_MenuControlTypeId,
        _UIA_MenuBarControlTypeId,
        _UIA_TabControlTypeId,
        _UIA_TabItemControlTypeId,
    }
)

def _is_navigation_container(container_el: ctypes.c_void_p, oleaut32: Any, true_cond: ctypes.c_void_p) -> bool:
    """メディア停止ボタン等への誤爆を防ぐためアドレスバーや戻る/更新ボタンを近傍に含むナビ領域か判定する。"""
    owned: List[ctypes.c_void_p] = []
    queue: Deque[Tuple[ctypes.c_void_p, int]] = deque([(container_el, 0)])
    visited = 0
    try:
        while queue and visited < 60:
            cur_el, depth = queue.popleft()
            visited += 1
            if depth > 0:
                ctrl_type = _uia_get_prop_int(cur_el, oleaut32, _UIA_ControlTypePropertyId, default=0)
                if ctrl_type == _UIA_DocumentControlTypeId:
                    continue
                if ctrl_type in (_UIA_EditControlTypeId, _UIA_ComboBoxControlTypeId):
                    if _uia_get_prop_bool(cur_el, oleaut32, _UIA_IsEnabledPropertyId, default=False):
                        return True
                if ctrl_type == _UIA_ButtonControlTypeId:
                    name_lower = _uia_get_prop_str(cur_el, oleaut32, _UIA_NamePropertyId).strip().lower()
                    if any(hint in name_lower for hint in _NAV_TOOLBAR_HINT_SUBSTRINGS):
                        return True
                    continue
            if depth >= 3:
                continue
            children = _uia_find_scoped_all(cur_el, _TreeScope_Children, true_cond)
            for child in children:
                owned.append(child)
                queue.append((child, depth + 1))
        return False
    finally:
        for obj in owned:
            _com_release(obj)

def _is_stop_button_in_navigation_ui(
    ancestors: Sequence[ctypes.c_void_p], oleaut32: Any, true_cond: ctypes.c_void_p
) -> bool:
    for anc_el in reversed(ancestors):
        ctrl_type = _uia_get_prop_int(anc_el, oleaut32, _UIA_ControlTypePropertyId, default=0)
        if ctrl_type in _NAV_CONTAINER_CONTROL_TYPES and _is_navigation_container(anc_el, oleaut32, true_cond):
            return True
    return False

def is_browser_page_loading(p_uia: ctypes.c_void_p, root_el: ctypes.c_void_p, oleaut32: Any) -> bool:
    """ブラウザUI上の読み込み中止ボタン有無をタブ群・Document領域を除外して判定する。"""
    true_cond = _uia_create_true_condition(p_uia)
    if not true_cond:
        return False
    owned: List[ctypes.c_void_p] = []
    queue: Deque[Tuple[ctypes.c_void_p, int, Tuple[ctypes.c_void_p, ...]]] = deque([(root_el, 0, ())])
    visited = 0
    try:
        while queue and visited < 1000:
            cur_el, depth, recent_ancestors = queue.popleft()
            visited += 1
            if depth > 0:
                ctrl_type = _uia_get_prop_int(cur_el, oleaut32, _UIA_ControlTypePropertyId, default=0)
                if ctrl_type in _SKIP_LOADING_SCAN_CONTROL_TYPES:
                    continue
                if ctrl_type == _UIA_ButtonControlTypeId:
                    if (
                        _uia_get_prop_bool(cur_el, oleaut32, _UIA_IsEnabledPropertyId, default=False)
                        and not _uia_get_prop_bool(cur_el, oleaut32, _UIA_IsOffscreenPropertyId, default=False)
                    ):
                        name_lower = _uia_get_prop_str(cur_el, oleaut32, _UIA_NamePropertyId).strip().lower()
                        if any(sub in name_lower for sub in _STOP_LOADING_SUBSTRINGS):
                            return True
                        # 短い"stop"/"停止"名はToolBar/Group/Pane構成のナビゲーション近傍のみに限定する
                        if name_lower in _STOP_LOADING_EXACT_NAMES and _is_stop_button_in_navigation_ui(
                            recent_ancestors, oleaut32, true_cond
                        ):
                            return True
                    continue
            if depth >= 18:
                continue
            next_ancestors = (recent_ancestors + (cur_el,))[-2:] if depth > 0 else ()
            children = _uia_find_scoped_all(cur_el, _TreeScope_Children, true_cond)
            for child in children:
                owned.append(child)
                queue.append((child, depth + 1, next_ancestors))
        return False
    finally:
        for obj in owned:
            _com_release(obj)
        _com_release(true_cond)

def _find_visible_active_documents(
    p_uia: ctypes.c_void_p, root_el: ctypes.c_void_p, oleaut32: Any
) -> List[ctypes.c_void_p]:
    doc_cond = _uia_create_int_condition(p_uia, _UIA_ControlTypePropertyId, _UIA_DocumentControlTypeId)
    if not doc_cond:
        return []
    try:
        docs = _uia_find_all(root_el, doc_cond)
        visible_with_area: List[Tuple[float, ctypes.c_void_p]] = []
        for doc in docs:
            if _uia_get_prop_bool(doc, oleaut32, _UIA_IsOffscreenPropertyId, default=False):
                _com_release(doc)
                continue
            rect = _uia_get_bounding_rect(doc, oleaut32)
            if not rect or rect[2] <= 0 or rect[3] <= 0:
                _com_release(doc)
                continue
            visible_with_area.append((rect[2] * rect[3], doc))
        visible_with_area.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in visible_with_area]
    finally:
        _com_release(doc_cond)

def _find_visible_active_document(
    p_uia: ctypes.c_void_p, root_el: ctypes.c_void_p, oleaut32: Any
) -> Optional[ctypes.c_void_p]:
    docs = _find_visible_active_documents(p_uia, root_el, oleaut32)
    if not docs:
        return None
    chosen = docs[0]
    for extra in docs[1:]:
        _com_release(extra)
    return chosen
