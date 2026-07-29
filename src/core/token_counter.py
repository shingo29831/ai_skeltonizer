# AI Role Comment: Estimates token counts for text using language-specific character and word density heuristics without external dependencies.

import re
from typing import Dict


def estimate_tokens(text: str) -> int:
    if not isinstance(text, str):
        raise ValueError('Input text must be a valid string.')

    if not text.strip():
        return 0

    japanese_chars = re.findall(r'[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uff9f\u4e00-\u9faf\u3400-\u4dbf]', text)
    japanese_count = len(japanese_chars)

    non_japanese_text = re.sub(r'[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\uff00-\uff9f\u4e00-\u9faf\u3400-\u4dbf]', ' ', text)
    english_words = [word for word in non_japanese_text.split() if word.strip()]
    english_word_count = len(english_words)

    # Japanese BPE tokenization averages around 1.0 to 1.2 characters per token in modern LLMs.
    japanese_tokens = int(japanese_count * 1.1)

    # English words average around 1.3 tokens per word in standard BPE tokenizers.
    english_tokens = int(english_word_count * 1.3)

    return japanese_tokens + english_tokens


def format_token_display(tokens: int) -> str:
    if tokens < 0:
        raise ValueError('Token count cannot be negative.')
    return f'想定トークン数: {tokens:,}'