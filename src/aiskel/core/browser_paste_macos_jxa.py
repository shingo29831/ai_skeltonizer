"""Module: @role: macOSブラウザ自動貼り付けおよびCmd+Enter送信を実行するJXAスクリプト群を組み立てる。"""
from .browser_paste_macos_prompt import build_macos_jxa_prompt_helpers
from .browser_paste_macos_window import build_macos_jxa_window_helpers

_MACOS_FOCUS_AND_PASTE_TEMPLATE = r"""
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

__WINDOW_HELPERS__

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

__PROMPT_HELPERS__

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

_MACOS_TRIGGER_RUN_TEMPLATE = r"""
function run(argv) {
    var payload = JSON.parse(argv[0]);
    var bundleId = payload.bundleId;
    var titleKeywords = payload.titleKeywords || [];
    var loginRe = payload.loginRegex ? new RegExp(payload.loginRegex, "i") : null;
    var loadingRe = payload.loadingRegex ? new RegExp(payload.loadingRegex, "i") : null;
    var se = Application("System Events");
    var procs = se.applicationProcesses.whose({bundleIdentifier: bundleId})();
    if (procs.length === 0) {
        return JSON.stringify({ok: false, retryable: false, errorKind: "failed", reason: "対象ブラウザプロセスが見つかりません"});
    }
    var appProc = procs[0];
    if (!appProc.frontmost()) {
        return JSON.stringify({ok: false, retryable: false, errorKind: "user_aborted", reason: "対象ブラウザが最前面にありません"});
    }
    var wins = appProc.windows();
    if (wins.length === 0) {
        return JSON.stringify({ok: false, retryable: false, errorKind: "failed", reason: "ブラウザウィンドウが存在しません"});
    }
    var winTitle = "";
    try { winTitle = String(wins[0].name() || ""); } catch (e) {}
    if (winTitle && loginRe && loginRe.test(winTitle)) {
        return JSON.stringify({ok: false, retryable: false, errorKind: "login_required", reason: "認証・ログイン画面のため自動送信を中断しました (title: " + winTitle + ")"});
    }
    if (loadingRe && loadingRe.test(winTitle.trim())) {
        return JSON.stringify({ok: false, retryable: true, errorKind: "transient", reason: "サイトを読み込み中のため自動送信をスキップしました (title: " + winTitle + ")"});
    }
    if (titleKeywords.length > 0) {
        var titleLower = winTitle.toLowerCase();
        var allowed = titleKeywords.some(function(kw) {
            var k = String(kw || "").trim().toLowerCase();
            return k && titleLower.indexOf(k) !== -1;
        });
        if (!allowed) {
            return JSON.stringify({ok: false, retryable: false, errorKind: "user_aborted", reason: "許可対象外のページのため自動送信をスキップしました (title: " + winTitle + ")"});
        }
    }
    // アドレスバー等へのCmd+Enter誤爆を防ぐためWebコンテンツ領域内の入力フォーカスを確認する
    try { appProc.attributes.byName("AXManualAccessibility").value = true; } catch (e) {}
    var focusedEl = null;
    try { focusedEl = appProc.attributes.byName("AXFocusedUIElement").value(); } catch (e) {}
    if (!focusedEl) {
        return JSON.stringify({ok: false, retryable: false, errorKind: "failed", reason: "フォーカス中の入力要素を検出できませんでした"});
    }
    var inWebArea = false;
    var cur = focusedEl;
    for (var step = 0; step < 48; step++) {
        var role = "";
        try { role = String(cur.role() || ""); } catch (e) { break; }
        if (role === "AXWebArea") {
            inWebArea = true;
            break;
        }
        if (role === "AXWindow" || role === "AXApplication" || role === "AXToolbar") break;
        try { cur = cur.attributes.byName("AXParent").value(); } catch (e) { cur = null; }
        if (!cur) break;
    }
    if (!inWebArea) {
        return JSON.stringify({ok: false, retryable: false, errorKind: "failed", reason: "Webコンテンツ領域外にフォーカスがあるため自動送信を中止しました"});
    }
    se.keyCode(36, {using: "command down"});
    return JSON.stringify({ok: true, retryable: false, errorKind: "none", reason: ""});
}
"""

def build_macos_focus_and_paste_jxa() -> str:
    """macOSブラウザのプロンプトフォーカス・Cmd+V貼り付け用JXAスクリプトを組み立てる。"""
    return (
        _MACOS_FOCUS_AND_PASTE_TEMPLATE
        .replace("__WINDOW_HELPERS__", build_macos_jxa_window_helpers())
        .replace("__PROMPT_HELPERS__", build_macos_jxa_prompt_helpers())
    )

def build_macos_trigger_run_jxa() -> str:
    """macOSブラウザのWeb領域内Cmd+Enter送信確認用JXAスクリプトを返す。"""
    return _MACOS_TRIGGER_RUN_TEMPLATE
