"""Module: @role: Windows UI Automationを用いたプロンプト入力欄探索・状態検証および関連ヘルパーの公開ファサードを提供する。"""
import ctypes
import time
from typing import Any, List, Optional, Sequence, Tuple

from .browser_paste_windows_uia_com import (
    _CLSID_CUIAutomation,
    _CLSCTX_INPROC_SERVER,
    _COINIT_APARTMENTTHREADED,
    _GUID,
    _IID_IUIAutomation,
    _IS_64BIT,
    _RPC_E_CHANGED_MODE,
    _TreeScope_Children,
    _TreeScope_Descendants,
    _UIA_AriaPropertiesPropertyId,
    _UIA_AriaRolePropertyId,
    _UIA_BoundingRectanglePropertyId,
    _UIA_ButtonControlTypeId,
    _UIA_ComboBoxControlTypeId,
    _UIA_ControlTypePropertyId,
    _UIA_CustomControlTypeId,
    _UIA_DocumentControlTypeId,
    _UIA_EditControlTypeId,
    _UIA_FullDescriptionPropertyId,
    _UIA_GroupControlTypeId,
    _UIA_HasKeyboardFocusPropertyId,
    _UIA_HelpTextPropertyId,
    _UIA_IsEnabledPropertyId,
    _UIA_IsKeyboardFocusablePropertyId,
    _UIA_IsOffscreenPropertyId,
    _UIA_IsPasswordPropertyId,
    _UIA_MenuBarControlTypeId,
    _UIA_MenuControlTypeId,
    _UIA_NamePropertyId,
    _UIA_PaneControlTypeId,
    _UIA_TabControlTypeId,
    _UIA_TabItemControlTypeId,
    _UIA_TextControlTypeId,
    _UIA_ToolBarControlTypeId,
    _UIA_ValueIsReadOnlyPropertyId,
    _UIA_ValueValuePropertyId,
    _VARIANT,
    _VT_ARRAY,
    _VT_BOOL,
    _VT_BSTR,
    _VT_I4,
    _VT_R8,
    _com_call,
    _com_release,
    _is_uia_descendant_of,
    _uia_compare_elements,
    _uia_create_bstr_condition,
    _uia_create_int_condition,
    _uia_create_or_condition,
    _uia_create_true_condition,
    _uia_find_all,
    _uia_find_first,
    _uia_find_scoped_all,
    _uia_get_bounding_rect,
    _uia_get_prop_bool,
    _uia_get_prop_int,
    _uia_get_prop_str,
)
from .browser_paste_windows_uia_page import (
    _NAV_CONTAINER_CONTROL_TYPES,
    _NAV_TOOLBAR_HINT_SUBSTRINGS,
    _SKIP_LOADING_SCAN_CONTROL_TYPES,
    _STOP_LOADING_EXACT_NAMES,
    _STOP_LOADING_SUBSTRINGS,
    _find_visible_active_document,
    _find_visible_active_documents,
    _is_navigation_container,
    _is_stop_button_in_navigation_ui,
    is_browser_page_loading,
)

_ALLOWED_INNER_FOCUS_CONTROL_TYPES = frozenset(
    {
        _UIA_EditControlTypeId,
        _UIA_TextControlTypeId,
        _UIA_GroupControlTypeId,
        _UIA_CustomControlTypeId,
        _UIA_PaneControlTypeId,
        _UIA_DocumentControlTypeId,
    }
)
_NON_INPUT_ARIA_ROLES = frozenset(
    {
        "button",
        "link",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "checkbox",
        "radio",
        "switch",
        "tab",
        "option",
        "treeitem",
        "slider",
        "spinbutton",
    }
)


def _collect_element_match_texts(
    el: ctypes.c_void_p, oleaut32: Any, text_cond: Optional[ctypes.c_void_p]
) -> List[str]:
    texts: List[str] = []
    for prop_id in (_UIA_NamePropertyId, _UIA_HelpTextPropertyId, _UIA_FullDescriptionPropertyId, _UIA_AriaPropertiesPropertyId):
        val = _uia_get_prop_str(el, oleaut32, prop_id).strip()
        if val:
            texts.append(val)

    # ProseMirror/TipTap等のcontenteditable要素でプレースホルダーが子Textノードにある場合も拾う
    if not texts and text_cond is not None:
        child_texts = _uia_find_all(el, text_cond)
        try:
            for c_el in child_texts[:4]:
                c_name = _uia_get_prop_str(c_el, oleaut32, _UIA_NamePropertyId).strip()
                if c_name:
                    texts.append(c_name)
        finally:
            for c_el in child_texts:
                _com_release(c_el)
    return texts


