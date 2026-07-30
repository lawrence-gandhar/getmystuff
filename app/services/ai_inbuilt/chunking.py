"""
Pure text chunking for the in-built embedding pipeline — no I/O, no DB.
Kept separate from embedding_service.py so the splitting logic can be unit
tested/tuned in isolation from the Ollama/DB calls that consume it.
"""

import re
from typing import List

_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def split_text(text: str, max_chars: int = 1200, overlap_chars: int = 150) -> List[str]:
    """
    Split `text` into chunks of at most `max_chars`, paragraph-aware: consecutive
    paragraphs are packed together up to the limit, and any single paragraph
    longer than the limit is hard-split with `overlap_chars` of context carried
    into the next piece.

    max_chars=1200 (~300 tokens) stays comfortably inside nomic-embed-text's
    2048-token context window, so chunks aren't silently truncated by the
    embedding model before they're vectorized.
    """
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: List[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) <= max_chars:
            current = paragraph
            continue

        # A single paragraph longer than max_chars — hard-split with overlap.
        start = 0
        while start < len(paragraph):
            end = start + max_chars
            chunks.append(paragraph[start:end])
            start = end - overlap_chars if end < len(paragraph) else end

    if current:
        chunks.append(current)

    return chunks
