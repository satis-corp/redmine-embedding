"""Turn a SourceDocument into embeddable chunks.

``content`` is what users see in search results (original text);
``embed_text`` is what gets embedded (traces/logs normalised, title added).
Chunk size is a plain character budget for now.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sources.base import SourceDocument
from .stacktrace import normalize_log, normalize_trace, split_segments

_SENTENCE_BREAK = re.compile(r"(?<=[。！？])|\n|(?<=[.!?])\s+")
MAX_DISPLAY_CHARS = 4000  # cap for pasted traces/logs shown back to the user


@dataclass(slots=True)
class ChunkDraft:
    chunk_no: int
    content: str
    embed_text: str
    url_fragment: str = ""


def _hard_cut(text: str, max_chars: int) -> list[str]:
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def _break_long(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    pieces: list[str] = []
    cur = ""
    for part in (p for p in _SENTENCE_BREAK.split(paragraph) if p and p.strip()):
        if len(part) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.extend(_hard_cut(part, max_chars))
        elif cur and len(cur) + len(part) > max_chars:
            pieces.append(cur)
            cur = part
        else:
            cur += part
    if cur:
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def split_text(text: str, max_chars: int) -> list[str]:
    """Pack paragraphs up to ``max_chars``; over-long paragraphs break at sentences."""
    chunks: list[str] = []
    cur = ""
    for para in (p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        for piece in _break_long(para, max_chars):
            if cur and len(cur) + 2 + len(piece) > max_chars:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def chunk_document(doc: SourceDocument, max_chars: int = 1000) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []

    def add(content: str, embed_text: str, fragment: str) -> None:
        if embed_text.strip():
            drafts.append(ChunkDraft(len(drafts), content, embed_text, fragment))

    for section in doc.sections:
        for seg in split_segments(section.text):
            if seg.kind == "text":
                for piece in split_text(seg.text, max_chars):
                    # Give each piece its document title as context, unless it already starts with it.
                    embed = piece if not doc.title or piece.startswith(doc.title) else f"{doc.title}\n{piece}"
                    add(piece, embed, section.url_fragment)
            else:
                normalised = normalize_trace(seg.text) if seg.kind == "trace" else normalize_log(seg.text)
                add(seg.text[:MAX_DISPLAY_CHARS], normalised[:max_chars], section.url_fragment)
    return drafts
