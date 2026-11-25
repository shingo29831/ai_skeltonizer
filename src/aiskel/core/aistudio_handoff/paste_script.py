"""Module: @role: プロンプト入力欄への汎用待機貼付・反映検証および要約プロンプト挿入のクライアントJavaScriptを生成する。"""

def build_handoff_paste_js() -> str:
    """待機貼付エンジン・テキスト照合・要約プロンプト挿入のJSコード群を返す。"""
    return (
        "  const HANDOFF_PASTE_TIMEOUT_MS = 25000;\n"
        "  const HANDOFF_PASTE_POLL_MS = 250;\n"
        "  const HANDOFF_HYDRATION_SETTLE_MS = 400;\n"
        "  const HANDOFF_VERIFY_DELAY_MS = 350;\n"
        "  const HANDOFF_MAX_PASTE_RETRIES = 2;\n"
        "  const SUMMARY_PROMPT_PASTE_TIMEOUT_MS = 10000;\n"
        "  let activePasteTask = null;\n"
        "  let pasteTaskSeq = 0;\n"
        "  let handoffSessionActive = false;\n\n"
        "  function hasActiveHandoffUrlMarker() {\n"
        "    if (typeof readExtensionHandoffIdFromHash === 'function' && readExtensionHandoffIdFromHash()) {\n"
        "      return true;\n"
        "    }\n"
        "    if (typeof readPendingUserscriptHandoffId === 'function' && readPendingUserscriptHandoffId()) {\n"
        "      return true;\n"
        "    }\n"
        "    return false;\n"
        "  }\n\n"
        "  function isHandoffPendingOrActive() {\n"
        "    if (handoffSessionActive || hasActiveHandoffUrlMarker()) return true;\n"
        "    return Boolean(activePasteTask && activePasteTask.priority === 'handoff');\n"
        "  }\n\n"
        "  function normalizeVerifyText(text) {\n"
        "    return String(text || '').replace(/\\r\\n/g, '\\n').trim();\n"
        "  }\n\n"
        "  function collapseWhitespaceForVerify(text) {\n"
        "    return String(text || '').replace(/\\s+/g, ' ').trim();\n"
        "  }\n\n"
        "  function matchAnchoredText(haystack, needle) {\n"
        "    if (!haystack || !needle) return false;\n"
        "    if (needle.length <= 120) {\n"
        "      return haystack.includes(needle);\n"
        "    }\n"
        "    return haystack.includes(needle.slice(0, 60)) && haystack.includes(needle.slice(-60));\n"
        "  }\n\n"
        "  function isHandoffTextPresent(currentVal, expectedText) {\n"
        "    const normCurrent = normalizeVerifyText(currentVal);\n"
        "    const normExpected = normalizeVerifyText(expectedText);\n"
        "    if (!normCurrent || !normExpected) return false;\n"
        "    if (matchAnchoredText(normCurrent, normExpected)) return true;\n"
        "    // contenteditableでの連続改行/空白のDOM正規化差異を許容するため空白縮約でも照合\n"
        "    return matchAnchoredText(\n"
        "      collapseWhitespaceForVerify(currentVal),\n"
        "      collapseWhitespaceForVerify(expectedText)\n"
        "    );\n"
        "  }\n\n"
        "  function cancelActivePasteTask() {\n"
        "    if (activePasteTask && typeof activePasteTask.cancel === 'function') {\n"
        "      activePasteTask.cancel();\n"
        "    }\n"
        "    activePasteTask = null;\n"
        "  }\n\n"
        "  function waitAndPasteText(options) {\n"
        "    const textToPaste = options.textToPaste;\n"
        "    const priority = options.priority || 'normal';\n"
        "    if (activePasteTask && activePasteTask.priority === 'handoff' && priority !== 'handoff') {\n"
        "      return false;\n"
        "    }\n"
        "    cancelActivePasteTask();\n"
        "    const taskId = ++pasteTaskSeq;\n"
        "    const timeoutMs = options.timeoutMs || HANDOFF_PASTE_TIMEOUT_MS;\n"
        "    const settleMs = options.settleMs !== undefined ? options.settleMs : HANDOFF_HYDRATION_SETTLE_MS;\n"
        "    const requireEmptyInput = Boolean(options.requireEmptyInput);\n"
        "    const insertPosition = options.insertPosition === 'prepend' ? 'prepend' : 'append';\n"
        "    const abortIf = options.abortIf || null;\n"
        "    const deadline = Date.now() + timeoutMs;\n"
        "    let finished = false;\n"
        "    let observerHandle = null;\n"
        "    let pollTimer = null;\n"
        "    let settleTimer = null;\n"
        "    let verifyTimer = null;\n"
        "    let settlingPaste = false;\n"
        "    let pasteRetries = 0;\n"
        "    let draftPrefix = '';\n"
        "    let draftSuffix = '';\n"
        "    let preInputSnap = null;\n\n"
        "    const cleanup = () => {\n"
        "      if (observerHandle) {\n"
        "        observerHandle.disconnect();\n"
        "        observerHandle = null;\n"
        "      }\n"
        "      if (pollTimer !== null) {\n"
        "        window.clearTimeout(pollTimer);\n"
        "        pollTimer = null;\n"
        "      }\n"
        "      if (settleTimer !== null) {\n"
        "        window.clearTimeout(settleTimer);\n"
        "        settleTimer = null;\n"
        "      }\n"
        "      if (verifyTimer !== null) {\n"
        "        window.clearTimeout(verifyTimer);\n"
        "        verifyTimer = null;\n"
        "      }\n"
        "      if (activePasteTask && activePasteTask.id === taskId) {\n"
        "        activePasteTask = null;\n"
        "      }\n"
        "    };\n\n"
        "    const isInactive = () => finished || taskId !== pasteTaskSeq;\n"
        "    const finishWithAbort = () => {\n"
        "      finished = true;\n"
        "      cleanup();\n"
        "      if (options.onAborted) options.onAborted();\n"
        "    };\n\n"
        "    activePasteTask = {\n"
        "      id: taskId,\n"
        "      priority,\n"
        "      cancel: () => {\n"
        "        if (finished) return;\n"
        "        finished = true;\n"
        "        cleanup();\n"
        "      },\n"
        "    };\n\n"
        "    const attemptPaste = () => {\n"
        "      if (isInactive() || settlingPaste) return;\n"
        "      if (abortIf && abortIf()) {\n"
        "        finishWithAbort();\n"
        "        return;\n"
        "      }\n"
        "      if (Date.now() > deadline) {\n"
        "        finished = true;\n"
        "        cleanup();\n"
        "        if (options.onTimeout) options.onTimeout();\n"
        "        return;\n"
        "      }\n"
        "      if (isInferring()) {\n"
        "        pollTimer = window.setTimeout(attemptPaste, HANDOFF_PASTE_POLL_MS);\n"
        "        return;\n"
        "      }\n"
        "      const inputEl = findPromptInputElement();\n"
        "      if (!inputEl) {\n"
        "        pollTimer = window.setTimeout(attemptPaste, HANDOFF_PASTE_POLL_MS);\n"
        "        return;\n"
        "      }\n"
        "      settlingPaste = true;\n"
        "      // Angular SPAの初期フォームリセットで入力が消されないよう少し待機してから貼り付ける\n"
        "      settleTimer = window.setTimeout(() => {\n"
        "        settleTimer = null;\n"
        "        if (isInactive()) return;\n"
        "        if (abortIf && abortIf()) {\n"
        "          finishWithAbort();\n"
        "          return;\n"
        "        }\n"
        "        const readyEl = findPromptInputElement() || inputEl;\n"
        "        if (!readyEl || !readyEl.isConnected) {\n"
        "          settlingPaste = false;\n"
        "          pollTimer = window.setTimeout(attemptPaste, HANDOFF_PASTE_POLL_MS);\n"
        "          return;\n"
        "        }\n"
        "        const existing = getEditableInputText(readyEl);\n"
        "        const duplicateFound = options.exactDuplicateCheck\n"
        "          ? collapseWhitespaceForVerify(existing).includes(collapseWhitespaceForVerify(textToPaste))\n"
        "          : isHandoffTextPresent(existing, textToPaste);\n"
        "        if (duplicateFound) {\n"
        "          finished = true;\n"
        "          cleanup();\n"
        "          if (options.onSuccess) options.onSuccess(true, false, { preSnap: preInputSnap });\n"
        "          return;\n"
        "        }\n"
        "        // リトライ時は初回貼付の未反映断片が残るため空入力検査と下書き取得は初回のみ実施\n"
        "        if (pasteRetries === 0) {\n"
        "          preInputSnap = typeof getPromptInputSnapshot === 'function'\n"
        "            ? getPromptInputSnapshot(typeof findPromptRunButton === 'function' ? findPromptRunButton() : null)\n"
        "            : null;\n"
        "          if (requireEmptyInput && typeof isPromptInputEmpty === 'function' && !isPromptInputEmpty(readyEl)) {\n"
        "            finished = true;\n"
        "            cleanup();\n"
        "            if (options.onSkippedNonEmpty) options.onSkippedNonEmpty();\n"
        "            return;\n"
        "          }\n"
        "          const placeholder = (readyEl.getAttribute('placeholder') || readyEl.getAttribute('data-placeholder') || '').trim();\n"
        "          const existingTrimmed = existing.trim();\n"
        "          if (!options.replaceExisting && !requireEmptyInput && existingTrimmed && existingTrimmed !== placeholder) {\n"
        "            if (insertPosition === 'prepend') {\n"
        "              const cleanExisting = existing.replace(/^(?:[ \\t]*\\r?\\n)+/, '');\n"
        "              draftSuffix = '\\n\\n' + cleanExisting;\n"
        "            } else {\n"
        "              draftPrefix = existing + '\\n\\n';\n"
        "            }\n"
        "          }\n"
        "        }\n"
        "        const nextValue = draftPrefix + textToPaste + draftSuffix;\n"
        "        const ok = pasteTextIntoPromptInput(readyEl, nextValue);\n"
        "        if (!ok) {\n"
        "          settlingPaste = false;\n"
        "          pollTimer = window.setTimeout(attemptPaste, HANDOFF_PASTE_POLL_MS);\n"
        "          return;\n"
        "        }\n"
        "        verifyTimer = window.setTimeout(() => {\n"
        "          verifyTimer = null;\n"
        "          if (isInactive()) return;\n"
        "          // 貼付直後の手動送信や画面遷移で空になった場合に誤リトライ・誤エラーを出さないよう検証前にも確認\n"
        "          if (abortIf && abortIf()) {\n"
        "            finishWithAbort();\n"
        "            return;\n"
        "          }\n"
        "          const verifyEl = findPromptInputElement() || readyEl;\n"
        "          const currentVal = verifyEl ? getEditableInputText(verifyEl) : '';\n"
        "          // 既存下書きやプレースホルダーのみ残った場合を弾くため貼付テキスト本体のアンカー一致を検証\n"
        "          if (!verifyEl || !isHandoffTextPresent(currentVal, textToPaste)) {\n"
        "            if (pasteRetries < HANDOFF_MAX_PASTE_RETRIES) {\n"
        "              pasteRetries += 1;\n"
        "              settlingPaste = false;\n"
        "              pollTimer = window.setTimeout(attemptPaste, HANDOFF_PASTE_POLL_MS);\n"
        "              return;\n"
        "            }\n"
        "            finished = true;\n"
        "            cleanup();\n"
        "            if (options.onVerifyFailed) options.onVerifyFailed();\n"
        "            return;\n"
        "          }\n"
        "          finished = true;\n"
        "          cleanup();\n"
        "          if (options.onSuccess) options.onSuccess(false, Boolean(draftPrefix || draftSuffix), { preSnap: preInputSnap });\n"
        "        }, HANDOFF_VERIFY_DELAY_MS);\n"
        "      }, settleMs);\n"
        "    };\n\n"
        "    observerHandle = new MutationObserver(() => {\n"
        "      if (!isInactive() && !settlingPaste && findPromptInputElement()) {\n"
        "        attemptPaste();\n"
        "      }\n"
        "    });\n"
        "    if (document.body) {\n"
        "      observerHandle.observe(document.body, { childList: true, subtree: true });\n"
        "    }\n"
        "    attemptPaste();\n"
        "    return true;\n"
        "  }\n\n"
        "  function pasteSummaryAndPromptToChat(combinedText, options) {\n"
        "    const cleanText = String(combinedText || '').trim();\n"
        "    const opts = typeof options === 'function' ? { onDone: options } : (options || {});\n"
        "    const notifyDone = (ok, message) => {\n"
        "      if (typeof opts.onDone === 'function') opts.onDone(ok, message || '');\n"
        "    };\n"
        "    if (!cleanText) {\n"
        "      showToast('aiskel 挿入する要約・プロンプトが空です', true);\n"
        "      notifyDone(false, '挿入する要約・プロンプトが空です');\n"
        "      return false;\n"
        "    }\n"
        "    if (isHandoffPendingOrActive()) {\n"
        "      showToast('aiskel 新規タブ引き継ぎ処理中のため挿入をスキップしました', true);\n"
        "      notifyDone(false, '新規タブ引き継ぎ処理中のため挿入をスキップしました');\n"
        "      return false;\n"
        "    }\n"
        "    return waitAndPasteText({\n"
        "      textToPaste: cleanText,\n"
        "      priority: 'normal',\n"
        "      timeoutMs: SUMMARY_PROMPT_PASTE_TIMEOUT_MS,\n"
        "      settleMs: 120,\n"
        "      requireEmptyInput: false,\n"
        "      exactDuplicateCheck: true,\n"
        "      insertPosition: 'prepend',\n"
        "      abortIf: () => isHandoffPendingOrActive(),\n"
        "      onAborted: () => {\n"
        "        notifyDone(false, '挿入が中断されました');\n"
        "      },\n"
        "      onSuccess: (alreadyPresent, insertedWithDraft) => {\n"
        "        if (alreadyPresent) {\n"
        "          showToast('aiskel プロジェクト要約とプロンプトは既にチャット欄に入っています', false);\n"
        "          notifyDone(true, '既にチャット欄に入っています');\n"
        "        } else {\n"
        "          showToast(\n"
        "            insertedWithDraft\n"
        "              ? 'aiskel 入力途中のチャットの上部にプロジェクト要約とプロンプトを挿入しました'\n"
        "              : 'aiskel プロジェクト要約とプロンプトをチャット欄に挿入しました',\n"
        "            false\n"
        "          );\n"
        "          notifyDone(true, '');\n"
        "        }\n"
        "      },\n"
        "      onTimeout: () => {\n"
        "        showToast('aiskel チャット入力欄が見つからず挿入できませんでした', true);\n"
        "        notifyDone(false, 'チャット入力欄が見つかりませんでした');\n"
        "      },\n"
        "      onVerifyFailed: () => {\n"
        "        showToast('aiskel プロジェクト要約とプロンプトの挿入を確認できませんでした', true);\n"
        "        notifyDone(false, 'チャット欄への挿入を確認できませんでした');\n"
        "      },\n"
        "    });\n"
        "  }\n\n"
    )
