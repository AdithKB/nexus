"""Shared title-similarity helpers for find_duplicates.py and merge_duplicates.py."""
import re

STOPWORDS = {"a", "an", "the", "and", "or", "of", "in", "to", "for", "with",
             "on", "at", "by", "from", "is", "are", "was", "were", "be",
             "that", "this", "as", "it", "its", "not", "but", "can", "do"}


def title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9']+", title.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
