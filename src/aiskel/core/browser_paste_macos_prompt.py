"""Module: @role: macOS JXA向けのAXWebArea内プロンプト入力欄属性収集・スコアリング・空欄判定ヘルパーJSを生成する。"""

_MACOS_JXA_PROMPT_HELPERS = r"""
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
"""

def build_macos_jxa_prompt_helpers() -> str:
    """macOS JXA用のプロンプト入力欄属性収集・スコアリング・空欄判定ヘルパー関数群を返す。"""
    return _MACOS_JXA_PROMPT_HELPERS.strip("\n")