def _score_candidate(texts: Sequence[str], labels_lower: Sequence[str]) -> Tuple[int, str]:
    best_score = 0
    matched_label = ""
    for raw_text in texts:
        norm = raw_text.strip().lower()
        if not norm:
            continue
        for lbl in labels_lower:
            if norm == lbl:
                return 100, lbl
            if lbl in norm or (len(norm) >= 4 and norm in lbl):
                if 60 > best_score:
                    best_score = 60
                    matched_label = lbl
    return best_score, matched_label


def is_prompt_element_usable(el: ctypes.c_void_p, oleaut32: Any) -> bool:
    """対象プロンプト要素が可視・有効・編集可能・有効矩形を持つか判定する。"""
    if not _uia_get_prop_bool(el, oleaut32, _UIA_IsEnabledPropertyId, default=False):
        return False
    if _uia_get_prop_bool(el, oleaut32, _UIA_IsOffscreenPropertyId, default=False):
        return False
    if not _uia_get_prop_bool(el, oleaut32, _UIA_IsKeyboardFocusablePropertyId, default=False):
        return False
    if _uia_get_prop_bool(el, oleaut32, _UIA_IsPasswordPropertyId, default=False):
        return False
    if _uia_get_prop_bool(el, oleaut32, _UIA_ValueIsReadOnlyPropertyId, default=False):
        return False
    rect = _uia_get_bounding_rect(el, oleaut32)
    return bool(rect and rect[2] > 0 and rect[3] > 0)


def is_prompt_element_value_empty(
    el: ctypes.c_void_p,
    oleaut32: Any,
    labels: Sequence[str],
    matched_lbl: str = "",
) -> bool:
    """プロンプト入力欄の現在値が空または要素自身のプレースホルダー文字列と一致するか判定する。"""
    existing_val = _uia_get_prop_str(el, oleaut32, _UIA_ValueValuePropertyId).strip()
    if not existing_val:
        return True
    existing_lower = existing_val.lower()
    labels_lower = {lbl.strip().lower() for lbl in labels if lbl and lbl.strip()}
    if matched_lbl:
        labels_lower.add(matched_lbl.strip().lower())
    if existing_lower in labels_lower:
        return True
    # プレースホルダー専用属性はラベル語彙非依存で完全一致を許容し、Name属性はラベル語彙を含む場合のみ許容する
    for prop_id in (_UIA_HelpTextPropertyId, _UIA_FullDescriptionPropertyId):
        attr_low = _uia_get_prop_str(el, oleaut32, prop_id).strip().lower()
        if attr_low and attr_low == existing_lower:
            return True
    name_low = _uia_get_prop_str(el, oleaut32, _UIA_NamePropertyId).strip().lower()
    if name_low and name_low == existing_lower and any(lbl in name_low for lbl in labels_lower):
        return True
    return False

def verify_element_focused(p_uia: ctypes.c_void_p, target_el: ctypes.c_void_p, oleaut32: Any) -> bool:
    """対象要素自身または真の入力子孫ノードがキーボードフォーカスを獲得したか検証する。"""
    if _uia_get_prop_bool(target_el, oleaut32, _UIA_HasKeyboardFocusPropertyId, default=False):
        return True
    focused_el = ctypes.c_void_p()
    hr = _com_call(p_uia, 8, ctypes.c_long, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(focused_el))
    if hr < 0 or not focused_el.value:
        return False
    try:
        if _uia_compare_elements(p_uia, target_el, focused_el):
            return True

        ctrl_type = _uia_get_prop_int(focused_el, oleaut32, _UIA_ControlTypePropertyId, default=0)
        if ctrl_type not in _ALLOWED_INNER_FOCUS_CONTROL_TYPES:
            return False
        aria_role = _uia_get_prop_str(focused_el, oleaut32, _UIA_AriaRolePropertyId).strip().lower()
        if aria_role in _NON_INPUT_ARIA_ROLES:
            return False
        if _uia_get_prop_bool(focused_el, oleaut32, _UIA_ValueIsReadOnlyPropertyId, default=False):
            return False

        is_descendant = _is_uia_descendant_of(p_uia, target_el, focused_el)
        if is_descendant is not None:
            return is_descendant

        target_rect = _uia_get_bounding_rect(target_el, oleaut32)
        focused_rect = _uia_get_bounding_rect(focused_el, oleaut32)
        if target_rect and focused_rect and focused_rect[2] > 0 and focused_rect[3] > 0:
            return (
                focused_rect[0] >= (target_rect[0] - 2.0)
                and focused_rect[1] >= (target_rect[1] - 2.0)
                and (focused_rect[0] + focused_rect[2]) <= (target_rect[0] + target_rect[2] + 2.0)
                and (focused_rect[1] + focused_rect[3]) <= (target_rect[1] + target_rect[3] + 2.0)
            )
        return False
    finally:
        _com_release(focused_el)

