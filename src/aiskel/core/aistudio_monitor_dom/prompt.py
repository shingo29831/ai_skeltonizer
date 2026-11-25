"""Module: @role: Google AI Studioのプロンプト入力欄・添付チップ解析・コンポーザ解決・貼付操作のJSヘルパー群を生成する。"""

def build_monitor_dom_prompt_js() -> str:
    """プロンプト入力欄・添付チップ解析・コンポーザ解決・自動貼付を行うJSヘルパー関数群を返す。"""
    return (
        "  function getEditableInputText(el) {\n"
        "    if (!el || el.nodeType !== 1) return '';\n"
        "    const tag = (el.tagName || '').toUpperCase();\n"
        "    if (tag === 'TEXTAREA' || tag === 'INPUT') {\n"
        "      return el.value || '';\n"
        "    }\n"
        "    // contenteditableはtextContentだと<br>/<div>改行が脱落するためinnerTextを優先\n"
        "    if (typeof el.innerText === 'string') {\n"
        "      return el.innerText || el.textContent || '';\n"
        "    }\n"
        "    return el.textContent || '';\n"
        "  }\n\n"
        "  function getPromptAttachmentChips(promptBox) {\n"
        "    if (!promptBox) return [];\n"
        "    const STRICT_CHUNK_SELECTOR = [\n"
        "      'ms-file-chunk',\n"
        "      'ms-image-chunk',\n"
        "      'ms-audio-chunk',\n"
        "      'ms-video-chunk',\n"
        "      'ms-drive-chunk',\n"
        "      'ms-youtube-chunk',\n"
        "      'ms-webpage-chunk',\n"
        "      'ms-code-chunk',\n"
        "      'ms-pasted-text-chunk',\n"
        "      'ms-chunk-attachment',\n"
        "      'ms-attachment',\n"
        "      'ms-paste-chip',\n"
        "      'ms-file-chip',\n"
        "    ].join(', ');\n"
        "    const CLASS_CHUNK_SELECTOR = [\n"
        "      'ms-chunk-preview',\n"
        "      'ms-prompt-media',\n"
        "      '.file-chunk-container',\n"
        "      '[data-test-id=\"prompt-media-container\"]',\n"
        "      '.prompt-media-item-container',\n"
        "      '[class*=\"file-chunk\"]',\n"
        "      '[class*=\"chunk-chip\"]',\n"
        "      '[class*=\"attachment-chip\"]',\n"
        "      '[class*=\"media-chip\"]',\n"
        "      '[class*=\"paste-chip\"]',\n"
        "    ].join(', ');\n"
        "    const GENERIC_CHIP_SELECTOR = [\n"
        "      'ms-prompt-chip',\n"
        "      'mat-chip',\n"
        "      'mat-chip-row',\n"
        "      '[class*=\"file-chip\"]',\n"
        "      '[class*=\"prompt-chip\"]',\n"
        "    ].join(', ');\n"
        "    const DEDICATED_CHUNK_SELECTOR = STRICT_CHUNK_SELECTOR + ', ' + CLASS_CHUNK_SELECTOR;\n"
        "    const chipNodes = promptBox.querySelectorAll(DEDICATED_CHUNK_SELECTOR + ', ' + GENERIC_CHIP_SELECTOR);\n"
        "    const chips = [];\n"
        "    const acceptedNodes = [];\n"
        "    for (const chip of chipNodes) {\n"
        "      if (!isElementVisible(chip)) continue;\n"
        "      if (chip.closest('ms-run-button, ms-add-media-button, mat-menu, ms-model-selector, mat-select, ms-chat-turn, ms-system-instructions, [data-aiskel-ui=\"true\"]')) continue;\n"
        "      const isStrictChunk = chip.matches(STRICT_CHUNK_SELECTOR);\n"
        "      const isDedicatedChunk = isStrictChunk || chip.matches(CLASS_CHUNK_SELECTOR);\n"
        "      if (!isDedicatedChunk && !chip.closest('ms-prompt-box, ms-prompt-input-wrapper, footer')) continue;\n"
        "      const chipClass = typeof chip.className === 'string' ? chip.className : '';\n"
        "      const chipTestId = chip.getAttribute('data-test-id') || '';\n"
        "      const isContainerClass = /(?:^|[\\s_-])(?:container|list|wrapper|group)s?(?:$|[\\s_-])/i.test(chipClass + ' ' + chipTestId);\n"
        "      // 複数チャンクを束ねる親ラッパーとメディア親要素を除外し個別要素を数える\n"
        "      if (\n"
        "        !isStrictChunk &&\n"
        "        (chip.querySelector(STRICT_CHUNK_SELECTOR) ||\n"
        "          ((isContainerClass || chip.matches('ms-prompt-media')) &&\n"
        "            chip.querySelector(DEDICATED_CHUNK_SELECTOR + ', ' + GENERIC_CHIP_SELECTOR)))\n"
        "      ) {\n"
        "        continue;\n"
        "      }\n"
        "      if (isDedicatedChunk) {\n"
        "        if (chip.closest('button')) continue;\n"
        "      } else if (chip.closest('button, [role=\"button\"]')) {\n"
        "        continue;\n"
        "      }\n"
        "      if (acceptedNodes.some((acc) => acc.contains(chip))) continue;\n"
        "      const clone = chip.cloneNode(true);\n"
        "      if (clone && typeof clone.querySelectorAll === 'function') {\n"
        "        const removeSelector = isDedicatedChunk\n"
        "          ? 'mat-icon, .material-symbols-outlined, svg, mat-spinner, mat-progress-spinner, mat-progress-bar, [role=\"progressbar\"], button[aria-label*=\"remove\" i], button[aria-label*=\"delete\" i], button[aria-label*=\"close\" i], button[aria-label*=\"dismiss\" i], button[aria-label*=\"clear\" i], button[aria-label*=\"削除\"], button[aria-label*=\"閉じる\"], [role=\"button\"][aria-label*=\"remove\" i], [role=\"button\"][aria-label*=\"delete\" i], [role=\"button\"][aria-label*=\"close\" i], [role=\"button\"][aria-label*=\"dismiss\" i], [role=\"button\"][aria-label*=\"clear\" i], [role=\"button\"][aria-label*=\"削除\"], [role=\"button\"][aria-label*=\"閉じる\"]'\n"
        "          : 'button, [role=\"button\"], mat-icon, .material-symbols-outlined, svg, mat-spinner, mat-progress-spinner, mat-progress-bar, [role=\"progressbar\"]';\n"
        "        const uiNodes = clone.querySelectorAll(removeSelector);\n"
        "        for (const uiNode of uiNodes) {\n"
        "          uiNode.remove();\n"
        "        }\n"
        "      }\n"
        "      let label = ((clone && clone.textContent) || '').trim();\n"
        "      if (!label && (isStrictChunk || (isDedicatedChunk && !isContainerClass))) {\n"
        "        label = (chip.getAttribute('aria-label') || chip.getAttribute('title') || '').trim();\n"
        "      }\n"
        "      if (label) {\n"
        "        acceptedNodes.push(chip);\n"
        "        chips.push(label);\n"
        "      } else if (chip.querySelector('img, video, audio, canvas')) {\n"
        "        acceptedNodes.push(chip);\n"
        "        chips.push('[media]');\n"
        "      } else if (isStrictChunk) {\n"
        "        acceptedNodes.push(chip);\n"
        "        chips.push('[attachment]');\n"
        "      }\n"
        "    }\n"
        "    return chips;\n"
        "  }\n\n"
        "  function isPlaceholderOnlyInputText(el, rawText) {\n"
        "    const trimmed = String(rawText || '').trim();\n"
        "    if (!trimmed) return true;\n"
        "    if (!el || typeof el.getAttribute !== 'function') return false;\n"
        "    const placeholders = [\n"
        "      el.getAttribute('placeholder') || '',\n"
        "      el.getAttribute('data-placeholder') || '',\n"
        "      el.getAttribute('aria-placeholder') || '',\n"
        "    ];\n"
        "    return placeholders.some((ph) => {\n"
        "      const clean = ph.trim();\n"
        "      return Boolean(clean && trimmed === clean);\n"
        "    });\n"
        "  }\n\n"
        "  function isPromptInputEmpty(el) {\n"
        "    if (!el || !el.isConnected) return false;\n"
        "    const existing = getEditableInputText(el);\n"
        "    if (!isPlaceholderOnlyInputText(el, existing)) {\n"
        "      return false;\n"
        "    }\n"
        "    const promptBox = findOutermostPromptComposer(el);\n"
        "    if (promptBox && getPromptAttachmentChips(promptBox).length > 0) {\n"
        "      return false;\n"
        "    }\n"
        "    return true;\n"
        "  }\n\n"
        "  function findPromptRunButton() {\n"
        "    const selectors = [\n"
        "      'ms-prompt-renderer ms-chunk-editor footer ms-prompt-box ms-run-button button, ms-prompt-renderer ms-chunk-editor footer ms-prompt-box ms-run-button [role=\"button\"]',\n"
        "      'ms-prompt-renderer ms-chunk-editor footer ms-run-button button, ms-prompt-renderer ms-chunk-editor footer ms-run-button [role=\"button\"]',\n"
        "      'ms-prompt-renderer ms-prompt-box ms-run-button button, ms-prompt-renderer ms-prompt-box ms-run-button [role=\"button\"]',\n"
        "      'ms-prompt-input-wrapper ms-run-button button, ms-prompt-input-wrapper ms-run-button [role=\"button\"]',\n"
        "      'ms-prompt-box ms-run-button button, ms-prompt-box ms-run-button [role=\"button\"]',\n"
        "      'ms-prompt-renderer ms-run-button button, ms-chunk-editor ms-run-button button, ms-run-button button, ms-run-button [role=\"button\"]',\n"
        "      'ms-prompt-box button[type=\"submit\"], ms-prompt-input-wrapper button[type=\"submit\"], ms-chunk-editor button[type=\"submit\"]',\n"
        "    ];\n"
        "    for (const sel of selectors) {\n"
        "      const buttons = document.querySelectorAll(sel);\n"
        "      for (const btn of buttons) {\n"
        "        if (!btn.closest('ms-chat-turn, ms-system-instructions, [data-aiskel-ui=\"true\"]') && isElementVisible(btn)) {\n"
        "          return btn;\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "    return null;\n"
        "  }\n\n"
        "  function findOutermostPromptComposer(el) {\n"
        "    if (!el || typeof el.closest !== 'function') return null;\n"
        "    // footerやラッパー兄弟の添付を取りこぼさないよう最外コンポーザを取得\n"
        "    let best = null;\n"
        "    let cur = el;\n"
        "    while (cur && cur.nodeType === 1) {\n"
        "      if (cur.matches && cur.matches('ms-chat-turn, ms-system-instructions')) break;\n"
        "      if (\n"
        "        cur.matches &&\n"
        "        cur.matches('footer, ms-prompt-input-wrapper, ms-prompt-box') &&\n"
        "        isElementVisible(cur)\n"
        "      ) {\n"
        "        best = cur;\n"
        "      }\n"
        "      cur = cur.parentElement;\n"
        "    }\n"
        "    if (best) return best;\n"
        "    const fallback = el.closest('form, ms-chunk-editor, ms-prompt-renderer');\n"
        "    if (\n"
        "      fallback &&\n"
        "      !fallback.closest('ms-chat-turn, ms-system-instructions') &&\n"
        "      isElementVisible(fallback)\n"
        "    ) {\n"
        "      return fallback;\n"
        "    }\n"
        "    return null;\n"
        "  }\n\n"
        "  function collectPromptContainers(runBtn) {\n"
        "    const containers = [];\n"
        "    const addContainer = (node) => {\n"
        "      if (!node || node.nodeType !== 1 || !isElementVisible(node)) return;\n"
        "      if (!containers.includes(node)) {\n"
        "        containers.push(node);\n"
        "      }\n"
        "    };\n"
        "    const fromRun = findOutermostPromptComposer(runBtn);\n"
        "    addContainer(fromRun);\n"
        "    const inputEl = findPromptInputElement();\n"
        "    if (inputEl) {\n"
        "      const fromInput = findOutermostPromptComposer(inputEl);\n"
        "      addContainer(fromInput || inputEl.parentElement || inputEl);\n"
        "    }\n"
        "    // Runボタンと入力欄が別コンテナへ分離したUIでも両方を検査できるよう上位コンポーザを補完\n"
        "    const anchor = runBtn || inputEl;\n"
        "    if (anchor && typeof anchor.closest === 'function') {\n"
        "      const sharedComposer = anchor.closest('form, ms-chunk-editor, ms-prompt-renderer');\n"
        "      if (sharedComposer && !sharedComposer.closest('ms-chat-turn, ms-system-instructions')) {\n"
        "        addContainer(sharedComposer);\n"
        "      }\n"
        "    }\n"
        "    return { containers, inputEl };\n"
        "  }\n\n"
        "  function resolvePromptContainer(runBtn) {\n"
        "    const { containers } = collectPromptContainers(runBtn);\n"
        "    return containers.length ? containers[0] : null;\n"
        "  }\n\n"
        "  function isPromptAttachmentLoading(promptBox) {\n"
        "    if (!promptBox) return false;\n"
        "    const containers = promptBox.querySelectorAll(\n"
        "      '.composer-attachments-shelf, .multi-media-row, ms-prompt-media, [data-test-id=\"prompt-media-container\"], .prompt-media-item-container'\n"
        "    );\n"
        "    for (const container of containers) {\n"
        "      // 過去ターン等のスピナー誤検知を防ぐためコンポーザ外領域を除外\n"
        "      if (container.closest('ms-chat-turn, ms-system-instructions, [data-aiskel-ui=\"true\"]')) continue;\n"
        "      if (!isElementVisible(container)) continue;\n"
        "      const spinners = container.querySelectorAll('mat-spinner, mat-progress-spinner, mat-progress-bar, [role=\"progressbar\"]');\n"
        "      for (const sp of spinners) {\n"
        "        if (isElementVisible(sp)) return true;\n"
        "      }\n"
        "    }\n"
        "    return false;\n"
        "  }\n\n"
        "  function isComposerInputCandidate(el) {\n"
        "    if (!el || el.nodeType !== 1) return false;\n"
        "    if (el.closest('ms-chat-turn, ms-system-instructions, ms-run-settings, ms-prompt-options, ms-model-selector, ms-header, ms-toolbar, header, nav, aside, [role=\"dialog\"], [role=\"toolbar\"], [data-aiskel-ui=\"true\"]')) {\n"
        "      return false;\n"
        "    }\n"
        "    return isElementVisible(el) && isEditableInputElement(el);\n"
        "  }\n\n"
        "  function collectPromptInputElements(containers, directInputEl) {\n"
        "    const rawCandidates = [];\n"
        "    const addCandidate = (el) => {\n"
        "      if (isComposerInputCandidate(el) && !rawCandidates.includes(el)) {\n"
        "        rawCandidates.push(el);\n"
        "      }\n"
        "    };\n"
        "    addCandidate(directInputEl);\n"
        "    // OSペースト先のactiveElementが別ラッパー下にあってもテキストを捕捉する\n"
        "    if (document.activeElement && document.activeElement.nodeType === 1) {\n"
        "      addCandidate(document.activeElement);\n"
        "    }\n"
        "    for (const box of containers) {\n"
        "      const found = box.querySelectorAll('textarea, input, [contenteditable], [role=\"textbox\"]');\n"
        "      for (const el of found) {\n"
        "        addCandidate(el);\n"
        "      }\n"
        "    }\n"
        "    // 親ラッパーと内側入力要素の二重カウントを防ぐため最内側の要素のみ残す\n"
        "    return rawCandidates.filter((el) => !rawCandidates.some((other) => other !== el && el.contains(other)));\n"
        "  }\n\n"
        "  function collectMergedAttachmentChips(containers) {\n"
        "    if (!containers.length) return [];\n"
        "    // 親子関係にあるコンテナ間で添付チップが重複計上されるのを防ぐ\n"
        "    const outerOnly = containers.filter((box) => !containers.some((other) => other !== box && other.contains(box)));\n"
        "    const targetContainers = outerOnly.length ? outerOnly : containers;\n"
        "    const chips = [];\n"
        "    for (const box of targetContainers) {\n"
        "      for (const label of getPromptAttachmentChips(box)) {\n"
        "        chips.push(label);\n"
        "      }\n"
        "    }\n"
        "    return chips;\n"
        "  }\n\n"
        "  function getPromptInputSnapshot(runBtn) {\n"
        "    const { containers, inputEl } = collectPromptContainers(runBtn);\n"
        "    if (!containers.length && !isComposerInputCandidate(inputEl)) {\n"
        "      return { text: '', chipCount: 0, chipSignature: '0::', hasContent: false, hasText: false, hasChips: false, hasContainer: false, isAttachmentLoading: false };\n"
        "    }\n"
        "    const inputs = collectPromptInputElements(containers, inputEl);\n"
        "    const textParts = [];\n"
        "    for (const el of inputs) {\n"
        "      const val = getEditableInputText(el);\n"
        "      if (!isPlaceholderOnlyInputText(el, val)) {\n"
        "        textParts.push(val);\n"
        "      }\n"
        "    }\n"
        "    const chipParts = collectMergedAttachmentChips(containers);\n"
        "    const combinedText = textParts.join('\\n');\n"
        "    const chipSignature = chipParts.length + '::' + chipParts.join('|');\n"
        "    const hasText = combinedText.trim().length > 0;\n"
        "    const hasChips = chipParts.length > 0;\n"
        "    const hasContent = hasText || hasChips;\n"
        "    const isAttachmentLoading = containers.some((box) => isPromptAttachmentLoading(box));\n"
        "    return {\n"
        "      text: combinedText,\n"
        "      chipCount: chipParts.length,\n"
        "      chipSignature,\n"
        "      hasContent,\n"
        "      hasText,\n"
        "      hasChips,\n"
        "      hasContainer: true,\n"
        "      isAttachmentLoading,\n"
        "    };\n"
        "  }\n\n"
        "  function isEditableInputElement(el) {\n"
        "    if (!el || el.nodeType !== 1) return false;\n"
        "    if (el.disabled || el.readOnly || el.getAttribute('aria-disabled') === 'true' || el.getAttribute('aria-readonly') === 'true') {\n"
        "      return false;\n"
        "    }\n"
        "    const tag = (el.tagName || '').toUpperCase();\n"
        "    if (tag === 'TEXTAREA') return true;\n"
        "    if (el.isContentEditable) return true;\n"
        "    return el.getAttribute('role') === 'textbox';\n"
        "  }\n\n"
        "  function canTriggerKeyboardSubmit(ev) {\n"
        "    const target = ev.target && ev.target.nodeType === 1 ? ev.target : document.activeElement;\n"
        "    if (!isEditableInputElement(target)) return false;\n"
        "    const visibleButtons = Array.from(document.querySelectorAll('ms-run-button')).filter((el) => isElementVisible(el));\n"
        "    if (visibleButtons.length > 0 && visibleButtons.every((btn) => isRunButtonDisabled(btn))) {\n"
        "      return false;\n"
        "    }\n"
        "    return true;\n"
        "  }\n\n"
        "  function findPromptInputElement() {\n"
        "    // querySelectorAllのDOM出現順ではなく具象セレクタの優先順位で探索\n"
        "    const boxSelectors = [\n"
        "      'ms-prompt-renderer ms-chunk-editor footer ms-prompt-box',\n"
        "      'ms-prompt-renderer ms-chunk-editor footer ms-prompt-input-wrapper',\n"
        "      'ms-prompt-renderer ms-prompt-box',\n"
        "      'ms-prompt-input-wrapper',\n"
        "      'ms-prompt-box',\n"
        "      'ms-autosize-textarea',\n"
        "      'footer',\n"
        "      'ms-chunk-editor',\n"
        "      'ms-prompt-renderer',\n"
        "      'form',\n"
        "    ];\n"
        "    for (const boxSel of boxSelectors) {\n"
        "      const boxes = document.querySelectorAll(boxSel);\n"
        "      for (const box of boxes) {\n"
        "        if (box.closest('ms-chat-turn, ms-system-instructions, ms-run-settings, ms-prompt-options, ms-toolbar, [role=\"toolbar\"]') || !isElementVisible(box)) continue;\n"
        "        const inputs = box.querySelectorAll('textarea, [contenteditable], [role=\"textbox\"]');\n"
        "        for (const el of inputs) {\n"
        "          if (isComposerInputCandidate(el)) {\n"
        "            return el;\n"
        "          }\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "    if (document.activeElement && isComposerInputCandidate(document.activeElement)) {\n"
        "      return document.activeElement;\n"
        "    }\n"
        "    const fallbackInputs = document.querySelectorAll('footer textarea, textarea, [role=\"textbox\"], [contenteditable]');\n"
        "    for (const el of fallbackInputs) {\n"
        "      if (isComposerInputCandidate(el)) {\n"
        "        return el;\n"
        "      }\n"
        "    }\n"
        "    return null;\n"
        "  }\n\n"
        "  function pasteTextIntoPromptInput(el, text) {\n"
        "    if (!el || !el.isConnected) return false;\n"
        "    const tag = (el.tagName || '').toUpperCase();\n"
        "    el.focus();\n"
        "    if (tag === 'TEXTAREA' || tag === 'INPUT') {\n"
        "      const proto = tag === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;\n"
        "      const desc = Object.getOwnPropertyDescriptor(proto, 'value');\n"
        "      if (desc && typeof desc.set === 'function') {\n"
        "        desc.set.call(el, text);\n"
        "      } else {\n"
        "        el.value = text;\n"
        "      }\n"
        "      try {\n"
        "        el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertFromPaste', data: text }));\n"
        "      } catch (_e) {\n"
        "        el.dispatchEvent(new Event('input', { bubbles: true }));\n"
        "      }\n"
        "      el.dispatchEvent(new Event('change', { bubbles: true }));\n"
        "      try {\n"
        "        if (typeof el.setSelectionRange === 'function') {\n"
        "          el.setSelectionRange(text.length, text.length);\n"
        "        }\n"
        "        el.scrollTop = el.scrollHeight;\n"
        "      } catch (_e) {}\n"
        "      return true;\n"
        "    }\n"
        "    const applyFallbackNodes = () => {\n"
        "      while (el.firstChild) el.removeChild(el.firstChild);\n"
        "      const lines = String(text || '').split(/\\r?\\n/);\n"
        "      for (let i = 0; i < lines.length; i += 1) {\n"
        "        if (i > 0) el.appendChild(document.createElement('br'));\n"
        "        if (lines[i]) el.appendChild(document.createTextNode(lines[i]));\n"
        "      }\n"
        "    };\n"
        "    try {\n"
        "      const sel = window.getSelection();\n"
        "      if (sel) {\n"
        "        const range = document.createRange();\n"
        "        range.selectNodeContents(el);\n"
        "        sel.removeAllRanges();\n"
        "        sel.addRange(range);\n"
        "      }\n"
        "      const inserted = Boolean(document.execCommand && document.execCommand('insertText', false, text));\n"
        "      if (!inserted || !getEditableInputText(el).trim()) {\n"
        "        applyFallbackNodes();\n"
        "      }\n"
        "    } catch (_e) {\n"
        "      applyFallbackNodes();\n"
        "    }\n"
        "    try {\n"
        "      el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertFromPaste', data: text }));\n"
        "    } catch (_e) {\n"
        "      el.dispatchEvent(new Event('input', { bubbles: true }));\n"
        "    }\n"
        "    el.dispatchEvent(new Event('change', { bubbles: true }));\n"
        "    return true;\n"
        "  }\n\n"
    )
