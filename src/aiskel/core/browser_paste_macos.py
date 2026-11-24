"""Module: @role: macOS環境においてJXA経由でブラウザ内のAXWebArea配下を走査し、プロンプト入力欄へのフォーカスとCmd+V貼り付けを実行する。"""
import json
import subprocess
from typing import Optional, Sequence, Tuple

from .browser_focus_macos import _get_macos_default_browser_bundle_id, _resolve_running_macos_bundle_id
from .browser_paste_types import (
    BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
    BROWSER_STANDALONE_PROFILE_REGEX_PATTERN,
    BROWSER_TITLE_SUFFIX_REGEX_PATTERN,
    BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
    LOADING_TITLE_REGEX_PATTERN,
    LOGIN_TITLE_REGEX_PATTERN,
    SERVICE_TITLE_KEYWORDS_REGEX_PATTERN,
    PasteErrorKind,
    PasteOutcome,
)

_MACOS_FOCUS_AND_PASTE_JXA = r"""
function run(argv) {
    var payload = JSON.parse(argv[0]);
    var bundleId = payload.bundleId;
    var labels = payload.labels;
    var titleKeywords = payload.titleKeywords;
    var loginRegex = payload.loginRegex || "";
    var loadingRegex = payload.loadingRegex || "";
    var browserSuffixRegex = payload.browserSuffixRegex || "";
    var standaloneProfileRegex = payload.standaloneProfileRegex || "";
    var trailingCustomProfileSuffixRegex = payload.trailingCustomProfileSuffixRegex || "";
    var customProfileSuffixRegex = payload.customProfileSuffixRegex || "";
    var serviceTitleKeywordsRegex = payload.serviceTitleKeywordsRegex || "";
    var allowNonEmpty = payload.allowNonEmpty;
    var quickProbe = !!payload.quickProbe;
    var verifyStability = !!payload.verifyStability;
    var frontmostWaitMs = quickProbe ? 700 : 1000;
    var webAreaWaitMs = quickProbe ? 400 : 1200;
    var findPromptTimeoutMs = quickProbe ? 800 : 1500;
    var suffixRe = browserSuffixRegex ? new RegExp(browserSuffixRegex, "i") : null;
    var standaloneProfileRe = standaloneProfileRegex ? new RegExp(standaloneProfileRegex, "i") : null;
    var trailingCustomProfileSuffixRe = trailingCustomProfileSuffixRegex ? new RegExp(trailingCustomProfileSuffixRegex, "i") : null;
    var customProfileSuffixRe = customProfileSuffixRegex ? new RegExp(customProfileSuffixRegex) : null;
    var serviceKeywordsRe = serviceTitleKeywordsRegex ? new RegExp(serviceTitleKeywordsRegex, "i") : null;
    var loadingRe = loadingRegex ? new RegExp(loadingRegex, "i") : null;
    var loginRe = loginRegex ? new RegExp(loginRegex, "i") : null;
    var NON_INPUT_ROLES = {
        "AXButton": true, "AXLink": true, "AXMenuItem": true,
        "AXCheckBox": true, "AXRadioButton": true, "AXPopUpButton": true,
        "AXDisclosureTriangle": true, "AXTab": true, "AXSwitch": true,
        "AXSlider": true, "AXIncrementor": true
    };
    var CONTAINER_ROLES = {
        "AXApplication": true, "AXWindow": true, "AXWebArea": true,
        "AXScrollArea": true, "AXToolbar": true, "AXTabGroup": true,
        "AXMenuBar": true, "AXMenu": true, "AXSheet": true, "AXDrawer": true
    };
    var cgAvailable = false;
    try {
        ObjC.import("CoreGraphics");
        cgAvailable = (typeof $.CGWindowListCopyWindowInfo === "function");
    } catch (e) {}

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

    var se = Application("System Events");
    var procs = se.applicationProcesses.whose({bundleIdentifier: bundleId})();
    if (procs.length === 0) {
        return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "対象ブラウザプロセスが見つかりません"});
    }
    var appProc = procs[0];
    var appPid = 0;
    try { appPid = Number(appProc.unixId() || 0); } catch (e) {}

    var deadline = Date.now() + frontmostWaitMs;
    while (!appProc.frontmost() && Date.now() < deadline) {
        delay(0.05);
    }
    if (!appProc.frontmost()) {
        // Space切替アニメーション等で最前面化が遅れた場合は再フォーカス付きリトライへ委ねる
        return JSON.stringify({ok: false, retryable: true, errorKind: "window_lost", reason: "対象ブラウザが最前面にないため再試行します"});
    }

    // Chromium/Safari/Firefoxのアクセシビリティツリー公開を有効化する
    try { appProc.attributes.byName("AXManualAccessibility").value = true; } catch (e) {}
    try { appProc.attributes.byName("AXEnhancedUserInterface").value = true; } catch (e) {}

    var wins = appProc.windows();
    if (wins.length === 0) {
        return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "ブラウザウィンドウが存在しません"});
    }
    var frontWin = wins[0];
    var initialCgInfo = getProcessCGWindowIds(appPid, frontWin);
    var initialWinId = initialCgInfo ? initialCgInfo.frontId : null;
    var winTitle = "";
    try { winTitle = String(frontWin.name() || ""); } catch (e) {}
    if (winTitle && loginRe && loginRe.test(winTitle)) {
        return JSON.stringify({ok: false, retryable: false, errorKind: "login_required", reason: "認証・ログイン画面のため自動貼り付けを中断しました (title: " + winTitle + ")"});
    }
    if (isLoadingTitle(winTitle)) {
        return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "サイトを読み込み中のため完了を待機しています (title: " + winTitle + ")"});
    }
    if (titleKeywords.length > 0) {
        var titleLower = winTitle.toLowerCase();
        var allowed = titleKeywords.some(function(kw) {
            var k = String(kw || "").trim().toLowerCase();
            return k && titleLower.indexOf(k) !== -1;
        });
        if (!allowed) {
            return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "許可対象外のページのため自動貼り付けをスキップしました (title: " + winTitle + ")"});
        }
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

    var webArea = null;
    var waitWebDeadline = Date.now() + webAreaWaitMs;
    while (!webArea && Date.now() < waitWebDeadline) {
        webArea = findWebArea(frontWin, 0);
        if (!webArea) delay(0.06);
    }
    if (!webArea) {
        return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "Webコンテンツ領域(AXWebArea)が検出できませんでした"});
    }
    if (!isWebAreaLoaded(webArea)) {
        return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "Webページを読み込み中のため完了を待機しています"});
    }

    function collectOwnAttrsMap(el) {
        var out = [];
        var attrs = ["AXPlaceholderValue", "AXHelp", "AXDescription", "AXTitle"];
        for (var i = 0; i < attrs.length; i++) {
            try {
                var v = el.attributes.byName(attrs[i]).value();
                if (v) out.push({name: attrs[i], val: String(v)});
            } catch (e) {}
        }
        return out;
    }

    function collectTexts(el) {
        var pairs = collectOwnAttrsMap(el);
        var out = [];
        for (var i = 0; i < pairs.length; i++) {
            out.push(pairs[i].val);
        }
        if (out.length === 0) {
            try {
                var sub = el.uiElements();
                for (var j = 0; j < Math.min(sub.length, 4); j++) {
                    try {
                        var st = sub[j].value() || sub[j].title() || sub[j].description();
                        if (st) out.push(String(st));
                    } catch (e2) {}
                }
            } catch (e) {}
        }
        return out;
    }

    function buildAllowedPlaceholders(el, matchedLabel) {
        var baseLabels = [];
        if (matchedLabel) baseLabels.push(matchedLabel);
        for (var i = 0; i < labels.length; i++) {
            var ll = String(labels[i] || "").trim().toLowerCase();
            if (ll && baseLabels.indexOf(ll) === -1) baseLabels.push(ll);
        }
        var allowed = baseLabels.slice();
        var pairs = collectOwnAttrsMap(el);
        for (var j = 0; j < pairs.length; j++) {
            var attrName = pairs[j].name;
            var attrLow = pairs[j].val.trim().toLowerCase();
            if (!attrLow) continue;
            // 明示的プレースホルダー属性はラベル語彙非依存で許容し、Name系はラベル語彙を含む場合のみ許容する
            if (attrName === "AXPlaceholderValue" || attrName === "AXHelp") {
                if (allowed.indexOf(attrLow) === -1) allowed.push(attrLow);
                continue;
            }
            for (var k = 0; k < baseLabels.length; k++) {
                if (attrLow.indexOf(baseLabels[k]) !== -1) {
                    if (allowed.indexOf(attrLow) === -1) allowed.push(attrLow);
                    break;
                }
            }
        }
        return allowed;
    }

    function isPlaceholderText(txtLower, allowedPlaceholders) {
        if (!txtLower) return true;
        return allowedPlaceholders.indexOf(txtLower) !== -1;
    }

    function hasNonEmptyChildText(node, allowedPlaceholders, depth, state) {
        if (depth > 4 || state.visited >= 30) return false;
        state.visited += 1;
        var role = "";
        try { role = String(node.role() || ""); } catch (e) { return false; }
        if (depth > 0 && (role === "AXStaticText" || role === "AXTextArea" || role === "AXTextField")) {
            var txt = "";
            try { txt = String(node.value() || node.title() || "").trim(); } catch (e) {}
            if (txt && !isPlaceholderText(txt.toLowerCase(), allowedPlaceholders)) {
                return true;
            }
        }
        var children = [];
        try { children = node.uiElements(); } catch (e) { return false; }
        for (var i = 0; i < children.length; i++) {
            if (hasNonEmptyChildText(children[i], allowedPlaceholders, depth + 1, state)) return true;
        }
        return false;
    }

    function isPromptValueEmpty(el, matchedLabel) {
        var allowedPlaceholders = buildAllowedPlaceholders(el, matchedLabel);
        try {
            var curVal = String(el.value() || "").trim();
            if (curVal && !isPlaceholderText(curVal.toLowerCase(), allowedPlaceholders)) return false;
        } catch (e) {}
        return !hasNonEmptyChildText(el, allowedPlaceholders, 0, {visited: 0});
    }

    function scoreTexts(texts) {
        var best = 0;
        var matched = "";
        for (var i = 0; i < texts.length; i++) {
            var norm = texts[i].trim().toLowerCase();
            if (!norm) continue;
            for (var j = 0; j < labels.length; j++) {
                var lbl = labels[j].trim().toLowerCase();
                if (!lbl) continue;
                if (norm === lbl) return {score: 100, label: lbl};
                if (norm.indexOf(lbl) !== -1 || (norm.length >= 4 && lbl.indexOf(norm) !== -1)) {
                    if (60 > best) {
                        best = 60;
                        matched = lbl;
                    }
                }
            }
        }
        return {score: best, label: matched};
    }

    var startTime = Date.now();
    var bestMatch = null;
    function findPrompt(node, depth) {
        if (depth > 48 || (Date.now() - startTime) > findPromptTimeoutMs) return;
        var role = "";
        try { role = node.role(); } catch (e) { return; }

        if (role === "AXTextArea" || role === "AXTextField" || role === "AXGroup") {
            var isInput = (role === "AXTextArea" || role === "AXTextField");
            if (!isInput) {
                try {
                    var editableAttr = node.attributes.byName("AXEditable").value();
                    if (editableAttr) isInput = true;
                } catch (e) {}
            }
            if (isInput) {
                try {
                    var sz = null;
                    try { sz = node.size(); } catch (eSize) {}
                    if (node.enabled() && (!sz || (sz[0] > 0 && sz[1] > 0))) {
                        var res = scoreTexts(collectTexts(node));
                        if (res.score > 0 && (!bestMatch || res.score > bestMatch.score)) {
                            bestMatch = {el: node, score: res.score, label: res.label};
                            if (res.score >= 100) return;
                        }
                    }
                } catch (e) {}
            }
        }
        var children = [];
        try { children = node.uiElements(); } catch (e) { return; }
        for (var i = 0; i < children.length; i++) {
            findPrompt(children[i], depth + 1);
            if (bestMatch && bestMatch.score >= 100) return;
        }
    }

    findPrompt(webArea, 0);
    if (!bestMatch) {
        return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "ページ内に対象のプロンプト入力欄が見つかりませんでした"});
    }

    if (verifyStability) {
        // リダイレクトやSPA再描画中の誤貼付を防ぐため待機後にウィンドウ・AXWebArea・要素を再検証する
        delay(0.35);
        var midWinCheck = validateActiveWindow(appPid, initialWinId, "ページ安定確認中に別アプリまたは別ウィンドウへ切り替わったため中断しました");
        if (midWinCheck.err) return midWinCheck.err;
        frontWin = midWinCheck.win;

        var midTitle = "";
        try { midTitle = String(frontWin.name() || ""); } catch (e) {}
        if (midTitle && loginRe && loginRe.test(midTitle)) {
            return JSON.stringify({ok: false, retryable: false, errorKind: "login_required", reason: "認証・ログイン画面のため自動貼り付けを中断しました (title: " + midTitle + ")"});
        }
        webArea = findWebArea(frontWin, 0);
        if (!webArea || midTitle !== winTitle || isLoadingTitle(midTitle) || !isWebAreaLoaded(webArea)) {
            return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "ページ遷移または読み込み中のため安定完了を待機しています"});
        }
        bestMatch = null;
        startTime = Date.now();
        findPrompt(webArea, 0);
        if (!bestMatch) {
            return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "ページ描画更新中のため再試行します"});
        }
    }

    var promptEl = bestMatch.el;
    if (!allowNonEmpty && !isPromptValueEmpty(promptEl, bestMatch.label)) {
        return JSON.stringify({ok: false, retryable: false, errorKind: "user_aborted", reason: "入力欄に既存テキストが存在するため自動貼り付けをスキップしました"});
    }

    try {
        promptEl.focused = true;
    } catch (e) {
        try {
            promptEl.actions.byName("AXPress").perform();
        } catch (e2) {
            return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "プロンプト入力欄へのフォーカス設定に失敗しました"});
        }
    }

    delay(0.05);
    if (!verifyPromptFocused(promptEl)) {
        try {
            promptEl.actions.byName("AXPress").perform();
            delay(0.06);
        } catch (e3) {}
        if (!verifyPromptFocused(promptEl)) {
            return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "プロンプト入力欄がフォーカスを受け付けないため再試行します"});
        }
    }

    var prePasteCheck = validateActiveWindow(appPid, initialWinId, "貼り付け直前に別アプリまたは別ウィンドウへ切り替わったため中断しました");
    if (prePasteCheck.err) return prePasteCheck.err;
    se.keystroke("v", {using: "command down"});
    return JSON.stringify({ok: true, retryable: false, errorKind: "none", reason: ""});
}
"""


