"""Module: @role: macOS JXA向けのAXWebArea走査・プロンプト入力欄空欄判定・空チャット検査ヘルパーJSを生成する。"""

def build_macos_webarea_helpers_js() -> str:
    """macOS JXA用のAXWebArea走査・プロンプト空欄判定・空チャット検査ヘルパー関数群を返す。"""
    return r"""    function findWebArea(node, depth) {
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
    }"""