def find_best_prompt_element(
    p_uia: ctypes.c_void_p,
    root_el: ctypes.c_void_p,
    oleaut32: Any,
    labels: Sequence[str],
    quick_probe: bool = False,
) -> Tuple[Optional[ctypes.c_void_p], str, str]:
    """可視Document配下から最適なプロンプト入力要素を探索して返す。"""
    labels_lower = [lbl.strip().lower() for lbl in labels if lbl.strip()]
    edit_cond = _uia_create_int_condition(p_uia, _UIA_ControlTypePropertyId, _UIA_EditControlTypeId)
    textbox_cond = _uia_create_bstr_condition(p_uia, oleaut32, _UIA_AriaRolePropertyId, "textbox")
    text_cond = _uia_create_int_condition(p_uia, _UIA_ControlTypePropertyId, _UIA_TextControlTypeId)
    true_cond = _uia_create_true_condition(p_uia)

    input_cond: Optional[ctypes.c_void_p] = None
    if edit_cond and textbox_cond:
        input_cond = _uia_create_or_condition(p_uia, edit_cond, textbox_cond)
    elif edit_cond:
        input_cond = edit_cond
        edit_cond = None

    if not input_cond:
        for c in (edit_cond, textbox_cond, text_cond, true_cond):
            _com_release(c)
        return None, "", "UI Automation検索条件の生成に失敗しました"

    try:
        max_attempts = 4 if quick_probe else 12
        sleep_sec = 0.06 if quick_probe else 0.08
        early_dom_attempt = 2 if quick_probe else 3
        for attempt in range(max_attempts):
            # リダイレクト等によるDocument差し替えに追従するため毎回可視Documentを再取得する
            docs = _find_visible_active_documents(p_uia, root_el, oleaut32)
            try:
                if not docs:
                    time.sleep(sleep_sec)
                    continue

                best_el: Optional[ctypes.c_void_p] = None
                best_score = 0
                best_label = ""
                any_dom = False

                for doc_el in docs:
                    candidates = _uia_find_all(doc_el, input_cond)
                    for el in candidates:
                        try:
                            if not is_prompt_element_usable(el, oleaut32):
                                continue
                            texts = _collect_element_match_texts(el, oleaut32, text_cond)
                            score, matched_lbl = _score_candidate(texts, labels_lower)
                            if score > best_score:
                                _com_release(best_el)
                                best_el = el
                                best_score = score
                                best_label = matched_lbl
                                el = None
                        finally:
                            _com_release(el)
                    if best_el is not None and best_score >= 100:
                        break
                    if not any_dom and attempt >= early_dom_attempt and true_cond is not None:
                        any_child = _uia_find_first(doc_el, true_cond)
                        any_dom = any_child is not None
                        _com_release(any_child)

                if best_el is not None:
                    return best_el, best_label, ""

                # アクティブDocumentのDOMが展開済みかつ数回試して見つからない場合は早期終了する
                if attempt >= early_dom_attempt and any_dom:
                    return None, "", "ページ内に対象のプロンプト入力欄が見つかりませんでした"
            finally:
                for doc_el in docs:
                    _com_release(doc_el)

            time.sleep(sleep_sec)

        return None, "", "プロンプト入力欄の検出がタイムアウトしました"
    finally:
        for c in (input_cond, edit_cond, textbox_cond, text_cond, true_cond):
            if c is not None and c is not input_cond:
                _com_release(c)
        if input_cond is not None:
            _com_release(input_cond)