def _parse_macos_error_kind(raw_kind: object, ok: bool) -> PasteErrorKind:
    if ok:
        return PasteErrorKind.NONE
    try:
        return PasteErrorKind(str(raw_kind))
    except ValueError:
        return PasteErrorKind.FAILED


def paste_macos_browser_prompt(
    prompt_labels: Sequence[str],
    target_bundle_id: Optional[str] = None,
    allowed_title_keywords: Sequence[str] = (),
    allow_non_empty: bool = False,
    quick_probe: bool = False,
    verify_stability: bool = False,
) -> PasteOutcome:
    """最前面のmacOSデフォルトブラウザでプロンプト入力欄を探してフォーカスし、Cmd+Vを送信する。"""
    if not prompt_labels:
        return PasteOutcome(False, "プロンプトラベルが指定されていません", False, PasteErrorKind.FAILED)

    resolved_id = target_bundle_id
    if not resolved_id:
        bundle_id = _get_macos_default_browser_bundle_id()
        resolved_id = _resolve_running_macos_bundle_id(bundle_id)
    if not resolved_id:
        return PasteOutcome(False, "起動中のデフォルトブラウザが特定できませんでした", True, PasteErrorKind.TRANSIENT)

    payload = json.dumps(
        {
            "bundleId": resolved_id,
            "labels": list(prompt_labels),
            "titleKeywords": list(allowed_title_keywords),
            "loginRegex": LOGIN_TITLE_REGEX_PATTERN,
            "loadingRegex": LOADING_TITLE_REGEX_PATTERN,
            "browserSuffixRegex": BROWSER_TITLE_SUFFIX_REGEX_PATTERN,
            "standaloneProfileRegex": BROWSER_STANDALONE_PROFILE_REGEX_PATTERN,
            "trailingCustomProfileSuffixRegex": BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
            "customProfileSuffixRegex": BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
            "serviceTitleKeywordsRegex": SERVICE_TITLE_KEYWORDS_REGEX_PATTERN,
            "allowNonEmpty": allow_non_empty,
            "quickProbe": bool(quick_probe),
            "verifyStability": bool(verify_stability),
        }
    )

    try:
        # 安定確認待機＋AXツリー探索負荷によるTimeoutExpired誤爆を防ぐため8秒確保する
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-", payload],
            input=_MACOS_FOCUS_AND_PASTE_JXA,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if res.returncode != 0:
            err_msg = (res.stderr or "").strip()
            if "assistive" in err_msg.lower() or "-25211" in err_msg or "-1719" in err_msg:
                return PasteOutcome(False, "macOSのアクセシビリティ権限が許可されていません", False, PasteErrorKind.UNSUPPORTED)
            return PasteOutcome(False, f"osascript実行に失敗しました: {err_msg or f'code={res.returncode}'}", False, PasteErrorKind.FAILED)

        out = (res.stdout or "").strip()
        if not out:
            return PasteOutcome(False, "osascriptから応答がありませんでした", False, PasteErrorKind.FAILED)
        parsed = json.loads(out)
        ok = bool(parsed.get("ok"))
        reason = str(parsed.get("reason", ""))
        retryable = bool(parsed.get("retryable", False)) if not ok else False
        error_kind = _parse_macos_error_kind(parsed.get("errorKind"), ok)
        return PasteOutcome(ok, reason, retryable, error_kind, target_window=resolved_id)
    except subprocess.TimeoutExpired:
        return PasteOutcome(False, "ブラウザUI探索がタイムアウトしました", True, PasteErrorKind.TRANSIENT, target_window=resolved_id)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return PasteOutcome(False, f"macOSブラウザ操作エラー: {e}", False, PasteErrorKind.FAILED)
