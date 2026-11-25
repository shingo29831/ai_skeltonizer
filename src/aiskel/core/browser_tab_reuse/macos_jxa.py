"""Module: @role: macOSブラウザの既存空チャットタブ探索・選択・復元を実行するJXAスクリプトを組み立てる。"""
from .macos_title import build_macos_tab_title_helpers_js
from .macos_webarea import build_macos_webarea_helpers_js

_JXA_HEADER = r"""function run(argv) {
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
    var overallDeadline = Date.now() + 7000;"""

_JXA_MAIN = r"""    function waitAndVerifyTab(win, preTitle) {
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
}"""

def build_macos_find_and_activate_empty_tab_jxa() -> str:
    """macOSブラウザの空チャットタブ探索・選択・復元用JXAスクリプトを組み立てる。"""
    return (
        f"{_JXA_HEADER}\n"
        f"{build_macos_tab_title_helpers_js()}\n"
        f"{build_macos_webarea_helpers_js()}\n"
        f"{_JXA_MAIN}"
    )
