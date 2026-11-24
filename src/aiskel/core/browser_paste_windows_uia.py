"""Module: @role: Windows UI Automation COM呼び出し・要素探索・ページ読み込み状態判定ヘルパーを提供する。"""
import ctypes
import time
from collections import deque
from typing import Any, Deque, List, Optional, Sequence, Tuple

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_RPC_E_CHANGED_MODE = -2147417850

_TreeScope_Children = 2
_TreeScope_Descendants = 4

_UIA_BoundingRectanglePropertyId = 30001
_UIA_ControlTypePropertyId = 30003
_UIA_NamePropertyId = 30005
_UIA_HasKeyboardFocusPropertyId = 30008
_UIA_IsKeyboardFocusablePropertyId = 30009
_UIA_IsEnabledPropertyId = 30010
_UIA_HelpTextPropertyId = 30013
_UIA_IsPasswordPropertyId = 30019
_UIA_IsOffscreenPropertyId = 30022
_UIA_ValueValuePropertyId = 30045
_UIA_ValueIsReadOnlyPropertyId = 30046
_UIA_AriaRolePropertyId = 30101
_UIA_AriaPropertiesPropertyId = 30102
_UIA_FullDescriptionPropertyId = 30159

_UIA_ButtonControlTypeId = 50000
_UIA_ComboBoxControlTypeId = 50003
_UIA_EditControlTypeId = 50004
_UIA_MenuControlTypeId = 50009
_UIA_MenuBarControlTypeId = 50010
_UIA_TabControlTypeId = 50018
_UIA_TabItemControlTypeId = 50019
_UIA_TextControlTypeId = 50020
_UIA_ToolBarControlTypeId = 50021
_UIA_CustomControlTypeId = 50025
_UIA_GroupControlTypeId = 50026
_UIA_DocumentControlTypeId = 50030
_UIA_PaneControlTypeId = 50033

_VT_I4 = 3
_VT_R8 = 5
_VT_BSTR = 8
_VT_BOOL = 11
_VT_ARRAY = 0x2000

_IS_64BIT = ctypes.sizeof(ctypes.c_void_p) == 8

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


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# Unionを含めない固定構造体として定義し32/64bit ctypes引数渡し制限を回避する
class _VARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("wReserved1", ctypes.c_uint16),
        ("wReserved2", ctypes.c_uint16),
        ("wReserved3", ctypes.c_uint16),
        ("data1", ctypes.c_ssize_t),
        ("data2", ctypes.c_ssize_t),
    ]


_CLSID_CUIAutomation = _GUID(
    0xFF48DBA4, 0x60EF, 0x4201, (ctypes.c_ubyte * 8)(0xAA, 0x87, 0x54, 0x10, 0x3E, 0xEF, 0x59, 0x4E)
)
_IID_IUIAutomation = _GUID(
    0x30CBE57D, 0xD9D0, 0x452A, (ctypes.c_ubyte * 8)(0xAB, 0x13, 0x7A, 0xC5, 0xAC, 0x48, 0x25, 0xEE)
)


def _com_call(obj_ptr: ctypes.c_void_p, index: int, restype: Any, argtypes: Sequence[Any], *args: Any) -> Any:
    vtable = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn_type = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = fn_type(vtable[index])
    return fn(obj_ptr, *args)


def _com_release(obj_ptr: Optional[ctypes.c_void_p]) -> None:
    if obj_ptr and obj_ptr.value:
        _com_call(obj_ptr, 2, ctypes.c_uint32, ())


def _uia_create_int_condition(p_uia: ctypes.c_void_p, prop_id: int, value: int) -> Optional[ctypes.c_void_p]:
    var = _VARIANT(vt=_VT_I4, data1=value)
    cond = ctypes.c_void_p()
    if _IS_64BIT:
        hr = _com_call(
            p_uia, 23, ctypes.c_long,
            (ctypes.c_int, ctypes.POINTER(_VARIANT), ctypes.POINTER(ctypes.c_void_p)),
            prop_id, ctypes.byref(var), ctypes.byref(cond),
        )
    else:
        hr = _com_call(
            p_uia, 23, ctypes.c_long,
            (ctypes.c_int, _VARIANT, ctypes.POINTER(ctypes.c_void_p)),
            prop_id, var, ctypes.byref(cond),
        )
    return cond if hr >= 0 and cond.value else None


