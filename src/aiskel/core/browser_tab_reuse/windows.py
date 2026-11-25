"""Module: @role: Windows UI Automationを用いて起動済みブラウザ内の未会話（空チャット）タブを検出し、選択・最前面化を行う。"""
import ctypes
import time
from collections import deque
from typing import Any, Deque, List, Optional, Sequence, Tuple

from ..browser_focus.windows import (
    _find_target_browser_hwnds,
    _get_windows_default_browser_exe_names,
    _init_win32_apis,
    focus_windows_hwnd,
)
from ..browser_paste_types import is_loading_or_unready_title, is_login_or_auth_title
from ..browser_paste_windows import _get_window_title, _init_paste_win32_apis
from ..browser_paste_windows_uia import (
    _CLSID_CUIAutomation,
    _CLSCTX_INPROC_SERVER,
    _COINIT_APARTMENTTHREADED,
    _GUID,
    _IID_IUIAutomation,
    _RPC_E_CHANGED_MODE,
    _TreeScope_Children,
    _UIA_AriaPropertiesPropertyId,
    _UIA_ButtonControlTypeId,
    _UIA_ControlTypePropertyId,
    _UIA_DocumentControlTypeId,
    _UIA_FullDescriptionPropertyId,
    _UIA_HelpTextPropertyId,
    _UIA_IsEnabledPropertyId,
    _UIA_IsOffscreenPropertyId,
    _UIA_MenuBarControlTypeId,
    _UIA_MenuControlTypeId,
    _UIA_NamePropertyId,
    _UIA_TabItemControlTypeId,
    _UIA_ToolBarControlTypeId,
    _com_call,
    _com_release,
    _find_visible_active_documents,
    _is_uia_descendant_of,
    _uia_create_int_condition,
    _uia_create_true_condition,
    _uia_find_all,
    _uia_find_scoped_all,
    _uia_get_bounding_rect,
    _uia_get_prop_bool,
    _uia_get_prop_int,
    _uia_get_prop_str,
    find_best_prompt_element,
    is_browser_page_loading,
    is_prompt_element_value_empty,
)
from .types import (
    CHAT_TURN_ACTION_BUTTON_KEYWORDS,
    NEW_CHAT_BUTTON_NAMES,
    is_empty_chat_tab_title,
)

_UIA_InvokePatternId = 10000
_IID_IUIAutomationInvokePattern = _GUID(
    0xFB377FBE, 0x8EA6, 0x46D5, (ctypes.c_ubyte * 8)(0x9C, 0x73, 0x64, 0x99, 0x64, 0x2D, 0x30, 0x59)
)
# UIA_SelectionItemPatternId (UIAutomationClient.h)
_UIA_SelectionItemPatternId = 10010
_UIA_SelectionItemIsSelectedPropertyId = 30079
_IID_IUIAutomationSelectionItemPattern = _GUID(
    0xA8EFA66A, 0x0FDA, 0x421A, (ctypes.c_ubyte * 8)(0x91, 0x94, 0x38, 0x02, 0x1F, 0x35, 0x78, 0xEA)
)
_SW_MINIMIZE = 6
_SW_RESTORE = 9
_TAB_VERIFY_TIMEOUT_SEC = 2.0
_DISABLED_NEW_CHAT_TOOLTIPS = (
    "already in a new chat",
    "すでに新しいチャット",
    "既に新しいチャット",
)


def _is_button_disabled(btn_el: ctypes.c_void_p, oleaut32: Any) -> bool:
    if not _uia_get_prop_bool(btn_el, oleaut32, _UIA_IsEnabledPropertyId, default=True):
        return True
    aria_props = _uia_get_prop_str(btn_el, oleaut32, _UIA_AriaPropertiesPropertyId).lower().replace(" ", "")
    if "disabled=true" in aria_props:
        return True
    for prop_id in (_UIA_HelpTextPropertyId, _UIA_FullDescriptionPropertyId):
        desc = _uia_get_prop_str(btn_el, oleaut32, prop_id).lower()
        if desc and any(hint in desc for hint in _DISABLED_NEW_CHAT_TOOLTIPS):
            return True
    return False


