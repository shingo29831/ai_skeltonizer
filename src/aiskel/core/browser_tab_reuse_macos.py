"""Module: @role: macOS環境においてJXA/Accessibility経由で起動済みブラウザの空チャットタブを検出し、選択・最前面化を行う。"""
import json
import subprocess
from typing import Optional, Sequence

from .browser_focus_macos import _get_macos_default_browser_bundle_id, _resolve_running_macos_bundle_id
from .browser_paste_types import (
    BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
    BROWSER_STANDALONE_PROFILE_REGEX_PATTERN,
    BROWSER_TITLE_SUFFIX_REGEX_PATTERN,
    BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
    LOADING_TITLE_REGEX_PATTERN,
    LOGIN_TITLE_REGEX_PATTERN,
    SERVICE_TITLE_KEYWORDS_REGEX_PATTERN,
)
from .browser_tab_reuse_types import (
    CHAT_TURN_ACTION_BUTTON_KEYWORDS,
    NEW_CHAT_BUTTON_NAMES,
    TAB_STATUS_SUFFIX_REGEX_PATTERN,
    get_empty_chat_service_and_phrases,
    get_empty_chat_titles_for_url,
)

_MACOS_FIND_AND_ACTIVATE_EMPTY_TAB_JXA = r"""
function run(argv) {
    var payload = JSON.parse(argv[0]);
    var bundleId = payload.bundleId;
    var labels = payload.labels;
    var titleKeywords = payload.titleKeywords;
    var emptyChatTitles = payload.emptyChatTitles;
    var emptyServiceNames = payload.emptyServiceNames || [];
    var emptyCorePhrases = payload.emptyCorePhrases || [];
    var isAiStudio = !!payload.isAiStudio;
    var newChatButtonNames = payload.newChatButtonNames;
    var turnActionKeywords = payload.turnActionKeywords;
    var suffixRe = payload.browserSuffixRegex ? new RegExp(payload.browserSuffixRegex, "i") : null;
    var standaloneProfileRe = payload.standaloneProfileRegex ? new RegExp(payload.standaloneProfileRegex, "i") : null;
    var trailingCustomProfileSuffixRe = payload.trailingCustomProfileSuffixRegex ? new RegExp(payload.trailingCustomProfileSuffixRegex, "i") : null;
    var customProfileSuffixRe = payload.customProfileSuffixRegex ? new RegExp(payload.customProfileSuffixRegex) : null;
    var serviceKeywordsRe = payload.serviceTitleKeywordsRegex ? new RegExp(payload.serviceTitleKeywordsRegex, "i") : null;
    var loadingRe = payload.loadingRegex ? new RegExp(payload.loadingRegex, "i") : null;
    var loginRe = payload.loginRegex ? new RegExp(payload.loginRegex, "i") : null;
    var tabStatusSuffixRe = payload.tabStatusSuffixRegex ? new RegExp(payload.tabStatusSuffixRegex, "i") : null;
    var disabledNewChatTooltips = ["already in a new chat", "すでに新しいチャット", "既に新しいチャット"];
    var overallDeadline = Date.now() + 7000;

    function stripBrowserSuffix(rawTitle) {
        var core = String(rawTitle || "").trim();
        var stripped = false;
        while (core && suffixRe) {
            var next = core.replace(suffixRe, "").trim();
            if (next === core) break;
            core = next;
            stripped = true;
        }
        return {core: core, stripped: stripped};
    }

    function isLoadingTitle(rawTitle) {
        if (!loadingRe) return false;
        var res = stripBrowserSuffix(rawTitle);
        var core = res.core;
        if (loadingRe.test(core)) return true;
        if (res.stripped) {
            if (standaloneProfileRe && standaloneProfileRe.test(core)) return true;
            if (customProfileSuffixRe) {
                var m = core.match(customProfileSuffixRe);
                if (m && (!serviceKeywordsRe || !serviceKeywordsRe.test(m[0]))) {
                    if (loadingRe.test(core.slice(0, core.length - m[0].length).trim())) return true;
                }
            }
            return false;
        }
        if (trailingCustomProfileSuffixRe) {
            var tm = core.match(trailingCustomProfileSuffixRe);
            if (tm && (!serviceKeywordsRe || !serviceKeywordsRe.test(tm[0]))) {
                var w = core.slice(0, core.length - tm[0].length).trim();
                if (loadingRe.test(w) || (standaloneProfileRe && standaloneProfileRe.test(w))) return true;
            }
        }
        return false;
    }

    function firstNonEmptyLine(rawTitle) {
        var lines = String(rawTitle || "").split(/\r?\n/);
        for (var i = 0; i < lines.length; i++) {
            var trimmed = lines[i].trim();
            if (trimmed) return trimmed;
        }
        return "";
    }

    function normalizeTabTitle(rawTitle) {
        var cleaned = firstNonEmptyLine(rawTitle);
        while (cleaned) {
            var stripped = stripBrowserSuffix(cleaned).core;
            var next = tabStatusSuffixRe ? stripped.replace(tabStatusSuffixRe, "").trim() : stripped;
            if (next === cleaned) break;
            cleaned = next;
        }
        return cleaned.toLowerCase();
    }

    function stripSuffixesGuardingBlockedTitles(rawLine) {
        var cleaned = String(rawLine || "").trim();
        while (cleaned) {
            if ((loginRe && loginRe.test(cleaned)) || isLoadingTitle(cleaned)) return null;
            var stripped = stripBrowserSuffix(cleaned).core;
            if (stripped !== cleaned) {
                if (!stripped || (loginRe && loginRe.test(stripped)) || isLoadingTitle(stripped)) return null;
            }
            var next = tabStatusSuffixRe ? stripped.replace(tabStatusSuffixRe, "").trim() : stripped;
            if (next === cleaned) break;
            cleaned = next;
        }
        if (!cleaned || (loginRe && loginRe.test(cleaned)) || isLoadingTitle(cleaned)) return null;
        return cleaned.toLowerCase();
    }

    function isEmptyTabTitle(rawTitle) {
        var first = firstNonEmptyLine(rawTitle);
        if (!first) return false;
        var norm = stripSuffixesGuardingBlockedTitles(first);
        if (!norm) return false;
        if (emptyChatTitles.indexOf(norm) !== -1) return true;

        var rawParts = norm.split(/\s+(?:-|—|\||·)\s+|\s*(?:\||—|·)\s*|:\s+/);
        var parts = [];
        for (var i = 0; i < rawParts.length; i++) {
            var p = rawParts[i].trim();
            if (p) parts.push(p);
        }
        if (parts.length === 1) {
            return emptyServiceNames.indexOf(parts[0]) !== -1;
        }
        if (parts.length === 2) {
            return (
                (emptyServiceNames.indexOf(parts[0]) !== -1 && emptyCorePhrases.indexOf(parts[1]) !== -1) ||
                (emptyServiceNames.indexOf(parts[1]) !== -1 && emptyCorePhrases.indexOf(parts[0]) !== -1)
            );
        }
        return false;
    }

    function matchesAllowedTitle(winTitle) {
        if (!winTitle || (loginRe && loginRe.test(winTitle)) || isLoadingTitle(winTitle)) return false;
        if (titleKeywords.length === 0) return true;
        var lower = winTitle.toLowerCase();
        return titleKeywords.some(function(kw) { return kw && lower.indexOf(kw) !== -1; });
    }

    function findWebArea(node, depth) {
        if (depth > 14) return null;
        try { if (node.role() === "AXWebArea") return node; } catch (e) { return null; }
        var children = [];
        try { children = node.uiElements(); } catch (e) { return null; }
        for (var i = 0; i < children.length; i++) {
            var found = findWebArea(children[i], depth + 1);
            if (found) return found;
        }
        return null;
    }

    function isWebAreaLoaded(area) {
        try {
            if (area.attributes.byName("AXLoaded").value() === false) return false;
        } catch (e) {}
        return true;
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
                    if (60 > best) { best = 60; matched = lbl; }
                }
            }
        }
        return {score: best, label: matched};
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

    function isInputElementEmpty(el, matchedLabel) {
        var allowedPlaceholders = buildAllowedPlaceholders(el, matchedLabel);
        try {
            var curVal = String(el.value() || "").trim();
            if (curVal && !isPlaceholderText(curVal.toLowerCase(), allowedPlaceholders)) return false;
        } catch (e) {}
        return !hasNonEmptyChildText(el, allowedPlaceholders, 0, {visited: 0});
    }

    function isAxButtonDisabled(node, texts) {
        try {
            if (node.enabled() === false) return true;
        } catch (e) {}
        for (var i = 0; i < texts.length; i++) {
            var low = String(texts[i] || "").toLowerCase();
            for (var j = 0; j < disabledNewChatTooltips.length; j++) {
                if (low.indexOf(disabledNewChatTooltips[j]) !== -1) return true;
            }
        }
        return false;
    }

    function inspectWebAreaEmptyChat(webArea) {
        if (!webArea || !isWebAreaLoaded(webArea)) {
            return {empty: false, definiteNonEmpty: false};
        }
        var bestMatch = null;
        var hasChatTurns = false;
        var timedOut = false;
        var deadline = Math.min(Date.now() + 900, overallDeadline);

        function walk(node, depth) {
            if (hasChatTurns || timedOut) return;
            if (Date.now() > deadline) {
                timedOut = true;
                return;
            }
            if (depth > 64) return;
            var role = "";
            try { role = String(node.role() || ""); } catch (e) { return; }

            if (role === "AXButton") {
                var bSize = null;
                try { bSize = node.size(); } catch (eSz) {}
                if (!bSize || (bSize[0] > 0 && bSize[1] > 0)) {
                    var bTexts = collectTexts(node);
                    var bName = (bTexts[0] || "").trim().toLowerCase();
                    if (!bName && bTexts.length > 1) bName = bTexts[1].trim().toLowerCase();
                    if (bName) {
                        if (isAiStudio && newChatButtonNames.indexOf(bName) !== -1) {
                            if (!isAxButtonDisabled(node, bTexts)) { hasChatTurns = true; return; }
                        }
                        if (bName === "stop" || bName === "停止") { hasChatTurns = true; return; }
                        for (var k = 0; k < turnActionKeywords.length; k++) {
                            if (bName.indexOf(turnActionKeywords[k]) !== -1) { hasChatTurns = true; return; }
                        }
                    }
                }
            }

            if (role === "AXTextArea" || role === "AXTextField" || role === "AXGroup") {
                var isInput = (role === "AXTextArea" || role === "AXTextField");
                if (!isInput) {
                    try { if (node.attributes.byName("AXEditable").value()) isInput = true; } catch (e) {}
                }
                if (isInput) {
                    try {
                        var sz = null;
                        try { sz = node.size(); } catch (eSz) {}
                        if (node.enabled() && (!sz || (sz[0] > 0 && sz[1] > 0))) {
                            var sc = scoreTexts(collectTexts(node));
                            if (sc.score > 0 && (!bestMatch || sc.score > bestMatch.score)) {
                                bestMatch = {el: node, score: sc.score, label: sc.label};
                            }
                        }
                    } catch (e) {}
                }
            }

            var children = [];
            try { children = node.uiElements(); } catch (e) { return; }
            for (var i = 0; i < children.length; i++) {
                walk(children[i], depth + 1);
                if (hasChatTurns || timedOut) return;
            }
        }

        walk(webArea, 0);
        // 走査タイムアウト時は会話ありタブの誤判定を防ぐため必ずfalseとする
        if (timedOut || hasChatTurns || !bestMatch) {
            return {empty: false, definiteNonEmpty: hasChatTurns};
        }
        var inputEmpty = isInputElementEmpty(bestMatch.el, bestMatch.label);
        return {empty: inputEmpty, definiteNonEmpty: !inputEmpty};
    }

    function isWebAreaEmptyChat(webArea) {
        return inspectWebAreaEmptyChat(webArea).empty;
    }

    function waitAndVerifyTab(win, preTitle) {
        var pollDeadline = Math.min(Date.now() + 2000, overallDeadline);
        var mismatchSince = null;
        while (Date.now() < pollDeadline) {
            var postTitle = "";
            try { postTitle = String(win.name() || ""); } catch (e) {}
            if (loginRe && loginRe.test(postTitle)) return false;
            if (!isLoadingTitle(postTitle)) {
                if (!matchesAllowedTitle(postTitle)) {
                    var tmpArea = findWebArea(win, 0);
                    // 旧タブ残像(postTitle === preTitle)や再構築中(!tmpArea)で誤打ち切りしない
                    if (postTitle !== preTitle && tmpArea && isWebAreaLoaded(tmpArea)) {
                        if (mismatchSince === null) mismatchSince = Date.now();
                        else if ((Date.now() - mismatchSince) >= 600) return false;
                    } else {
                        mismatchSince = null;
                    }
                } else {
                    mismatchSince = null;
                    var postArea = findWebArea(win, 0);
                    var res = inspectWebAreaEmptyChat(postArea);
                    if (res.empty) return true;
                    if (res.definiteNonEmpty) return false;
                }
            }
            delay(0.08);
        }
        return false;
    }

    function isTabElementSelected(node) {
        try { if (node.value() === 1 || node.value() === true) return true; } catch (e) {}
        try { if (node.selected() === true) return true; } catch (e) {}
        try { if (node.attributes.byName("AXSelected").value() === true) return true; } catch (e) {}
        return false;
    }

    function collectTabs(node, depth, out) {
        if (depth > 10) return;
        var role = "";
        try { role = String(node.role() || ""); } catch (e) { return; }
        if (role === "AXWebArea") return;
        if (role === "AXRadioButton" || role === "AXTab") {
            var title = "";
            try { title = String(node.title() || node.description() || ""); } catch (e) {}
            if (title) {
                out.push({el: node, title: title, selected: isTabElementSelected(node)});
            }
            return;
        }
        var children = [];
        try { children = node.uiElements(); } catch (e) { return; }
        for (var i = 0; i < children.length; i++) {
            collectTabs(children[i], depth + 1, out);
        }
    }

    function findFallbackTabByWinTitle(tabs, winTitle) {
        var normWin = normalizeTabTitle(winTitle);
        if (!normWin) return null;
        for (var i = 0; i < tabs.length; i++) {
            var normTab = normalizeTabTitle(tabs[i].title);
            if (normTab && (normTab === normWin || normWin.indexOf(normTab) === 0)) {
                return tabs[i].el;
            }
        }
        return null;
    }

    function isWindowMinimized(win) {
        try { return win.attributes.byName("AXMinimized").value() === true; } catch (e) { return false; }
    }

    function setWindowMinimized(win, state) {
        try { win.attributes.byName("AXMinimized").value = state; } catch (e) {}
    }

    var se = Application("System Events");
    var origFrontApp = null;
    try {
        var frontProcs = se.applicationProcesses.whose({frontmost: true})();
        if (frontProcs && frontProcs.length > 0) origFrontApp = frontProcs[0];
    } catch (e) {}

    var procs = se.applicationProcesses.whose({bundleIdentifier: bundleId})();
    if (procs.length === 0) return JSON.stringify({found: false});
    var appProc = procs[0];
    try { appProc.attributes.byName("AXManualAccessibility").value = true; } catch (e) {}
    try { appProc.attributes.byName("AXEnhancedUserInterface").value = true; } catch (e) {}

    var wins = [];
    try { wins = appProc.windows(); } catch (e) { return JSON.stringify({found: false}); }
    var origBrowserFrontmost = false;
    try { origBrowserFrontmost = appProc.frontmost() === true; } catch (e) {}
    var origFrontWin = (!origBrowserFrontmost || wins.length === 0) ? null : wins[0];

    var orderedWins = [];
    for (var i = 0; i < wins.length; i++) {
        if (!isWindowMinimized(wins[i])) orderedWins.push({win: wins[i], minimized: false});
    }
    for (var j = 0; j < wins.length; j++) {
        if (isWindowMinimized(wins[j])) orderedWins.push({win: wins[j], minimized: true});
    }

    for (var w = 0; w < orderedWins.length; w++) {
        if (Date.now() >= overallDeadline) break;
        var entry = orderedWins[w];
        var win = entry.win;
        var winTitle = "";
        try { winTitle = String(win.name() || ""); } catch (e) {}

        if (!entry.minimized && matchesAllowedTitle(winTitle)) {
            var webArea = findWebArea(win, 0);
            if (isWebAreaEmptyChat(webArea)) {
                try { win.actions.byName("AXRaise").perform(); } catch (e) {}
                return JSON.stringify({found: true});
            }
        }

        var tabs = [];
        collectTabs(win, 0, tabs);
        var prevSelected = null;
        var candidates = [];
        for (var t = 0; t < tabs.length; t++) {
            if (tabs[t].selected && !prevSelected) prevSelected = tabs[t].el;
            if ((entry.minimized || !tabs[t].selected) && isEmptyTabTitle(tabs[t].title)) {
                candidates.push(tabs[t].el);
            }
        }
        if (!prevSelected && winTitle) {
            prevSelected = findFallbackTabByWinTitle(tabs, winTitle);
        }
        var activeTitleIsEmpty = entry.minimized && isEmptyTabTitle(winTitle);
        if (candidates.length === 0 && !activeTitleIsEmpty) continue;

        var verified = false;
        var unminimized = false;
        var tabSwitched = false;
        try {
            if (entry.minimized) {
                setWindowMinimized(win, false);
                unminimized = true;
                delay(0.15);
                if (activeTitleIsEmpty && waitAndVerifyTab(win, "")) {
                    try { win.actions.byName("AXRaise").perform(); } catch (e) {}
                    verified = true;
                    return JSON.stringify({found: true});
                }
            }

            for (var c = 0; c < candidates.length; c++) {
                if (Date.now() >= overallDeadline) break;
                try {
                    candidates[c].actions.byName("AXPress").perform();
                    tabSwitched = true;
                } catch (e) { continue; }
                if (waitAndVerifyTab(win, winTitle)) {
                    if (entry.minimized) setWindowMinimized(win, false);
                    try { win.actions.byName("AXRaise").perform(); } catch (e) {}
                    verified = true;
                    return JSON.stringify({found: true});
                }
            }
        } finally {
            if (!verified) {
                if (tabSwitched && prevSelected) {
                    try { prevSelected.actions.byName("AXPress").perform(); } catch (e) {}
                }
                if (unminimized) {
                    setWindowMinimized(win, true);
                    if (!origBrowserFrontmost && origFrontApp) {
                        try { origFrontApp.frontmost = true; } catch (e) {}
                    } else if (origBrowserFrontmost && origFrontWin) {
                        try { origFrontWin.actions.byName("AXRaise").perform(); } catch (e) {}
                    }
                }
            }
        }
    }
    return JSON.stringify({found: false});
}
"""


