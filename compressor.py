"""
compressor.py — LexRank extractive transcript compression.

Reduces long transcripts to the top 25% most semantically central sentences
before sending to Gemini. Safe fallback: returns original text unchanged if
sumy is unavailable or fails.

Author: Adith K B
"""
from __future__ import annotations

COMPRESS_THRESHOLD_CHARS = 16_000   # ~4k tokens; config overrides this
COMPRESS_TARGET_RATIO    = 0.25     # keep top 25% of sentences


def compress_transcript(
    text: str,
    threshold_chars: int = COMPRESS_THRESHOLD_CHARS,
    target_ratio: float = COMPRESS_TARGET_RATIO,
) -> tuple[str, bool, int, int]:
    """
    Compress a transcript using LexRank extractive summarization.

    Returns:
        (compressed_text, was_compressed, original_chars, compressed_chars)

    Falls back to (original_text, False, len(text), len(text)) on any failure.
    """
    if not text or len(text) <= threshold_chars:
        return text, False, len(text or ""), len(text or "")

    try:
        from sumy.parsers.plaintext import PlaintextParser
        from sumy.nlp.tokenizers import Tokenizer
        from sumy.summarizers.lex_rank import LexRankSummarizer
        from sumy.nlp.stemmers import Stemmer
        from sumy.utils import get_stop_words

        parser   = PlaintextParser.from_string(text, Tokenizer("english"))
        stemmer  = Stemmer("english")
        summary  = LexRankSummarizer(stemmer)
        summary.stop_words = get_stop_words("english")

        # Count total sentences, target top ratio
        total_sentences = len(list(parser.document.sentences))
        keep = max(10, int(total_sentences * target_ratio))

        compressed_sentences = summary(parser.document, keep)
        compressed = " ".join(str(sentence) for sentence in compressed_sentences)
        
        return compressed, True, len(text), len(compressed)

    except Exception as e:
        # sumy not installed, NLTK data missing, or any other failure
        print(f"[COMPRESS] Failed: {e}")
        return text, False, len(text), len(text)
