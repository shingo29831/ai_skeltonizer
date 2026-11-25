"""Module: @role: macOS JXA向けのブラウザタブタイトル正規化・読込中判定・空チャット一致判定ヘルパーJSを生成する。"""

def build_macos_tab_title_helpers_js() -> str:
    """macOS JXA用のタブタイトル正規化・読込中判定・空チャット一致判定ヘルパー関数群を返す。"""
    return r"""    function stripBrowserSuffix(rawTitle) {
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
    }"""
