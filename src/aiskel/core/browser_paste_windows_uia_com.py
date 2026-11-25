"""Module: @role: Windows UI AutomationのCOM構造体・定数・条件生成・要素走査・プロパティ取得プリミティブを提供する。"""
import ctypes
from typing import Any, List, Optional, Sequence, Tuple

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