def _uia_create_bstr_condition(
    p_uia: ctypes.c_void_p, oleaut32: Any, prop_id: int, text: str
) -> Optional[ctypes.c_void_p]:
    bstr = oleaut32.SysAllocString(text)
    if not bstr:
        return None
    var = _VARIANT(vt=_VT_BSTR, data1=int(bstr))
    cond = ctypes.c_void_p()
    try:
        if _IS_64BIT:
            hr = _com_call(
                p_uia, 23, ctypes.c_long,
                (ctypes.c_int, ctypes.POINTER(_VARIANT), ctypes.POINTER(ctypes.c_void_p)),
                prop_id, ctypes.byref(var), ctypes.byref(cond),
            )
        else:
            hr = _com_call(
                p_uia, 23, ctypes.c_long,
                (ctypes.c_int, _VARIANT, ctypes.POINTER(ctypes.c_void_p)),
                prop_id, var, ctypes.byref(cond),
            )
        return cond if hr >= 0 and cond.value else None
    finally:
        oleaut32.VariantClear(ctypes.byref(var))


def _uia_create_or_condition(
    p_uia: ctypes.c_void_p, cond1: ctypes.c_void_p, cond2: ctypes.c_void_p
) -> Optional[ctypes.c_void_p]:
    cond = ctypes.c_void_p()
    hr = _com_call(
        p_uia, 28, ctypes.c_long,
        (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
        cond1, cond2, ctypes.byref(cond),
    )
    return cond if hr >= 0 and cond.value else None


def _uia_create_true_condition(p_uia: ctypes.c_void_p) -> Optional[ctypes.c_void_p]:
    cond = ctypes.c_void_p()
    hr = _com_call(p_uia, 21, ctypes.c_long, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(cond))
    return cond if hr >= 0 and cond.value else None


def _uia_find_scoped_all(el: ctypes.c_void_p, scope: int, cond: ctypes.c_void_p) -> List[ctypes.c_void_p]:
    arr = ctypes.c_void_p()
    hr = _com_call(
        el, 6, ctypes.c_long,
        (ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
        scope, cond, ctypes.byref(arr),
    )
    if hr < 0 or not arr.value:
        return []
    result: List[ctypes.c_void_p] = []
    try:
        length = ctypes.c_int(0)
        if _com_call(arr, 3, ctypes.c_long, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(length)) < 0:
            return []
        for idx in range(length.value):
            item = ctypes.c_void_p()
            if _com_call(
                arr, 4, ctypes.c_long, (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)), idx, ctypes.byref(item)
            ) >= 0 and item.value:
                result.append(item)
        return result
    finally:
        _com_release(arr)


def _uia_find_all(el: ctypes.c_void_p, cond: ctypes.c_void_p) -> List[ctypes.c_void_p]:
    return _uia_find_scoped_all(el, _TreeScope_Descendants, cond)


def _uia_find_first(el: ctypes.c_void_p, cond: ctypes.c_void_p) -> Optional[ctypes.c_void_p]:
    found = ctypes.c_void_p()
    hr = _com_call(
        el, 5, ctypes.c_long,
        (ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
        _TreeScope_Descendants, cond, ctypes.byref(found),
    )
    return found if hr >= 0 and found.value else None


def _uia_get_prop_int(el: ctypes.c_void_p, oleaut32: Any, prop_id: int, default: int = 0) -> int:
    var = _VARIANT()
    hr = _com_call(el, 10, ctypes.c_long, (ctypes.c_int, ctypes.POINTER(_VARIANT)), prop_id, ctypes.byref(var))
    try:
        if hr >= 0 and var.vt == _VT_I4:
            return ctypes.c_int32(var.data1 & 0xFFFFFFFF).value
        return default
    finally:
        oleaut32.VariantClear(ctypes.byref(var))


def _uia_get_prop_bool(el: ctypes.c_void_p, oleaut32: Any, prop_id: int, default: bool = False) -> bool:
    var = _VARIANT()
    hr = _com_call(el, 10, ctypes.c_long, (ctypes.c_int, ctypes.POINTER(_VARIANT)), prop_id, ctypes.byref(var))
    try:
        if hr >= 0 and var.vt == _VT_BOOL:
            return ctypes.c_int16(var.data1 & 0xFFFF).value != 0
        return default
    finally:
        oleaut32.VariantClear(ctypes.byref(var))


def _uia_get_prop_str(el: ctypes.c_void_p, oleaut32: Any, prop_id: int) -> str:
    var = _VARIANT()
    hr = _com_call(el, 10, ctypes.c_long, (ctypes.c_int, ctypes.POINTER(_VARIANT)), prop_id, ctypes.byref(var))
    try:
        if hr >= 0 and var.vt == _VT_BSTR and var.data1:
            return ctypes.wstring_at(var.data1)
        return ""
    finally:
        oleaut32.VariantClear(ctypes.byref(var))


def _uia_get_bounding_rect(el: ctypes.c_void_p, oleaut32: Any) -> Optional[Tuple[float, float, float, float]]:
    var = _VARIANT()
    hr = _com_call(
        el, 10, ctypes.c_long, (ctypes.c_int, ctypes.POINTER(_VARIANT)), _UIA_BoundingRectanglePropertyId, ctypes.byref(var)
    )
    try:
        if hr < 0 or var.vt != (_VT_ARRAY | _VT_R8) or not var.data1:
            return None
        psa = ctypes.c_void_p(var.data1)
        lb = ctypes.c_long(0)
        ub = ctypes.c_long(0)
        if oleaut32.SafeArrayGetLBound(psa, 1, ctypes.byref(lb)) < 0:
            return None
        if oleaut32.SafeArrayGetUBound(psa, 1, ctypes.byref(ub)) < 0:
            return None
        if (ub.value - lb.value + 1) < 4:
            return None
        pdata = ctypes.c_void_p()
        if oleaut32.SafeArrayAccessData(psa, ctypes.byref(pdata)) < 0 or not pdata.value:
            return None
        try:
            doubles = ctypes.cast(pdata, ctypes.POINTER(ctypes.c_double))
            return (doubles[0], doubles[1], doubles[2], doubles[3])
        finally:
            oleaut32.SafeArrayUnaccessData(psa)
    finally:
        oleaut32.VariantClear(ctypes.byref(var))


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


def _uia_compare_elements(p_uia: ctypes.c_void_p, el1: ctypes.c_void_p, el2: ctypes.c_void_p) -> bool:
    same = ctypes.c_int(0)
    hr = _com_call(
        p_uia, 3, ctypes.c_long,
        (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)),
        el1, el2, ctypes.byref(same),
    )
    return hr >= 0 and same.value != 0


def _is_uia_descendant_of(
    p_uia: ctypes.c_void_p, ancestor_el: ctypes.c_void_p, node_el: ctypes.c_void_p
) -> Optional[bool]:
    # IUIAutomation::get_RawViewWalker (vtable 16) と TreeWalker::GetParentElement (vtable 3) で真の親子関係を検証する
    walker = ctypes.c_void_p()
    hr_w = _com_call(p_uia, 16, ctypes.c_long, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(walker))
    if hr_w < 0 or not walker.value:
        return None
    cur: Optional[ctypes.c_void_p] = node_el
    owned: List[ctypes.c_void_p] = []
    try:
        for _ in range(48):
            parent = ctypes.c_void_p()
            hr_p = _com_call(
                walker, 3, ctypes.c_long,
                (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                cur, ctypes.byref(parent),
            )
            if hr_p < 0:
                return None
            if not parent.value:
                return False
            owned.append(parent)
            if _uia_compare_elements(p_uia, ancestor_el, parent):
                return True
            cur = parent
        return None
    finally:
        for obj in owned:
            _com_release(obj)
        _com_release(walker)


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
