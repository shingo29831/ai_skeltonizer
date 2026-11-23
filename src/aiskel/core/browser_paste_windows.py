"""Module: @role: Windows UI Automation COM APIを直接呼び出してプロンプト入力欄へフォーカスし、修飾キー干渉を排除したWin32 SendInputで貼り付けを実行する。"""
import ctypes
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from .browser_focus_windows import _get_windows_default_browser_exe_names
from .browser_paste_types import PasteErrorKind, PasteOutcome, is_login_or_auth_title

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_RPC_E_CHANGED_MODE = -2147417850

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

_UIA_EditControlTypeId = 50004
_UIA_TextControlTypeId = 50020
_UIA_DocumentControlTypeId = 50030

_VT_I4 = 3
_VT_R8 = 5
_VT_BSTR = 8
_VT_BOOL = 11
_VT_ARRAY = 0x2000

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_KEYEVENTF_KEYUP = 0x0002

_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_V = 0x56
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_VK_LSHIFT = 0xA0
_VK_RSHIFT = 0xA1
_VK_LCONTROL = 0xA2
_VK_RCONTROL = 0xA3
_VK_LMENU = 0xA4
_VK_RMENU = 0xA5

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)

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


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("union", _INPUT_UNION),
    ]


_CLSID_CUIAutomation = _GUID(
    0xFF48DBA4, 0x60EF, 0x4201, (ctypes.c_ubyte * 8)(0xAA, 0x87, 0x54, 0x10, 0x3E, 0xEF, 0x59, 0x4E)
)
_IID_IUIAutomation = _GUID(
    0x30CBE57D, 0xD9D0, 0x452A, (ctypes.c_ubyte * 8)(0xAB, 0x13, 0x7A, 0xC5, 0xAC, 0x48, 0x25, 0xEE)
)


def _init_paste_win32_apis() -> Tuple[Any, Any, Any, Any]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    user32.GetCursorPos.restype = ctypes.c_int
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = ctypes.c_int
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
    user32.MapVirtualKeyW.restype = ctypes.c_uint
    if hasattr(user32, "SetThreadDpiAwarenessContext"):
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p

    # 64bit環境でのHANDLE切り捨てを防ぐため全kernel32 APIに厳密な型を設定する
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long

    oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
    oleaut32.SysAllocString.restype = ctypes.c_void_p
    oleaut32.VariantClear.argtypes = [ctypes.POINTER(_VARIANT)]
    oleaut32.VariantClear.restype = ctypes.c_long
    oleaut32.SafeArrayGetLBound.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
    oleaut32.SafeArrayGetLBound.restype = ctypes.c_long
    oleaut32.SafeArrayGetUBound.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
    oleaut32.SafeArrayGetUBound.restype = ctypes.c_long
    oleaut32.SafeArrayAccessData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    oleaut32.SafeArrayAccessData.restype = ctypes.c_long
    oleaut32.SafeArrayUnaccessData.argtypes = [ctypes.c_void_p]
    oleaut32.SafeArrayUnaccessData.restype = ctypes.c_long

    return user32, kernel32, ole32, oleaut32


def _com_call(obj_ptr: ctypes.c_void_p, index: int, restype: Any, argtypes: Sequence[Any], *args: Any) -> Any:
    vtable = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn_type = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = fn_type(vtable[index])
    return fn(obj_ptr, *args)


def _com_release(obj_ptr: Optional[ctypes.c_void_p]) -> None:
    if obj_ptr and obj_ptr.value:
        _com_call(obj_ptr, 2, ctypes.c_uint32, ())


def _get_hwnd_process_exe_name(hwnd: int, user32: Any, kernel32: Any) -> Optional[str]:
    pid = ctypes.c_uint32(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    h_proc = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid.value)
    if not h_proc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(len(buf))
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return None
    finally:
        kernel32.CloseHandle(h_proc)


def _get_window_title(hwnd: int, user32: Any) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


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