def _inspect_document_empty_chat(
    p_uia: ctypes.c_void_p,
    root_el: ctypes.c_void_p,
    oleaut32: Any,
    prompt_labels: Sequence[str],
    is_aistudio: bool,
) -> Tuple[bool, bool]:
    """Documentを検査し、(空チャットか, 非空確定で即終了すべきか) を返す。"""
    target_el, matched_lbl, _ = find_best_prompt_element(
        p_uia, root_el, oleaut32, prompt_labels, quick_probe=True
    )
    if target_el is None:
        return False, False

    docs = _find_visible_active_documents(p_uia, root_el, oleaut32)
    btn_cond = _uia_create_int_condition(p_uia, _UIA_ControlTypePropertyId, _UIA_ButtonControlTypeId)
    try:
        if not is_prompt_element_value_empty(target_el, oleaut32, prompt_labels, matched_lbl):
            return False, True

        if not btn_cond or not docs:
            return False, False

        # DevToolsやサイドパネル等別Documentのボタン誤爆を防ぐため入力欄を含むDocumentのみ検査する
        target_doc: Optional[ctypes.c_void_p] = None
        if len(docs) == 1:
            target_doc = docs[0]
        else:
            for doc_el in docs:
                if _is_uia_descendant_of(p_uia, doc_el, target_el) is True:
                    target_doc = doc_el
                    break
            if target_doc is None:
                target_doc = docs[0]

        buttons = _uia_find_all(target_doc, btn_cond)
        try:
            for btn in buttons:
                if _uia_get_prop_bool(btn, oleaut32, _UIA_IsOffscreenPropertyId, default=False):
                    continue
                rect = _uia_get_bounding_rect(btn, oleaut32)
                if rect is not None and (rect[2] <= 0 or rect[3] <= 0):
                    continue
                name_lower = _uia_get_prop_str(btn, oleaut32, _UIA_NamePropertyId).strip().lower()
                if not name_lower:
                    continue
                if is_aistudio and name_lower in NEW_CHAT_BUTTON_NAMES:
                    if not _is_button_disabled(btn, oleaut32):
                        return False, True
                if name_lower in ("stop", "停止"):
                    return False, True
                if any(kw in name_lower for kw in CHAT_TURN_ACTION_BUTTON_KEYWORDS):
                    return False, True
        finally:
            for btn in buttons:
                _com_release(btn)
        return True, False
    finally:
        _com_release(target_el)
        for doc_el in docs:
            _com_release(doc_el)
        _com_release(btn_cond)


def _is_document_empty_chat(
    p_uia: ctypes.c_void_p,
    root_el: ctypes.c_void_p,
    oleaut32: Any,
    prompt_labels: Sequence[str],
    is_aistudio: bool,
) -> bool:
    """アクティブDocumentに会話履歴がなく、空のプロンプト入力欄が存在するか検証する。"""
    is_empty, _ = _inspect_document_empty_chat(p_uia, root_el, oleaut32, prompt_labels, is_aistudio)
    return is_empty


def _collect_browser_tab_items(
    p_uia: ctypes.c_void_p, root_el: ctypes.c_void_p, oleaut32: Any
) -> List[Tuple[ctypes.c_void_p, str, bool]]:
    """Document領域を除外してブラウザUI上のタブ要素 (要素, タイトル, 選択状態) 一覧を取得する。"""
    true_cond = _uia_create_true_condition(p_uia)
    if not true_cond:
        return []
    skip_types = {_UIA_DocumentControlTypeId, _UIA_MenuControlTypeId, _UIA_MenuBarControlTypeId}
    # 多数のブックマークボタン等でノード上限を消費しないようToolBar配下は後回しにする
    main_queue: Deque[Tuple[ctypes.c_void_p, int]] = deque([(root_el, 0)])
    toolbar_queue: Deque[Tuple[ctypes.c_void_p, int]] = deque()
    owned: List[ctypes.c_void_p] = []
    tabs: List[Tuple[ctypes.c_void_p, str, bool]] = []
    tab_ptrs: set[int] = set()
    visited = 0
    try:
        while (main_queue or toolbar_queue) and visited < 3000:
            cur_el, depth = main_queue.popleft() if main_queue else toolbar_queue.popleft()
            visited += 1
            ctrl_type = 0
            if depth > 0:
                ctrl_type = _uia_get_prop_int(cur_el, oleaut32, _UIA_ControlTypePropertyId, default=0)
                if ctrl_type in skip_types:
                    continue
                if ctrl_type == _UIA_TabItemControlTypeId:
                    name = _uia_get_prop_str(cur_el, oleaut32, _UIA_NamePropertyId).strip()
                    is_selected = _uia_get_prop_bool(
                        cur_el, oleaut32, _UIA_SelectionItemIsSelectedPropertyId, default=False
                    )
                    if cur_el.value:
                        tab_ptrs.add(cur_el.value)
                    tabs.append((cur_el, name, is_selected))
                    continue
            if depth >= 18:
                continue
            target_q = toolbar_queue if ctrl_type == _UIA_ToolBarControlTypeId else main_queue
            children = _uia_find_scoped_all(cur_el, _TreeScope_Children, true_cond)
            for child in children:
                owned.append(child)
                target_q.append((child, depth + 1))
        return tabs
    finally:
        for obj in owned:
            if not obj.value or obj.value not in tab_ptrs:
                _com_release(obj)
        _com_release(true_cond)


