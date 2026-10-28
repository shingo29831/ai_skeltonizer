# AI Role Comment: Estimates token counts for text using language-specific character and word density heuristics without external dependencies.

import re
from typing import Dict


def estimate_tokens(text: str) -> int:
    if not isinstance(text, str):
        raise ValueError('Input text must be a valid string.')

    if not text.strip():
        return 0

    tokens = 0.0

    # BPEで消滅しやすい改行・空白・記号・サブワードを高精度推計
    tokens += len(re.findall(r'\r\n|\r|\n', text))
    for spaces in re.findall(r' {2,}', text):
        tokens += max(1.0, len(spaces) / 3.0)

    cjk_chars = re.findall(r'[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uff9f\u4e00-\u9faf\u3400-\u4dbf]', text)
    tokens += len(cjk_chars) * 1.2

    special_symbols = re.findall(r'[\u2500-\u257f\u2580-\u259f\u2190-\u21ff\u2600-\u26ff\u2700-\u27bf]', text)
    tokens += len(special_symbols) * 1.2

    ascii_symbols = re.findall(r'[!-/:-@\[-`{-~]', text)
    tokens += len(ascii_symbols) * 1.0

    clean_text = re.sub(
        r'[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uff9f\u4e00-\u9faf\u3400-\u4dbf\u2500-\u257f\u2580-\u259f\u2190-\u21ff\u2600-\u26ff\u2700-\u27bf]',
        ' ',
        text,
    )
    for word in re.findall(r'[A-Za-z]+|\d+', clean_text):
        if word.isdigit():
            tokens += max(1.0, (len(word) + 2) // 3)
        else:
            subwords = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)', word)
            for sub in subwords or [word]:
                length = len(sub)
                if length <= 4:
                    tokens += 1.0
                elif length <= 8:
                    tokens += 1.3
                else:
                    tokens += length / 3.5

    return int(round(tokens))


def format_token_display(tokens: int) -> str:
    if tokens < 0:
        raise ValueError('Token count cannot be negative.')
    return f'想定トークン数: {tokens:,}'