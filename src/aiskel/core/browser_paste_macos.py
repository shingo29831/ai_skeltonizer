"""Module: @role: macOS環境においてJXA経由でブラウザ内のAXWebArea配下を走査し、プロンプト入力欄へのフォーカスとCmd+V貼り付けを実行する。"""
import json
import subprocess
from typing import Optional, Sequence, Tuple

from .browser_focus_macos import _get_macos_default_browser_bundle_id, _resolve_running_macos_bundle_id

_MACOS_FOCUS_AND_PASTE_JXA = r"""
function run(argv) {
    var payload = JSON.parse(argv[0]);
    var bundleId = payload.bundleId;
    var labels = payload.labels;
    var titleKeywords = payload.titleKeywords;
    var allowNonEmpty = payload.allowNonEmpty;

    var se = Application("System Events");
    var procs = se.applicationProcesses.whose({bundleIdentifier: bundleId});
    if (procs.length === 0) {
        return JSON.stringify({ok: false, reason: "対象ブラウザプロセスが見つかりません"});
    }
    var appProc = procs[0];

    var deadline = Date.now() + 1000;
    while (!appProc.frontmost() && Date.now() < deadline) {
        delay(0.05);
    }
    if (!appProc.frontmost()) {
        return JSON.stringify({ok: false, reason: "対象ブラウザが最前面になりませんでした"});
    }

    // Chromium/Safari/Firefoxのアクセシビリティツリー公開を有効化する
    try { appProc.attributes.byName("AXManualAccessibility").value = true; } catch (e) {}
    try { appProc.attributes.byName("AXEnhancedUserInterface").value = true; } catch (e) {}

    var wins = appProc.windows;
    if (wins.length === 0) {
        return JSON.stringify({ok: false, reason: "ブラウザウィンドウが存在しません"});
    }
    var frontWin = wins[0];
    var winTitle = "";
    try { winTitle = String(frontWin.name() || ""); } catch (e) {}
    if (titleKeywords.length > 0) {
        var titleLower = winTitle.toLowerCase();
        var allowed = titleKeywords.some(function(kw) { return titleLower.indexOf(kw) !== -1; });
        if (!allowed) {
            return JSON.stringify({ok: false, reason: "許可対象外のページのため自動貼り付けをスキップしました (title: " + winTitle + ")"});
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
    var waitWebDeadline = Date.now() + 1200;
    while (!webArea && Date.now() < waitWebDeadline) {
        webArea = findWebArea(frontWin, 0);
        if (!webArea) delay(0.08);
    }
    if (!webArea) {
        return JSON.stringify({ok: false, reason: "Webコンテンツ領域(AXWebArea)が検出できませんでした"});
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
        if (depth > 48 || (Date.now() - startTime) > 1500) return;
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
                    if (node.enabled()) {
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
        return JSON.stringify({ok: false, reason: "ページ内に対象のプロンプト入力欄が見つかりませんでした"});
    }

    var promptEl = bestMatch.el;
    if (!allowNonEmpty) {
        try {
            var curVal = String(promptEl.value() || "").trim();
            if (curVal && curVal.toLowerCase() !== bestMatch.label) {
                return JSON.stringify({ok: false, reason: "入力欄に既存テキストが存在するため自動貼り付けをスキップしました"});
            }
        } catch (e) {}
    }

    try {
        promptEl.focused = true;
    } catch (e) {
        try {
            promptEl.actions.byName("AXPress").perform();
        } catch (e2) {
            return JSON.stringify({ok: false, reason: "プロンプト入力欄へのフォーカス設定に失敗しました"});
        }
    }

    delay(0.05);
    if (!appProc.frontmost()) {
        return JSON.stringify({ok: false, reason: "貼り付け直前に別アプリへ切り替わったため中断しました"});
    }
    se.keystroke("v", {using: "command down"});
    return JSON.stringify({ok: true, reason: ""});
}
"""


def paste_macos_browser_prompt(
    prompt_labels: Sequence[str],
    target_bundle_id: Optional[str] = None,
    allowed_title_keywords: Sequence[str] = (),
    allow_non_empty: bool = False,
) -> Tuple[bool, str]:
    """最前面のmacOSデフォルトブラウザでプロンプト入力欄を探してフォーカスし、Cmd+Vを送信する。"""
    if not prompt_labels:
        return False, "プロンプトラベルが指定されていません"

    resolved_id = target_bundle_id
    if not resolved_id:
        bundle_id = _get_macos_default_browser_bundle_id()
        resolved_id = _resolve_running_macos_bundle_id(bundle_id)
    if not resolved_id:
        return False, "起動中のデフォルトブラウザが特定できませんでした"

    payload = json.dumps(
        {
            "bundleId": resolved_id,
            "labels": list(prompt_labels),
            "titleKeywords": list(allowed_title_keywords),
            "allowNonEmpty": allow_non_empty,
        }
    )

    try:
        res = subprocess.run(
            ["osascript", "-l", "JavaScript", "-", payload],
            input=_MACOS_FOCUS_AND_PASTE_JXA,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            err_msg = (res.stderr or "").strip()
            if "assistive" in err_msg.lower() or "-25211" in err_msg or "-1719" in err_msg:
                return False, "macOSのアクセシビリティ権限が許可されていません"
            return False, f"osascript実行に失敗しました: {err_msg or f'code={res.returncode}'}"

        out = (res.stdout or "").strip()
        if not out:
            return False, "osascriptから応答がありませんでした"
        parsed = json.loads(out)
        return bool(parsed.get("ok")), str(parsed.get("reason", ""))
    except subprocess.TimeoutExpired:
        return False, "ブラウザUI探索がタイムアウトしました"
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return False, f"macOSブラウザ操作エラー: {e}"