def _select_uia_tab_item_background(tab_el: ctypes.c_void_p) -> bool:
    """フォーカス奪取や座標誤爆を防ぐためSelectionItemPattern(未実装時のみInvokePattern)でタブを切り替える。"""
    pat = ctypes.c_void_p()
    hr = _com_call(
        tab_el,
        14,
        ctypes.c_long,
        (ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)),
        _UIA_SelectionItemPatternId,
        ctypes.byref(_IID_IUIAutomationSelectionItemPattern),
        ctypes.byref(pat),
    )
    if hr >= 0 and pat.value:
        try:
            # 反映遅延中の二重実行を防ぐためSelect()成功時はInvokeへフォールバックしない
            return _com_call(pat, 3, ctypes.c_long, ()) >= 0
        finally:
            _com_release(pat)

    inv_pat = ctypes.c_void_p()
    hr_inv = _com_call(
        tab_el,
        14,
        ctypes.c_long,
        (ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)),
        _UIA_InvokePatternId,
        ctypes.byref(_IID_IUIAutomationInvokePattern),
        ctypes.byref(inv_pat),
    )
    if hr_inv >= 0 and inv_pat.value:
        try:
            return _com_call(inv_pat, 3, ctypes.c_long, ()) >= 0
        finally:
            _com_release(inv_pat)
    return False


def _wait_and_verify_selected_tab(
    hwnd: int,
    user32: Any,
    p_uia: ctypes.c_void_p,
    root_el: ctypes.c_void_p,
    oleaut32: Any,
    prompt_labels: Sequence[str],
    allowed_title_keywords: Sequence[str],
    is_aistudio: bool,
    pre_title: str = "",
) -> bool:
    deadline = time.monotonic() + _TAB_VERIFY_TIMEOUT_SEC
    mismatch_since: Optional[float] = None
    while time.monotonic() < deadline:
        title = _get_window_title(hwnd, user32)
        if is_login_or_auth_title(title):
            return False
        if not is_loading_or_unready_title(title):
            title_ok = (not allowed_title_keywords) or any(kw in title.lower() for kw in allowed_title_keywords)
            if not title_ok:
                # 切替直後の旧タブ残像(pre_titleと同一)による誤打ち切りを防ぐ
                if title != pre_title and not is_browser_page_loading(p_uia, root_el, oleaut32):
                    if mismatch_since is None:
                        mismatch_since = time.monotonic()
                    elif (time.monotonic() - mismatch_since) >= 0.6:
                        return False
                else:
                    mismatch_since = None
            else:
                mismatch_since = None
                if not is_browser_page_loading(p_uia, root_el, oleaut32):
                    is_empty, definite_non_empty = _inspect_document_empty_chat(
                        p_uia, root_el, oleaut32, prompt_labels, is_aistudio
                    )
                    if is_empty:
                        return True
                    if definite_non_empty:
                        return False
        time.sleep(0.08)
    return False


def _restore_minimized_state_if_failed(
    hwnd: int,
    prev_fg_hwnd: int,
    user32_focus: Any,
) -> None:
    user32_focus.ShowWindow(hwnd, _SW_MINIMIZE)
    if prev_fg_hwnd and prev_fg_hwnd != hwnd:
        focus_windows_hwnd(prev_fg_hwnd)


