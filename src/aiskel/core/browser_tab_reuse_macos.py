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
    get_empty_chat_titles_for_url,
)

_MACOS_FIND_AND_ACTIVATE_EMPTY_TAB_JXA = r"""
function run(argv) {
    var payload = JSON.parse(argv[0]);
    var bundleId = payload.bundleId;
    var labels = payload.labels;
    var titleKeywords = payload.titleKeywords;
    var emptyChatTitles = payload.emptyChatTitles;
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
    var tabStatusSuffixRe = /\s+-\s+(?:audio\s+playing|muted|inactive|pinned|memory\s+usage:.*|音声を再生中|ミュート中|非アクティブ|固定済み|メモリ使用量:.*)$/i;
    var overallDeadline = Date.now() + 4500;

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

    function isEmptyTabTitle(rawTitle) {
        var core = stripBrowserSuffix(rawTitle).core.replace(tabStatusSuffixRe, "").trim().toLowerCase();
        return emptyChatTitles.indexOf(core) !== -1;
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

    function collectTexts(el) {
        var out = [];
        var attrs = ["AXDescription", "AXTitle", "AXHelp", "AXPlaceholderValue"];
        for (var i = 0; i < attrs.length; i++) {
            try {
                var v = el.attributes.byName(attrs[i]).value();
                if (v) out.push(String(v));
            } catch (e) {}
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

    function isPlaceholderText(txtLower, matchedLabel) {
        if (!txtLower || txtLower === matchedLabel) return true;
        for (var i = 0; i < labels.length; i++) {
            var ll = labels[i].trim().toLowerCase();
            if (!ll) continue;
            if (txtLower === ll || ll.indexOf(txtLower) !== -1 || txtLower.indexOf(ll) !== -1) {
                return true;
            }
        }
        return false;
    }

    function hasNonEmptyChildText(node, matchedLabel, depth, state) {
        if (depth > 4 || state.visited >= 30) return false;
        state.visited += 1;
        var role = "";
        try { role = String(node.role() || ""); } catch (e) { return false; }
        if (depth > 0 && (role === "AXStaticText" || role === "AXTextArea" || role === "AXTextField")) {
            var txt = "";
            try { txt = String(node.value() || node.title() || "").trim(); } catch (e) {}
            if (txt && !isPlaceholderText(txt.toLowerCase(), matchedLabel)) {
                return true;
            }
        }
        var children = [];
        try { children = node.uiElements(); } catch (e) { return false; }
        for (var i = 0; i < children.length; i++) {
            if (hasNonEmptyChildText(children[i], matchedLabel, depth + 1, state)) return true;
        }
        return false;
    }

    function isInputElementEmpty(el, matchedLabel) {
        try {
            var curVal = String(el.value() || "").trim();
            if (curVal && curVal.toLowerCase() !== matchedLabel) return false;
        } catch (e) {}
        return !hasNonEmptyChildText(el, matchedLabel, 0, {visited: 0});
    }

    function isWebAreaEmptyChat(webArea) {
        if (!webArea || !isWebAreaLoaded(webArea)) return false;
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
                var bTexts = collectTexts(node);
                var bName = (bTexts[0] || "").trim().toLowerCase();
                if (!bName && bTexts.length > 1) bName = bTexts[1].trim().toLowerCase();
                if (bName) {
                    if (isAiStudio && newChatButtonNames.indexOf(bName) !== -1) {
                        var isEnabled = true;
                        try { isEnabled = node.enabled() !== false; } catch (e) {}
                        if (isEnabled) { hasChatTurns = true; return; }
                    }
                    if (bName === "stop" || bName === "停止") { hasChatTurns = true; return; }
                    for (var k = 0; k < turnActionKeywords.length; k++) {
                        if (bName.indexOf(turnActionKeywords[k]) !== -1) { hasChatTurns = true; return; }
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
        if (timedOut || hasChatTurns || !bestMatch) return false;
        return isInputElementEmpty(bestMatch.el, bestMatch.label);
    }

    function waitAndVerifyTab(win) {
        var pollDeadline = Math.min(Date.now() + 700, overallDeadline);
        while (Date.now() < pollDeadline) {
            var postTitle = "";
            try { postTitle = String(win.name() || ""); } catch (e) {}
            if (loginRe && loginRe.test(postTitle)) return false;
            if (matchesAllowedTitle(postTitle)) {
                var postArea = findWebArea(win, 0);
                if (isWebAreaEmptyChat(postArea)) return true;
            }
            delay(0.08);
        }
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
                var selected = false;
                try { selected = (node.value() === 1 || node.selected() === true); } catch (e) {}
                out.push({el: node, title: title, selected: selected});
            }
            return;
        }
        var children = [];
        try { children = node.uiElements(); } catch (e) { return; }
        for (var i = 0; i < children.length; i++) {
            collectTabs(children[i], depth + 1, out);
        }
    }

    function isWindowMinimized(win) {
        try { return win.attributes.byName("AXMinimized").value() === true; } catch (e) { return false; }
    }

    function setWindowMinimized(win, state) {
        try { win.attributes.byName("AXMinimized").value = state; } catch (e) {}
    }

    var se = Application("System Events");
    var procs = se.applicationProcesses.whose({bundleIdentifier: bundleId})();
    if (procs.length === 0) return JSON.stringify({found: false});
    var appProc = procs[0];
    try { appProc.attributes.byName("AXManualAccessibility").value = true; } catch (e) {}
    try { appProc.attributes.byName("AXEnhancedUserInterface").value = true; } catch (e) {}

    var wins = [];
    try { wins = appProc.windows(); } catch (e) { return JSON.stringify({found: false}); }

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
            if (tabs[t].selected) prevSelected = tabs[t].el;
            if ((entry.minimized || !tabs[t].selected) && isEmptyTabTitle(tabs[t].title)) {
                candidates.push(tabs[t].el);
            }
        }
        var activeTitleIsEmpty = entry.minimized && isEmptyTabTitle(winTitle);
        if (candidates.length === 0 && !activeTitleIsEmpty) continue;

        if (entry.minimized) {
            setWindowMinimized(win, false);
            delay(0.15);
            if (activeTitleIsEmpty && waitAndVerifyTab(win)) {
                try { win.actions.byName("AXRaise").perform(); } catch (e) {}
                return JSON.stringify({found: true});
            }
        }

        for (var c = 0; c < candidates.length; c++) {
            if (Date.now() >= overallDeadline) break;
            try {
                candidates[c].actions.byName("AXPress").perform();
            } catch (e) { continue; }
            if (waitAndVerifyTab(win)) {
                if (entry.minimized) setWindowMinimized(win, false);
                try { win.actions.byName("AXRaise").perform(); } catch (e) {}
                return JSON.stringify({found: true});
            }
        }

        if (prevSelected) {
            try { prevSelected.actions.byName("AXPress").perform(); } catch (e) {}
        }
        if (entry.minimized) {
            setWindowMinimized(win, true);
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
            "isAiStudio": bool(is_aistudio),
            "newChatButtonNames": list(NEW_CHAT_BUTTON_NAMES),
            "turnActionKeywords": list(CHAT_TURN_ACTION_BUTTON_KEYWORDS),
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
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-", payload],
            input=_MACOS_FIND_AND_ACTIVATE_EMPTY_TAB_JXA,
            capture_output=True,
            text=True,
            timeout=8,
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
