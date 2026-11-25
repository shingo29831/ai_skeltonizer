"""Module: @role: macOS JXA向けのブラウザウィンドウ・CGWindow・タイトル・AXWebArea・フォーカス検証ヘルパーJSを生成する。"""

_MACOS_JXA_WINDOW_HELPERS = r"""
    function isLoadingTitle(rawTitle) {
        if (!loadingRe) return false;
        var core = String(rawTitle || "").trim();
        var strippedBrowserSuffix = false;
        while (core && suffixRe) {
            var next = core.replace(suffixRe, "").trim();
            if (next === core) break;
            core = next;
            strippedBrowserSuffix = true;
        }
        if (loadingRe.test(core)) return true;
        if (strippedBrowserSuffix) {
            if (standaloneProfileRe && standaloneProfileRe.test(core)) return true;
            if (customProfileSuffixRe) {
                var match = core.match(customProfileSuffixRe);
                if (match) {
                    var tail = match[0];
                    if (!serviceKeywordsRe || !serviceKeywordsRe.test(tail)) {
                        var withoutProfile = core.slice(0, core.length - tail.length).trim();
                        if (loadingRe.test(withoutProfile)) return true;
                    }
                }
            }
            return false;
        }
        if (trailingCustomProfileSuffixRe) {
            var trailingMatch = core.match(trailingCustomProfileSuffixRe);
            if (trailingMatch) {
                var tTail = trailingMatch[0];
                if (!serviceKeywordsRe || !serviceKeywordsRe.test(tTail)) {
                    var withoutTrailing = core.slice(0, core.length - tTail.length).trim();
                    if (loadingRe.test(withoutTrailing) || (standaloneProfileRe && standaloneProfileRe.test(withoutTrailing))) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    function isWebAreaLoaded(area) {
        try {
            var loaded = area.attributes.byName("AXLoaded").value();
            if (loaded === false) return false;
        } catch (e) {}
        return true;
    }

    function getProcessCGWindowIds(pid, frontWin) {
        if (!cgAvailable || !pid) return null;
        try {
            var raw = $.CGWindowListCopyWindowInfo(
                $.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements,
                $.kCGNullWindowID
            );
            if (!raw) return null;
            var list = ObjC.deepUnwrap(raw);
            if (!list || !list.length) return null;
            var winPos = null;
            var winSize = null;
            try { winPos = frontWin ? frontWin.position() : null; } catch (e) {}
            try { winSize = frontWin ? frontWin.size() : null; } catch (e) {}
            var ids = [];
            var matchedId = null;
            for (var i = 0; i < list.length; i++) {
                var item = list[i];
                if (item && item.kCGWindowOwnerPID === pid && item.kCGWindowLayer === 0) {
                    var b = item.kCGWindowBounds;
                    if (b && b.Width > 50 && b.Height > 50 && typeof item.kCGWindowNumber === "number") {
                        ids.push(item.kCGWindowNumber);
                        if (
                            matchedId === null && winPos && winSize &&
                            Math.abs(b.X - winPos[0]) <= 4 && Math.abs(b.Y - winPos[1]) <= 4 &&
                            Math.abs(b.Width - winSize[0]) <= 4 && Math.abs(b.Height - winSize[1]) <= 4
                        ) {
                            matchedId = item.kCGWindowNumber;
                        }
                    }
                }
            }
            if (ids.length === 0) return null;
            return {frontId: matchedId !== null ? matchedId : ids[0], allIds: ids};
        } catch (e) {
            return null;
        }
    }

    function validateActiveWindow(appPid, initialWinId, switchedOtherMsg) {
        if (!appProc.frontmost()) {
            var activeProcs = [];
            try { activeProcs = se.applicationProcesses.whose({frontmost: true})(); } catch (e) {}
            if (activeProcs && activeProcs.length > 0) {
                return {err: JSON.stringify({ok: false, retryable: false, errorKind: "user_aborted", reason: switchedOtherMsg}), win: null};
            }
            return {err: JSON.stringify({ok: false, retryable: true, errorKind: "window_lost", reason: "ウィンドウ遷移中のため再試行します"}), win: null};
        }
        var curWins = [];
        try { curWins = appProc.windows(); } catch (e) {}
        if (!curWins || curWins.length === 0) {
            return {err: JSON.stringify({ok: false, retryable: true, errorKind: "window_lost", reason: "ブラウザウィンドウが一時的に見つからないため再試行します"}), win: null};
        }
        var curFront = curWins[0];
        if (initialWinId !== null) {
            var cgInfo = getProcessCGWindowIds(appPid, curFront);
            if (cgInfo && cgInfo.frontId !== initialWinId) {
                if (cgInfo.allIds.indexOf(initialWinId) !== -1) {
                    return {err: JSON.stringify({ok: false, retryable: false, errorKind: "user_aborted", reason: switchedOtherMsg}), win: null};
                }
                return {err: JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "対象ブラウザウィンドウが切り替わったため再試行します"}), win: null};
            }
        }
        return {err: null, win: curFront};
    }

    function getAxOptionalAttr(el, attrName) {
        try {
            var v = el.attributes.byName(attrName).value();
            return v ? String(v) : "";
        } catch (e) {
            return "";
        }
    }

    function sameAxElement(a, b) {
        try {
            var aPos = a.position();
            var aSize = a.size();
            var bPos = b.position();
            var bSize = b.size();
            var aRole = String(a.role() || "");
            var bRole = String(b.role() || "");
            if (
                aRole !== bRole || !aPos || !bPos || !aSize || !bSize ||
                Math.abs(aPos[0] - bPos[0]) > 2 || Math.abs(aPos[1] - bPos[1]) > 2 ||
                Math.abs(aSize[0] - bSize[0]) > 2 || Math.abs(aSize[1] - bSize[1]) > 2
            ) {
                return false;
            }
            var checkAttrs = ["AXSubrole", "AXDOMIdentifier", "AXIdentifier", "AXDescription", "AXTitle"];
            for (var i = 0; i < checkAttrs.length; i++) {
                var av = getAxOptionalAttr(a, checkAttrs[i]);
                var bv = getAxOptionalAttr(b, checkAttrs[i]);
                if (av && bv && av !== bv) return false;
            }
            return true;
        } catch (e) {
            return false;
        }
    }

    function hasFocusedDescendant(node, depth, state) {
        if (depth > 8 || state.visited >= 60) return false;
        state.visited += 1;
        var role = "";
        try { role = String(node.role() || ""); } catch (e) { return false; }
        if (depth > 0 && NON_INPUT_ROLES[role]) return false;
        try {
            if (node.focused() === true) return true;
        } catch (e) {}
        var children = [];
        try { children = node.uiElements(); } catch (e) { return false; }
        for (var i = 0; i < children.length; i++) {
            if (hasFocusedDescendant(children[i], depth + 1, state)) return true;
        }
        return false;
    }

    function isFocusedElementInsidePrompt(promptEl) {
        var focusedEl = null;
        try { focusedEl = appProc.attributes.byName("AXFocusedUIElement").value(); } catch (e) {}
        if (!focusedEl) return false;
        var fRole = "";
        try { fRole = String(focusedEl.role() || ""); } catch (e) {}
        if (NON_INPUT_ROLES[fRole] || CONTAINER_ROLES[fRole]) return false;
        if (sameAxElement(focusedEl, promptEl)) return true;

        var cur = focusedEl;
        var traversedParent = false;
        var reachedBoundary = false;
        for (var step = 0; step < 48; step++) {
            var parentEl = null;
            try { parentEl = cur.attributes.byName("AXParent").value(); } catch (e) { parentEl = null; }
            if (!parentEl) {
                if (traversedParent) reachedBoundary = true;
                break;
            }
            traversedParent = true;
            if (sameAxElement(parentEl, promptEl)) return true;
            var pRole = "";
            try { pRole = String(parentEl.role() || ""); } catch (e) {}
            if (pRole === "AXWebArea" || pRole === "AXWindow" || pRole === "AXApplication") {
                reachedBoundary = true;
                break;
            }
            cur = parentEl;
        }
        if (traversedParent && reachedBoundary) return false;

        try {
            var pPos = promptEl.position();
            var pSize = promptEl.size();
            var fPos = focusedEl.position();
            var fSize = focusedEl.size();
            if (pPos && pSize && fPos && fSize && fSize[0] > 0 && fSize[1] > 0) {
                return (
                    fPos[0] >= (pPos[0] - 2) &&
                    fPos[1] >= (pPos[1] - 2) &&
                    (fPos[0] + fSize[0]) <= (pPos[0] + pSize[0] + 2) &&
                    (fPos[1] + fSize[1]) <= (pPos[1] + pSize[1] + 2)
                );
            }
        } catch (e) {}
        return false;
    }

    function verifyPromptFocused(promptEl) {
        try {
            if (promptEl.focused() === true) return true;
        } catch (e) {}
        if (hasFocusedDescendant(promptEl, 0, {visited: 0})) return true;
        if (isFocusedElementInsidePrompt(promptEl)) return true;
        return false;
    }

    function findWebArea(node, depth) {
        if (depth > 14) return null;
        try {
            if (node.role() === "AXWebArea") return node;
        } catch (e) { return null; }
        var children = [];
        try { children = node.uiElements(); } catch (e) { return null; }
        for (var i = 0; i < children.length; i++) {
            var found = findWebArea(children[i], depth + 1);
            if (found) return found;
        }
        return null;
    }
"""

def build_macos_jxa_window_helpers() -> str:
    """macOS JXA用のウィンドウ状態・CGWindow検証・AXフォーカス検証ヘルパー関数群を返す。"""
    return _MACOS_JXA_WINDOW_HELPERS.strip("\n")