def focus_macos_empty_chat_tab(
    target_url: str,
    prompt_labels: Sequence[str],
    allowed_title_keywords: Sequence[str],
    is_aistudio: bool,
) -> Optional[str]:
    """起動済みmacOSブラウザから対象サイトの空チャットタブを探し、選択・最前面化してBundle IDを返す。"""
    empty_titles = get_empty_chat_titles_for_url(target_url)
    if not empty_titles:
        return None
    empty_service_names, empty_core_phrases = get_empty_chat_service_and_phrases(target_url)

    bundle_id = _get_macos_default_browser_bundle_id()
    resolved_id = _resolve_running_macos_bundle_id(bundle_id)
    if not resolved_id:
        return None

    payload = json.dumps(
        {
            "bundleId": resolved_id,
            "labels": list(prompt_labels),
            "titleKeywords": list(allowed_title_keywords),
            "emptyChatTitles": list(empty_titles),
            "emptyServiceNames": list(empty_service_names),
            "emptyCorePhrases": list(empty_core_phrases),
            "isAiStudio": bool(is_aistudio),
            "newChatButtonNames": list(NEW_CHAT_BUTTON_NAMES),
            "turnActionKeywords": list(CHAT_TURN_ACTION_BUTTON_KEYWORDS),
            "tabStatusSuffixRegex": TAB_STATUS_SUFFIX_REGEX_PATTERN,
            "loginRegex": LOGIN_TITLE_REGEX_PATTERN,
            "loadingRegex": LOADING_TITLE_REGEX_PATTERN,
            "browserSuffixRegex": BROWSER_TITLE_SUFFIX_REGEX_PATTERN,
            "standaloneProfileRegex": BROWSER_STANDALONE_PROFILE_REGEX_PATTERN,
            "trailingCustomProfileSuffixRegex": BROWSER_TRAILING_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
            "customProfileSuffixRegex": BROWSER_CUSTOM_PROFILE_SUFFIX_REGEX_PATTERN,
            "serviceTitleKeywordsRegex": SERVICE_TITLE_KEYWORDS_REGEX_PATTERN,
        }
    )

    try:
        # JXA側overallDeadline(7.0s)後のタブ選択・最小化/フォーカス復元を確実に完了させるため余裕を持たせる
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-", payload],
            input=_MACOS_FIND_AND_ACTIVATE_EMPTY_TAB_JXA,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if res.returncode != 0:
            return None
        parsed = json.loads((res.stdout or "").strip() or "{}")
        if not parsed.get("found"):
            return None
        act = subprocess.run(["open", "-b", resolved_id], capture_output=True, text=True, timeout=3)
        return resolved_id if act.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
