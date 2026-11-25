"""Module: @role: Linux AT-SPI要素探索・キャッシュ制御・ドキュメント状態およびフォーカス検証ヘルパーを提供する。"""
from collections import deque
from typing import Any, Deque, List, Optional, Sequence, Set, Tuple

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

def _is_atspi_prompt_empty(node: Any, labels_lower: Sequence[str], matched_lbl: str = "") -> bool:
    """AT-SPI入力要素が空または要素自身のプレースホルダー文字列と一致するか判定する。"""
    existing = _get_atspi_existing_text(node)
    if not existing:
        return True
    existing_lower = existing.lower()
    allowed = {lbl.strip().lower() for lbl in labels_lower if lbl and lbl.strip()}
    if matched_lbl:
        allowed.add(matched_lbl.strip().lower())
    if existing_lower in allowed:
        return True
    try:
        attrs = node.get_attributes() or {}
        for key in ("placeholder-text", "placeholder", "aria-placeholder"):
            val = (attrs.get(key) or "").strip().lower()
            if val and val == existing_lower:
                return True
        for raw_attr in ((node.get_name() or "").strip(), (node.get_description() or "").strip()):
            attr_low = raw_attr.lower()
            if attr_low and attr_low == existing_lower and any(lbl in attr_low for lbl in allowed):
                return True
    except Exception:
        pass
    return False

def _clear_atspi_single_cache(node: Any) -> None:
    # 走査ループ内でのO(N^2)再帰クリアを防ぐため単一ノードのみクリアする
    if node is None:
        return
    try:
        if hasattr(node, "clear_cache_single"):
            node.clear_cache_single()
    except Exception:
        pass

def _clear_atspi_subtree_cache(node: Any) -> None:
    if node is None:
        return
    try:
        if hasattr(node, "clear_cache"):
            node.clear_cache()
    except Exception:
        pass

def _disable_atspi_cache(desktop: Any, atspi_mod: Any) -> None:
    try:
        cache_enum = getattr(atspi_mod, "Cache", None)
        if cache_enum is not None and hasattr(desktop, "set_cache_mask"):
            desktop.set_cache_mask(cache_enum.NONE)
    except Exception:
        pass
    _clear_atspi_subtree_cache(desktop)

def _is_atspi_document_busy(root_win: Any, atspi_mod: Any, max_nodes: int = 1000) -> bool:
    """表示中タブのDocumentノードのみを対象にAT-SPIのBUSY状態を判定する。"""
    if not hasattr(root_win, "clear_cache_single"):
        _clear_atspi_subtree_cache(root_win)

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

    # 多数のタブヘッダでノード上限を消費しないよう非タブ領域を先に探索しタブ領域は後回しにする
    main_queue: Deque[Tuple[Any, int]] = deque([(root_win, 0)])
    tab_queue: Deque[Tuple[Any, int]] = deque()
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
                # 非表示のバックグラウンドタブを読込中と誤認しないようVISIBLEかつSHOWINGのみ判定する
                if states.contains(atspi_mod.StateType.VISIBLE) and states.contains(atspi_mod.StateType.SHOWING):
                    if states.contains(atspi_mod.StateType.BUSY):
                        return True
                continue
            if depth > 0 and role in skip_chrome_roles:
                continue
            if role == atspi_mod.Role.PAGE_TAB:
                states = node.get_state_set()
                if not (states.contains(atspi_mod.StateType.VISIBLE) and states.contains(atspi_mod.StateType.SHOWING)):
                    continue
                if states.contains(atspi_mod.StateType.SELECTABLE) and not states.contains(atspi_mod.StateType.SELECTED):
                    continue
            if depth >= 16:
                continue
            target_q = tab_queue if role in tab_roles else main_queue
            count = node.get_child_count()
            for i in range(count):
                child = node.get_child_at_index(i)
                if child is not None:
                    target_q.append((child, depth + 1))
        except Exception:
            continue
    return False

def _verify_atspi_node_focused(node: Any, atspi_mod: Any, max_nodes: int = 200) -> bool:
    """対象ノードまたは内部の入力子孫要素がAT-SPI上でFOCUSEDを獲得したか検証する。"""
    if not hasattr(node, "clear_cache_single"):
        _clear_atspi_subtree_cache(node)

    non_input_roles = {
        atspi_mod.Role.PUSH_BUTTON,
        atspi_mod.Role.TOGGLE_BUTTON,
        atspi_mod.Role.LINK,
        atspi_mod.Role.MENU_ITEM,
        atspi_mod.Role.CHECK_BOX,
        atspi_mod.Role.RADIO_BUTTON,
        atspi_mod.Role.COMBO_BOX,
        atspi_mod.Role.PAGE_TAB,
        atspi_mod.Role.SCROLL_BAR,
        atspi_mod.Role.SLIDER,
        atspi_mod.Role.SPIN_BUTTON,
    }
    queue: Deque[Tuple[Any, int]] = deque([(node, 0)])
    visited = 0
    while queue and visited < max_nodes:
        cur, depth = queue.popleft()
        if cur is None:
            continue
        visited += 1
        try:
            _clear_atspi_single_cache(cur)
            if depth > 0 and cur.get_role() in non_input_roles:
                continue
            if cur.get_state_set().contains(atspi_mod.StateType.FOCUSED):
                return True
            if depth >= 16:
                continue
            for i in range(cur.get_child_count()):
                child = cur.get_child_at_index(i)
                if child is not None:
                    queue.append((child, depth + 1))
        except Exception:
            continue
    return False

def _has_focused_atspi_document(root_win: Any, atspi_mod: Any, max_nodes: int = 800) -> bool:
    doc_roles = {atspi_mod.Role.DOCUMENT_WEB, atspi_mod.Role.DOCUMENT_FRAME}
    queue: Deque[Tuple[Any, int]] = deque([(root_win, 0)])
    visited = 0
    while queue and visited < max_nodes:
        node, depth = queue.popleft()
        if node is None:
            continue
        visited += 1
        try:
            _clear_atspi_single_cache(node)
            if node.get_role() in doc_roles:
                states = node.get_state_set()
                if states.contains(atspi_mod.StateType.VISIBLE) and states.contains(atspi_mod.StateType.SHOWING):
                    if _verify_atspi_node_focused(node, atspi_mod):
                        return True
                continue
            if depth >= 16:
                continue
            for i in range(node.get_child_count()):
                child = node.get_child_at_index(i)
                if child is not None:
                    queue.append((child, depth + 1))
        except Exception:
            continue
    return False