def _uia_find_all(el: ctypes.c_void_p, cond: ctypes.c_void_p) -> List[ctypes.c_void_p]:
    arr = ctypes.c_void_p()
    hr = _com_call(
        el, 6, ctypes.c_long,
        (ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
        _TreeScope_Descendants, cond, ctypes.byref(arr),
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


def _uia_find_first(el: ctypes.c_void_p, cond: ctypes.c_void_p) -> Optional[ctypes.c_void_p]:
    found = ctypes.c_void_p()
    hr = _com_call(
        el, 5, ctypes.c_long,
        (ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
        _TreeScope_Descendants, cond, ctypes.byref(found),
    )
    return found if hr >= 0 and found.value else None


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


def _collect_element_match_texts(
    p_uia: ctypes.c_void_p, el: ctypes.c_void_p, oleaut32: Any, text_cond: Optional[ctypes.c_void_p]
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


def _find_visible_active_document(
    p_uia: ctypes.c_void_p, root_el: ctypes.c_void_p, oleaut32: Any
) -> Optional[ctypes.c_void_p]:
    doc_cond = _uia_create_int_condition(p_uia, _UIA_ControlTypePropertyId, _UIA_DocumentControlTypeId)
    if not doc_cond:
        return None
    try:
        docs = _uia_find_all(root_el, doc_cond)
        chosen: Optional[ctypes.c_void_p] = None
        for doc in docs:
            if _uia_get_prop_bool(doc, oleaut32, _UIA_IsOffscreenPropertyId, default=False):
                _com_release(doc)
                continue
            rect = _uia_get_bounding_rect(doc, oleaut32)
            if not rect or rect[2] <= 0 or rect[3] <= 0:
                _com_release(doc)
                continue
            if chosen is None:
                chosen = doc
            else:
                _com_release(doc)
        return chosen
    finally:
        _com_release(doc_cond)


def _find_best_prompt_element(
    p_uia: ctypes.c_void_p,
    root_el: ctypes.c_void_p,
    oleaut32: Any,
    labels: Sequence[str],
    quick_probe: bool = False,
) -> Tuple[Optional[ctypes.c_void_p], str, str]:
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
        doc_el: Optional[ctypes.c_void_p] = None
        max_attempts = 4 if quick_probe else 12
        sleep_sec = 0.06 if quick_probe else 0.08
        early_dom_attempt = 2 if quick_probe else 3
        try:
            for attempt in range(max_attempts):
                if doc_el is None:
                    doc_el = _find_visible_active_document(p_uia, root_el, oleaut32)
                if doc_el is None:
                    time.sleep(sleep_sec)
                    continue

                candidates = _uia_find_all(doc_el, input_cond)
                best_el: Optional[ctypes.c_void_p] = None
                best_score = 0
                best_label = ""

                for el in candidates:
                    try:
                        if not _uia_get_prop_bool(el, oleaut32, _UIA_IsEnabledPropertyId, default=False):
                            continue
                        if _uia_get_prop_bool(el, oleaut32, _UIA_IsOffscreenPropertyId, default=False):
                            continue
                        if not _uia_get_prop_bool(el, oleaut32, _UIA_IsKeyboardFocusablePropertyId, default=False):
                            continue
                        if _uia_get_prop_bool(el, oleaut32, _UIA_IsPasswordPropertyId, default=False):
                            continue
                        if _uia_get_prop_bool(el, oleaut32, _UIA_ValueIsReadOnlyPropertyId, default=False):
                            continue
                        rect = _uia_get_bounding_rect(el, oleaut32)
                        if not rect or rect[2] <= 0 or rect[3] <= 0:
                            continue

                        texts = _collect_element_match_texts(p_uia, el, oleaut32, text_cond)
                        score, matched_lbl = _score_candidate(texts, labels_lower)
                        if score > best_score:
                            _com_release(best_el)
                            best_el = el
                            best_score = score
                            best_label = matched_lbl
                            el = None  # 所有権移転
                    finally:
                        _com_release(el)

                if best_el is not None:
                    return best_el, best_label, ""

                # アクティブDocumentのDOMが展開済みかつ数回試して見つからない場合は早期終了する
                if attempt >= early_dom_attempt and true_cond is not None:
                    any_child = _uia_find_first(doc_el, true_cond)
                    has_dom = any_child is not None
                    _com_release(any_child)
                    if has_dom:
                        return None, "", "ページ内に対象のプロンプト入力欄が見つかりませんでした"

                time.sleep(sleep_sec)

            return None, "", "プロンプト入力欄の検出がタイムアウトしました"
        finally:
            _com_release(doc_el)
    finally:
        for c in (input_cond, edit_cond, textbox_cond, text_cond, true_cond):
            if c is not None and c is not input_cond:
                _com_release(c)
        if input_cond is not None:
            _com_release(input_cond)


def _send_key_release_if_pressed(user32: Any, inputs: List[_INPUT], vk: int) -> None:
    if user32.GetAsyncKeyState(vk) & 0x8000:
        scan = user32.MapVirtualKeyW(vk, 0)
        ev = _INPUT(type=_INPUT_KEYBOARD)
        ev.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=_KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
        inputs.append(ev)


def _send_ctrl_v_clean(user32: Any) -> bool:
    inputs: List[_INPUT] = []
    # コマンド実行直後にユーザーが押下している修飾キーによる誤結合(Ctrl+Shift+V等)を防止する
    for mod_vk in (
        _VK_SHIFT, _VK_LSHIFT, _VK_RSHIFT,
        _VK_MENU, _VK_LMENU, _VK_RMENU,
        _VK_LWIN, _VK_RWIN,
        _VK_CONTROL, _VK_LCONTROL, _VK_RCONTROL,
    ):
        _send_key_release_if_pressed(user32, inputs, mod_vk)

    scan_ctrl = user32.MapVirtualKeyW(_VK_CONTROL, 0)
    scan_v = user32.MapVirtualKeyW(_VK_V, 0)
    for vk, scan, flags in (
        (_VK_CONTROL, scan_ctrl, 0),
        (_VK_V, scan_v, 0),
        (_VK_V, scan_v, _KEYEVENTF_KEYUP),
        (_VK_CONTROL, scan_ctrl, _KEYEVENTF_KEYUP),
    ):
        ev = _INPUT(type=_INPUT_KEYBOARD)
        ev.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        inputs.append(ev)

    arr = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
    if sent == len(inputs):
        time.sleep(0.05)
        return True
    return False


def _click_safe_region_preserving_cursor(user32: Any, el: ctypes.c_void_p, oleaut32: Any) -> bool:
    prev_dpi = None
    has_dpi_api = hasattr(user32, "SetThreadDpiAwarenessContext")
    if has_dpi_api:
        prev_dpi = user32.SetThreadDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        if not prev_dpi:
            prev_dpi = user32.SetThreadDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE)

    try:
        rect = _uia_get_bounding_rect(el, oleaut32)
        if not rect or rect[2] <= 0 or rect[3] <= 0:
            return False

        # 右側・下側のオーバーレイボタンを避け入力欄左上の安全領域を対象にする
        click_x = int(round(rect[0] + min(24.0, rect[2] * 0.2)))
        click_y = int(round(rect[1] + min(16.0, rect[3] * 0.5)))

        orig_pt = _POINT()
        has_orig = bool(user32.GetCursorPos(ctypes.byref(orig_pt)))
        if not user32.SetCursorPos(click_x, click_y):
            return False
        try:
            time.sleep(0.02)
            down = _INPUT(type=_INPUT_MOUSE)
            down.union.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=_MOUSEEVENTF_LEFTDOWN, time=0, dwExtraInfo=0)
            up = _INPUT(type=_INPUT_MOUSE)
            up.union.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=_MOUSEEVENTF_LEFTUP, time=0, dwExtraInfo=0)
            arr = (_INPUT * 2)(down, up)
            return user32.SendInput(2, arr, ctypes.sizeof(_INPUT)) == 2
        finally:
            if has_orig:
                time.sleep(0.01)
                user32.SetCursorPos(orig_pt.x, orig_pt.y)
    finally:
        if has_dpi_api and prev_dpi:
            user32.SetThreadDpiAwarenessContext(prev_dpi)


def paste_windows_browser_prompt(
    prompt_labels: Sequence[str],
    target_hwnd: Optional[int] = None,
    allowed_title_keywords: Sequence[str] = (),
    allow_non_empty: bool = False,
    quick_probe: bool = False,
) -> PasteOutcome:
    """Windowsブラウザの対象プロンプト入力欄を検出し、フォーカスしてCtrl+Vを送信する。"""
    exe_names = set(_get_windows_default_browser_exe_names())
    if not exe_names:
        return PasteOutcome(False, "デフォルトブラウザの実行ファイルが特定できませんでした", False, PasteErrorKind.UNSUPPORTED)

    user32, kernel32, ole32, oleaut32 = _init_paste_win32_apis()
    fg_hwnd_ptr = user32.GetForegroundWindow()
    fg_hwnd = int(fg_hwnd_ptr) if fg_hwnd_ptr else 0
    if not fg_hwnd:
        # ウィンドウ遷移直後の一時的なフォーカス喪失時のみリトライを許容する
        return PasteOutcome(False, "対象ブラウザが最前面にありません", True, PasteErrorKind.WINDOW_LOST)

    fg_exe = _get_hwnd_process_exe_name(fg_hwnd, user32, kernel32)
    if target_hwnd:
        if fg_hwnd == target_hwnd:
            hwnd = target_hwnd
        elif not user32.IsWindow(ctypes.c_void_p(target_hwnd)) or not user32.IsWindowVisible(ctypes.c_void_p(target_hwnd)):
            # 一時ウィンドウ終了後に新ブラウザウィンドウへ遷移した場合のみ追従し、非ブラウザ復帰時は奪取しない
            if fg_exe in exe_names:
                hwnd = fg_hwnd
            else:
                return PasteOutcome(
                    False,
                    "対象ブラウザウィンドウが閉じられ別アプリへ切り替わったため中断しました",
                    False,
                    PasteErrorKind.USER_ABORTED,
                )
        elif fg_exe in exe_names:
            return PasteOutcome(
                False,
                "別のブラウザウィンドウが最前面になったため中断しました",
                False,
                PasteErrorKind.USER_ABORTED,
            )
        else:
            return PasteOutcome(
                False,
                "別アプリへ切り替わったため自動貼り付けを中断しました",
                False,
                PasteErrorKind.USER_ABORTED,
            )
    else:
        hwnd = fg_hwnd

    if fg_exe not in exe_names:
        return PasteOutcome(
            False,
            f"最前面ウィンドウ ({fg_exe}) がデフォルトブラウザではありません",
            False,
            PasteErrorKind.USER_ABORTED,
        )

    title = _get_window_title(hwnd, user32)
    if is_login_or_auth_title(title):
        return PasteOutcome(
            False,
            f"認証・ログイン画面のため自動貼り付けを中断しました (title: {title})",
            False,
            PasteErrorKind.LOGIN_REQUIRED,
            target_window=hwnd,
        )
    if allowed_title_keywords:
        title_lower = title.lower()
        if not any(kw in title_lower for kw in allowed_title_keywords):
            return PasteOutcome(
                False,
                f"許可対象外のページのため自動貼り付けをスキップしました (title: {title})",
                True,
                PasteErrorKind.TRANSIENT,
                target_window=hwnd,
            )

    hr_co = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    should_uninit = hr_co in (0, 1)
    if hr_co < 0 and hr_co != _RPC_E_CHANGED_MODE:
        return PasteOutcome(False, f"COM初期化に失敗しました (HRESULT={hr_co:#x})", False, PasteErrorKind.FAILED, target_window=hwnd)

    p_uia = ctypes.c_void_p()
    root_el = ctypes.c_void_p()
    target_el: Optional[ctypes.c_void_p] = None
    try:
        hr_create = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_CUIAutomation),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_IUIAutomation),
            ctypes.byref(p_uia),
        )
        if hr_create < 0 or not p_uia.value:
            return PasteOutcome(False, f"UI Automationの初期化に失敗しました (HRESULT={hr_create:#x})", False, PasteErrorKind.FAILED, target_window=hwnd)

        hr_el = _com_call(
            p_uia, 6, ctypes.c_long, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            ctypes.c_void_p(hwnd), ctypes.byref(root_el),
        )
        if hr_el < 0 or not root_el.value:
            return PasteOutcome(False, f"ブラウザウィンドウのUIA要素取得に失敗しました (HRESULT={hr_el:#x})", True, PasteErrorKind.TRANSIENT, target_window=hwnd)

        target_el, matched_lbl, err_reason = _find_best_prompt_element(
            p_uia, root_el, oleaut32, prompt_labels, quick_probe=quick_probe
        )
        if target_el is None:
            is_cond_err = "検索条件の生成" in err_reason
            return PasteOutcome(
                False,
                err_reason,
                not is_cond_err,
                PasteErrorKind.FAILED if is_cond_err else PasteErrorKind.TRANSIENT,
                target_window=hwnd,
            )

        if not allow_non_empty:
            existing_val = _uia_get_prop_str(target_el, oleaut32, _UIA_ValueValuePropertyId).strip()
            if existing_val and existing_val.lower() != matched_lbl:
                return PasteOutcome(
                    False,
                    "入力欄に既存テキストが存在するため自動貼り付けをスキップしました",
                    False,
                    PasteErrorKind.USER_ABORTED,
                    target_window=hwnd,
                )

        # 物理カーソル移動やオーバーレイ誤爆を避けるためUIA SetFocusを最優先する
        _com_call(target_el, 3, ctypes.c_long, ())
        time.sleep(0.04)
        has_focus = _uia_get_prop_bool(target_el, oleaut32, _UIA_HasKeyboardFocusPropertyId, default=False)
        if not has_focus:
            if not _click_safe_region_preserving_cursor(user32, target_el, oleaut32):
                return PasteOutcome(False, "プロンプト入力欄へのフォーカス設定に失敗しました", True, PasteErrorKind.TRANSIENT, target_window=hwnd)
            time.sleep(0.05)

        cur_fg_ptr = user32.GetForegroundWindow()
        cur_fg = int(cur_fg_ptr) if cur_fg_ptr else 0
        if not cur_fg or cur_fg != hwnd:
            if not cur_fg:
                return PasteOutcome(
                    False,
                    "貼り付け直前にフォアグラウンドウィンドウが失われたため再試行します",
                    True,
                    PasteErrorKind.WINDOW_LOST,
                )
            cur_fg_exe = _get_hwnd_process_exe_name(cur_fg, user32, kernel32)
            if (
                cur_fg_exe in exe_names
                and (not user32.IsWindow(ctypes.c_void_p(hwnd)) or not user32.IsWindowVisible(ctypes.c_void_p(hwnd)))
            ):
                return PasteOutcome(
                    False,
                    "対象ブラウザウィンドウが新ウィンドウへ切り替わったため再試行します",
                    True,
                    PasteErrorKind.TRANSIENT,
                    target_window=cur_fg,
                )
            return PasteOutcome(
                False,
                "貼り付け直前に別ウィンドウへ切り替わったため中断しました",
                False,
                PasteErrorKind.USER_ABORTED,
            )

        if not _send_ctrl_v_clean(user32):
            return PasteOutcome(False, "Ctrl+Vキーイベントの送信に失敗しました", False, PasteErrorKind.FAILED, target_window=hwnd)

        return PasteOutcome(True, "", False, PasteErrorKind.NONE, target_window=hwnd)
    finally:
        _com_release(target_el)
        _com_release(root_el)
        _com_release(p_uia)
        if should_uninit:
            ole32.CoUninitialize()