def focus_windows_empty_chat_tab(
    target_url: str,
    prompt_labels: Sequence[str],
    allowed_title_keywords: Sequence[str],
    is_aistudio: bool,
) -> Optional[int]:
    """起動済みWindowsブラウザから対象サイトの空チャットタブを探し、選択・最前面化してHWNDを返す。"""
    exe_names = _get_windows_default_browser_exe_names()
    if not exe_names:
        return None

    user32_focus, kernel32_focus, dwmapi, get_window_long, wnd_enum_proc = _init_win32_apis()
    visible_hwnds, minimized_hwnds = _find_target_browser_hwnds(
        exe_names, user32_focus, kernel32_focus, dwmapi, get_window_long, wnd_enum_proc
    )
    if not visible_hwnds and not minimized_hwnds:
        return None

    user32, _, ole32, oleaut32 = _init_paste_win32_apis()
    hr_co = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    should_uninit = hr_co in (0, 1)
    if hr_co < 0 and hr_co != _RPC_E_CHANGED_MODE:
        return None

    p_uia = ctypes.c_void_p()
    try:
        hr_create = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_CUIAutomation),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_IUIAutomation),
            ctypes.byref(p_uia),
        )
        if hr_create < 0 or not p_uia.value:
            return None

        for is_minimized, hwnd_list in ((False, visible_hwnds), (True, minimized_hwnds)):
            for hwnd in hwnd_list:
                root_el = ctypes.c_void_p()
                hr_el = _com_call(
                    p_uia, 6, ctypes.c_long, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                    ctypes.c_void_p(hwnd), ctypes.byref(root_el),
                )
                if hr_el < 0 or not root_el.value:
                    continue
                tabs: List[Tuple[ctypes.c_void_p, str, bool]] = []
                try:
                    title = _get_window_title(hwnd, user32)
                    title_lower = title.lower()
                    active_title_matches = (
                        not is_login_or_auth_title(title)
                        and not is_loading_or_unready_title(title)
                        and ((not allowed_title_keywords) or any(kw in title_lower for kw in allowed_title_keywords))
                    )
                    # 検証成功前に前面化しないため、可視ウィンドウではバックグラウンドのまま空チャットを確認する
                    if not is_minimized and active_title_matches:
                        if not is_browser_page_loading(p_uia, root_el, oleaut32):
                            if _is_document_empty_chat(p_uia, root_el, oleaut32, prompt_labels, is_aistudio):
                                focused = focus_windows_hwnd(hwnd)
                                if focused:
                                    return focused

                    tabs = _collect_browser_tab_items(p_uia, root_el, oleaut32)
                    candidate_tabs = [
                        (tab_el, tab_name)
                        for tab_el, tab_name, is_sel in tabs
                        if (is_minimized or not is_sel) and is_empty_chat_tab_title(tab_name, target_url)
                    ]

                    if is_minimized:
                        active_matches_empty = is_empty_chat_tab_title(title, target_url)
                        if not candidate_tabs and not active_matches_empty:
                            continue
                        fg_ptr = user32_focus.GetForegroundWindow()
                        prev_fg_hwnd = int(fg_ptr) if fg_ptr else 0
                        prev_selected = next((tab_el for tab_el, _, is_sel in tabs if is_sel), None)
                        user32_focus.ShowWindow(hwnd, _SW_RESTORE)
                        if active_matches_empty and _wait_and_verify_selected_tab(
                            hwnd, user32, p_uia, root_el, oleaut32, prompt_labels, allowed_title_keywords, is_aistudio
                        ):
                            focused = focus_windows_hwnd(hwnd)
                            if focused:
                                return focused
                        tab_switched = False
                        for tab_el, _ in candidate_tabs:
                            if not _select_uia_tab_item_background(tab_el):
                                continue
                            tab_switched = True
                            if _wait_and_verify_selected_tab(
                                hwnd,
                                user32,
                                p_uia,
                                root_el,
                                oleaut32,
                                prompt_labels,
                                allowed_title_keywords,
                                is_aistudio,
                                pre_title=title,
                            ):
                                focused = focus_windows_hwnd(hwnd)
                                if focused:
                                    return focused
                                break
                        if tab_switched and prev_selected is not None:
                            _select_uia_tab_item_background(prev_selected)
                        _restore_minimized_state_if_failed(hwnd, prev_fg_hwnd, user32_focus)
                        continue

                    if not candidate_tabs:
                        continue

                    prev_selected = next((tab_el for tab_el, _, is_sel in tabs if is_sel), None)
                    for tab_el, _ in candidate_tabs:
                        if not _select_uia_tab_item_background(tab_el):
                            continue
                        if _wait_and_verify_selected_tab(
                            hwnd,
                            user32,
                            p_uia,
                            root_el,
                            oleaut32,
                            prompt_labels,
                            allowed_title_keywords,
                            is_aistudio,
                            pre_title=title,
                        ):
                            if focus_windows_hwnd(hwnd):
                                return hwnd
                            break

                    if prev_selected is not None:
                        _select_uia_tab_item_background(prev_selected)
                finally:
                    for tab_el, _, _ in tabs:
                        _com_release(tab_el)
                    _com_release(root_el)
        return None
    except OSError:
        return None
    finally:
        _com_release(p_uia)
        if should_uninit:
            ole32.CoUninitialize()
